"""rn frame / rn sheet: exact frames by decoded index, and labelled contact sheets small enough to view."""
from __future__ import annotations

import io
import math
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from .ff import RnError, probe, run, source_fps

# sandbox_exec shows at most 4 images totalling 512 KB per call, so a page defaults to 125 KB.
DEFAULT_MAX_KB = 125
MAX_PER_PAGE = 24


def _select_expr(indices: list[int]) -> str:
    return '+'.join(f'eq(n\\,{index})' for index in indices)


def extract(path: str | Path, indices: list[int], out_dir: Path, *, width: int | None = None) -> list[tuple[int, Path]]:
    """Decodes from the start and keeps the frames whose decoded index is listed. Exact by construction:
    no input seeking (which can land 1-2 frames off on x264) and no timestamp arithmetic.
    ponytail: decode time grows with the source; fine for clips and references up to a few minutes."""
    wanted = sorted(set(i for i in indices if i >= 0))
    if not wanted:
        raise RnError('no frames selected')
    results: list[tuple[int, Path]] = []
    for start in range(0, len(wanted), 200):  # keep the select expression a sane length
        chunk = wanted[start:start + 200]
        pattern = out_dir / f'f{start:06d}_%06d.png'
        filters = f"select='{_select_expr(chunk)}'" + (f',scale={width}:-2:flags=bicubic' if width else '')
        run(['ffmpeg', '-i', str(path), '-vf', filters, '-fps_mode', 'passthrough', '-frames:v', str(len(chunk)), '-y', str(pattern)])
        produced = sorted(out_dir.glob(f'f{start:06d}_*.png'))
        results.extend(zip(chunk, produced))
    return results


def frame_command(path: str, *, index: int | None, time: float | None, last: bool, out: str) -> dict:
    info = probe(path)
    video = info['video']
    if not video:
        raise RnError(f'{path}: no video stream')
    if last:
        info = probe(path, exact=True)
        index = info['video']['frames'] - 1
    elif time is not None:
        index = math.floor(time * float(source_fps(info)) + 1e-6)
    assert index is not None
    with tempfile.TemporaryDirectory() as tmp:
        got = extract(path, [index], Path(tmp))
        if not got:
            raise RnError(f'{path}: frame {index} does not exist (the clip has {video["frames"]} frames)')
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        if Path(out).suffix.lower() in ('.jpg', '.jpeg'):
            run(['ffmpeg', '-i', str(got[0][1]), '-q:v', '2', '-y', out])
        else:
            shutil.copyfile(got[0][1], out)
    fps = source_fps(info)
    return {'path': out, 'index': index, 'time': round(float(index / fps), 4)}


def pick_indices(frames: int, fps: Fraction, *, every: int | None, rate: float | None, indices: list[int] | None,
                 seconds: list[float] | None, count: int | None) -> list[int]:
    if indices:
        return [i for i in indices if 0 <= i < frames]
    if seconds:
        return sorted({min(frames - 1, math.floor(s * float(fps) + 1e-6)) for s in seconds if s >= 0})
    if every:
        return list(range(0, frames, every))
    if rate:
        step = float(fps) / rate
        return sorted({min(frames - 1, round(k * step)) for k in range(math.ceil(frames / step))})
    count = min(count or 16, frames)
    if count <= 1:
        return [0]
    return sorted({round(k * (frames - 1) / (count - 1)) for k in range(count)})


def _timecode(index: int, fps: Fraction) -> str:
    seconds = float(index / fps)
    return f'{int(seconds // 60):02d}:{seconds % 60:05.2f}'


def _compose(tiles: list[tuple[int, Path]], fps: Fraction, columns: int, title: str):
    from PIL import Image, ImageDraw, ImageFont

    images = [Image.open(path).convert('RGB') for _, path in tiles]
    tile_w, tile_h = images[0].size
    label_h = max(14, tile_w // 14)
    font = ImageFont.load_default(size=label_h - 3)
    title_h = label_h + 4
    rows = math.ceil(len(images) / columns)
    sheet = Image.new('RGB', (columns * tile_w + (columns + 1) * 2, title_h + rows * (tile_h + label_h + 2) + 2), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((4, 2), title, fill=(255, 255, 255), font=font)
    for position, ((index, _), image) in enumerate(zip(tiles, images)):
        x = 2 + (position % columns) * (tile_w + 2)
        y = title_h + (position // columns) * (tile_h + label_h + 2)
        sheet.paste(image, (x, y + label_h))
        draw.text((x + 2, y + 1), f'#{index}  {_timecode(index, fps)}', fill=(255, 220, 64), font=font)
    return sheet


def _save_under(sheet, path: Path, max_kb: int) -> int:
    from PIL import Image

    image = sheet
    while True:
        for quality in (82, 70, 58, 46, 36):
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=quality, optimize=True)
            if buffer.tell() <= max_kb * 1024:
                path.write_bytes(buffer.getvalue())
                return buffer.tell()
        if image.width < 320:
            path.write_bytes(buffer.getvalue())
            return buffer.tell()
        image = image.resize((int(image.width * 0.85), int(image.height * 0.85)), Image.LANCZOS)


def sheet_command(paths: list[str], *, out: str | None, every: int | None, rate: float | None, indices: list[int] | None,
                  seconds: list[float] | None, count: int | None, width: int, columns: int, max_kb: int) -> dict:
    pages = []
    for path in paths:
        info = probe(path)
        if not info['video'] or info['video']['still']:
            raise RnError(f'{path}: not a video')
        fps = source_fps(info)
        frames = info['video']['frames']
        chosen = pick_indices(frames, fps, every=every, rate=rate, indices=indices, seconds=seconds, count=count)
        if not chosen:
            raise RnError(f'{path}: no frames in range (the clip has {frames} frames)')
        stem = Path(path).stem
        base = Path(out) if out else Path('sheets') / f'{stem}.jpg'
        if len(paths) > 1 and out:
            base = Path(out).with_name(f'{Path(out).stem}-{stem}{Path(out).suffix or ".jpg"}')
        base.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            tiles = extract(path, chosen, Path(tmp), width=width)
            page_count = math.ceil(len(tiles) / MAX_PER_PAGE)
            for page in range(page_count):
                part = tiles[page * MAX_PER_PAGE:(page + 1) * MAX_PER_PAGE]
                target = base if page_count == 1 else base.with_name(f'{base.stem}-p{page + 1}{base.suffix or ".jpg"}')
                title = f'{Path(path).name}  {fps}fps  {frames}f  frames {part[0][0]}-{part[-1][0]}'
                size = _save_under(_compose(part, fps, columns, title), target, max_kb)
                pages.append({'path': str(target), 'source': path, 'indices': [index for index, _ in part], 'bytes': size})
    return {'pages': pages}
