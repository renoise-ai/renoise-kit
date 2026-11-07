"""v0.7.0: rn key, a backdrop matte (solid colour measured on the border, or white by flood fill) without a model."""
import json

import numpy as np
from conftest import ffmpeg
from PIL import Image

from rn.cli import main
from rn.key import border_connected, key_command

GREEN = (0, 177, 64)
SKIN = (205, 150, 120)


def green_clip(tmp_path, frames=8):
    """A green screen with a darker crease band and a skin-coloured square moving right; a white patch on the square."""
    folder = tmp_path / 'src'
    folder.mkdir()
    for n in range(frames):
        rgb = np.zeros((120, 160, 3), np.uint8) + np.uint8(GREEN)
        rgb[:, 120:130] = (0, 140, 50)  # shading on the backdrop: still backdrop
        x = 20 + n * 8
        rgb[40:90, x:x + 40] = SKIN
        rgb[55:65, x + 10:x + 30] = (250, 250, 250)
        Image.fromarray(rgb).save(folder / f'{n:03d}.png')
    clip = tmp_path / 'green.mp4'
    ffmpeg('-framerate', '10', '-i', str(folder / '%03d.png'), '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', str(clip))
    return clip


def test_green_screen_matte(tmp_path):
    result = key_command(str(green_clip(tmp_path)), out=str(tmp_path / 'm'))
    assert result['frames'] == 8 and result['mode'] == 'colour'
    measured = np.array([int(result['backdrop']['color'][i:i + 2], 16) for i in (1, 3, 5)])
    assert np.abs(measured - GREEN).max() <= 6
    assert result['backdrop']['border_uniform'] > 0.9
    matte = np.asarray(Image.open(tmp_path / 'm' / '00004.png'))  # frame 3: square at x 44..84
    assert matte[65, 64] == 255 and matte[60, 60] == 255      # skin and the white patch on it are subject
    assert matte[10, 10] == 0 and matte[100, 125] == 0         # backdrop, its shaded band too
    assert abs(result['coverage']['mean'] - 50 * 40 / (120 * 160)) < 0.02
    assert result['edl'] == {'mask': {'frames': 'm/%05d.png', 'fps': '10', 'mode': 'luma'}}


def test_cutout_despill(tmp_path):
    clip = green_clip(tmp_path)
    plain = key_command(str(clip), out=str(tmp_path / 'a'), cutout=True)
    spilled = key_command(str(clip), out=str(tmp_path / 'b'), cutout=True, despill_edges=True)
    assert plain['edl'] == {'frames': 'a/%05d.png', 'fps': '10'} and spilled['despill']
    a = np.asarray(Image.open(tmp_path / 'a' / '00001.png'))
    b = np.asarray(Image.open(tmp_path / 'b' / '00001.png'))
    assert a.shape[-1] == 4 and a[10, 10, 3] == 0 and a[65, 40, 3] == 255
    assert (b[..., 1] <= np.maximum(b[..., 0], b[..., 2])).all()  # no pixel greener than its red or blue
    assert (a[..., 1] > np.maximum(a[..., 0], a[..., 2])).any()


def test_white_backdrop_keeps_enclosed_white(tmp_path):
    rgb = np.full((100, 140, 3), 252, np.uint8)
    rgb[20:80, 30:110] = (40, 60, 200)        # a product
    rgb[40:60, 50:90] = (255, 255, 255)       # a white label inside it
    rgb[0:30, 0:4] = (255, 255, 255)
    still = tmp_path / 'p.png'
    Image.fromarray(rgb).save(still)
    result = key_command(str(still), out=str(tmp_path / 'k'), white=True, softness=0)
    matte = np.asarray(Image.open(tmp_path / 'k' / '00001.png'))
    assert matte[50, 70] == 255 and matte[25, 35] == 255   # the label stays: not connected to the border
    assert matte[5, 5] == 0 and matte[90, 130] == 0
    assert result['edl'] == {'mask': {'image': 'k/00001.png', 'mode': 'luma'}}
    assert result['backdrop']['border_uniform'] > 0.95


def test_border_connected_spiral():
    """Reach through turns: a corridor that runs right, down, left and up again from the border."""
    cand = np.zeros((9, 9), bool)
    cand[1, 0:8] = True; cand[1:8, 7] = True; cand[7, 2:8] = True; cand[3:8, 2] = True; cand[3, 2:5] = True
    cand[5, 5] = True  # isolated
    reached = border_connected(cand)
    assert reached[3, 4] and reached[7, 2] and not reached[5, 5]


def test_cli_and_errors(tmp_path, capsys):
    clip = green_clip(tmp_path, frames=3)
    assert main(['key', str(clip), '-o', str(tmp_path / 'c'), '--json', str(tmp_path / 'k.json')]) == 0
    out = capsys.readouterr().out
    assert 'backdrop #' in out and 'next: EDL overlay "mask"' in out and f'proof: {tmp_path / "c.proof.jpg"}' in out
    assert (tmp_path / 'c.proof.jpg').stat().st_size <= 125 * 1024
    assert json.loads((tmp_path / 'k.json').read_text())['frames'] == 3
    assert main(['key', str(clip), '-o', str(tmp_path / 'd'), '--white', '--color', '#00ff00']) == 1
    assert main(['key', str(clip), '-o', str(tmp_path / 'e'), '--color', 'green']) == 1
