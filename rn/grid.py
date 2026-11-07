"""rn grid: slice a sheet (storyboard, character turnaround, contact sheet, a model's 2x2 / 3x3 grid) into its tiles.
Rows and columns not given are found from the gutters: full-length lines of one even colour between the tiles.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .ff import RnError

EVEN = 8            # a gutter line's pixels stay within this (0..255) of their median
MIN_TILE = 0.05     # a band thinner than this share of the side is not a tile (a stray line, a caption strip)
TRIM_TOLERANCE = 24


def _even_lines(rgb: np.ndarray, axis: int) -> np.ndarray:
    """Per row (axis 1) or column (axis 0): every pixel within EVEN of the line's median colour."""
    median = np.median(rgb, axis=axis, keepdims=True)
    return (np.abs(rgb.astype(np.int16) - median).max(axis=-1) <= EVEN).all(axis=axis)


def _bands(even: np.ndarray, min_size: int) -> list[tuple[int, int]]:
    """Runs of uneven lines (the tiles) at least min_size long, as [start, end)."""
    bands, start = [], None
    for i, flat in enumerate([*even, True]):
        if not flat and start is None:
            start = i
        elif flat and start is not None:
            if i - start >= min_size:
                bands.append((start, i))
            start = None
    return bands


def _split(size: int, count: int, gap: int) -> list[tuple[int, int]]:
    tile = (size - gap * (count - 1)) / count
    if tile < 1:
        raise RnError(f'{count} tiles with a {gap}px gap do not fit in {size}px')
    return [(round(k * (tile + gap)), round(k * (tile + gap) + tile)) for k in range(count)]


def _trim(tile: np.ndarray) -> tuple[int, int, int, int]:
    """Bounds [x0, y0, x1, y1) of what differs from the tile's own edge colour (a uniform frame or margin)."""
    ring = np.concatenate([tile[0], tile[-1], tile[:, 0], tile[:, -1]])
    edge = np.median(ring, axis=0)
    content = np.abs(tile.astype(np.int16) - edge).max(axis=-1) > TRIM_TOLERANCE
    ys, xs = np.nonzero(content.any(axis=1))[0], np.nonzero(content.any(axis=0))[0]
    if not len(ys):
        return 0, 0, tile.shape[1], tile.shape[0]
    return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1


def grid_command(path: str, *, out: str, rows: int | None = None, cols: int | None = None, gap: int = 0, trim: bool = False) -> dict:
    from PIL import Image

    try:
        image = Image.open(path).convert('RGB')
    except (OSError, ValueError) as error:
        raise RnError(f'{path}: not an image ({error})') from None
    rgb = np.asarray(image)
    height, width = rgb.shape[:2]
    found = {}
    if rows:
        row_bands = _split(height, rows, gap)
    else:
        row_bands = _bands(_even_lines(rgb, 1), max(2, round(height * MIN_TILE)))
        found['rows'] = len(row_bands)
    if cols:
        col_bands = _split(width, cols, gap)
    else:
        col_bands = _bands(_even_lines(rgb, 0), max(2, round(width * MIN_TILE)))
        found['cols'] = len(col_bands)
    if not row_bands or not col_bands:
        raise RnError(f'{path}: no tiles found (an empty or one-colour image?); give --rows and --cols')

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for r, (y0, y1) in enumerate(row_bands, 1):
        for c, (x0, x1) in enumerate(col_bands, 1):
            box = [x0, y0, x1, y1]
            if trim:
                tx0, ty0, tx1, ty1 = _trim(rgb[y0:y1, x0:x1])
                box = [x0 + tx0, y0 + ty0, x0 + tx1, y0 + ty1]
            target = out_dir / f'r{r}c{c}.png'
            image.crop(tuple(box)).save(target)
            tiles.append({'row': r, 'col': c, 'path': str(target), 'box': [box[0], box[1], box[2] - box[0], box[3] - box[1]]})
    sizes = [(t['box'][2], t['box'][3]) for t in tiles]
    return {'path': str(path), 'size': [width, height], 'rows': len(row_bands), 'cols': len(col_bands), 'found': found,
            'gap': gap if rows or cols else None, 'trim': trim, 'dir': str(out_dir), 'tiles': tiles,
            'tile_sizes': {'min': list(min(sizes)), 'max': list(max(sizes))}}


def summary_lines(result: dict) -> list[str]:
    how = ', '.join(f'{k} found from the gutters' for k in result['found']) or 'as given'
    lines = [f'{result["path"]}: {result["size"][0]}x{result["size"][1]} -> {result["rows"]} rows x {result["cols"]} cols ({how})',
             f'{result["dir"]}/r1c1.png .. r{result["rows"]}c{result["cols"]}.png  tile sizes {result["tile_sizes"]["min"]}..{result["tile_sizes"]["max"]}']
    widths = [t['box'][2] for t in result['tiles']]
    heights = [t['box'][3] for t in result['tiles']]
    if result['found'] and not result['trim'] and (max(widths) > 1.2 * min(widths) or max(heights) > 1.2 * min(heights)):
        lines.append('check: tiles differ in size by over 20%: a blank row or column inside a tile may read as a gutter, '
                     'or tiles touch without one; give --rows / --cols if the count is wrong')
    if result['found'] and result['rows'] * result['cols'] == 1:
        lines.append('check: one tile only: the tiles may touch with no gutter; give --rows and --cols')
    return lines
