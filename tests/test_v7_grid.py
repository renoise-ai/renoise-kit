"""v0.7.0: rn grid, a sheet sliced into tiles (gutters found, or rows/cols given)."""
import numpy as np
from PIL import Image

from rn.cli import main
from rn.grid import grid_command

TILE_W, TILE_H, GUTTER, MARGIN = 100, 80, 12, 20


def sheet(tmp_path, rows=2, cols=3, framed=False):
    rng = np.random.default_rng(1)
    w = 2 * MARGIN + cols * TILE_W + (cols - 1) * GUTTER
    h = 2 * MARGIN + rows * TILE_H + (rows - 1) * GUTTER
    rgb = np.full((h, w, 3), 245, np.uint8)
    for r in range(rows):
        for c in range(cols):
            x, y = MARGIN + c * (TILE_W + GUTTER), MARGIN + r * (TILE_H + GUTTER)
            tile = rng.integers(0, 255, (TILE_H, TILE_W, 3), dtype=np.uint8)
            if framed:
                tile[:8], tile[-8:], tile[:, :8], tile[:, -8:] = 30, 30, 30, 30
            rgb[y:y + TILE_H, x:x + TILE_W] = tile
    path = tmp_path / 'sheet.png'
    Image.fromarray(rgb).save(path)
    return path


def test_auto_gutters(tmp_path):
    result = grid_command(str(sheet(tmp_path)), out=str(tmp_path / 't'))
    assert (result['rows'], result['cols']) == (2, 3) and result['found'] == {'rows': 2, 'cols': 3}
    boxes = {(t['row'], t['col']): t['box'] for t in result['tiles']}
    assert boxes[(1, 1)] == [MARGIN, MARGIN, TILE_W, TILE_H]
    assert boxes[(2, 3)] == [MARGIN + 2 * (TILE_W + GUTTER), MARGIN + TILE_H + GUTTER, TILE_W, TILE_H]
    assert Image.open(tmp_path / 't' / 'r2c3.png').size == (TILE_W, TILE_H)


def test_given_counts_and_trim(tmp_path):
    src = sheet(tmp_path, rows=1, cols=2, framed=True)
    even = grid_command(str(src), out=str(tmp_path / 'e'), rows=1, cols=2)
    assert even['found'] == {} and len(even['tiles']) == 2
    trimmed = grid_command(str(src), out=str(tmp_path / 'f'), trim=True)
    assert trimmed['tiles'][0]['box'] == [MARGIN + 8, MARGIN + 8, TILE_W - 16, TILE_H - 16]  # the dark frame goes too


def test_cli(tmp_path, capsys):
    assert main(['grid', str(sheet(tmp_path)), '-o', str(tmp_path / 'g')]) == 0
    assert '2 rows x 3 cols (rows found from the gutters, cols found from the gutters)' in capsys.readouterr().out
    blank = tmp_path / 'blank.png'
    Image.new('RGB', (50, 50), 'white').save(blank)
    assert main(['grid', str(blank), '-o', str(tmp_path / 'h')]) == 1
