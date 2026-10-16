"""v0.5.0 batch 3, compositing: vf/af, blend modes, masks, gain keys."""
import re
import subprocess

from PIL import Image

from conftest import write_edl
from rn.cli import main

EDL = {'output': {'width': 320, 'height': 240, 'fps': 24}, 'mix': {'lufs': None}}


def rn(*args) -> int:
    return main([str(a) for a in args])


def pixel(video, index, xy, tmp_path):
    out = tmp_path / f'p{index}-{xy[0]}-{xy[1]}.png'
    assert rn('frame', video, '--index', index, '-o', out) == 0
    return Image.open(out).convert('RGB').getpixel(xy)


def render(media, tmp_path, name, overlay=None, base=None, audio=None):
    tracks = [{'clips': [base or {'src': 'bars.mp4', 'out': 1}]}]
    if overlay:
        tracks.append({'clips': [overlay]})
    edl = write_edl(media, {**EDL, 'tracks': tracks, **({'audio': audio} if audio else {})}, f'{name}.json')
    out = tmp_path / f'{name}.mp4'
    assert rn('assemble', edl, '-o', out, '--no-cache') == 0
    return out


def rms_db(video, start, seconds):
    log = subprocess.run(['ffmpeg', '-hide_banner', '-ss', str(start), '-t', str(seconds), '-i', str(video), '-map', '0:a:0',
                          '-af', 'astats=measure_perchannel=none', '-f', 'null', '-'], capture_output=True, text=True).stderr
    value = re.findall(r'RMS level dB: (-?inf|-?[\d.]+)', log)[-1]
    return float('-inf') if 'inf' in value else float(value)


def close(a, b, tolerance=4):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def test_vf_runs_inside_the_clip(media, tmp_path):
    plain = render(media, tmp_path, 'plain')
    negated = render(media, tmp_path, 'negated', base={'src': 'bars.mp4', 'out': 1, 'vf': 'negate'})
    a, b = pixel(plain, 5, (40, 60), tmp_path), pixel(negated, 5, (40, 60), tmp_path)
    assert close(b, tuple(255 - v for v in a), 12)


def test_blend_modes_in_rgb(media, tmp_path):
    grey = {'color': '#808080', 'at': 0, 'duration': 1}
    plain = render(media, tmp_path, 'base')
    normal = render(media, tmp_path, 'normal', overlay={**grey, 'blend': 'normal'})
    reference = render(media, tmp_path, 'ref', overlay=grey)
    screen = render(media, tmp_path, 'screen', overlay={**grey, 'blend': 'screen'})
    multiply = render(media, tmp_path, 'multiply', overlay={**grey, 'blend': 'multiply'})
    xy = (60, 60)
    assert close(pixel(normal, 5, xy, tmp_path), pixel(reference, 5, xy, tmp_path))
    under = pixel(plain, 5, xy, tmp_path)
    expected_screen = tuple(255 - (255 - v) * (255 - 128) // 255 for v in under)
    expected_multiply = tuple(v * 128 // 255 for v in under)
    assert close(pixel(screen, 5, xy, tmp_path), expected_screen, 8)
    assert close(pixel(multiply, 5, xy, tmp_path), expected_multiply, 8)


def test_mask_keeps_the_white_side(media, tmp_path):
    matte = Image.new('L', (320, 240), 0)
    matte.paste(255, (0, 0, 160, 240))
    matte.save(media / 'half.png')
    red = {'color': '#ff0000', 'at': 0, 'duration': 1}
    kept = render(media, tmp_path, 'masked', overlay={**red, 'mask': {'image': 'half.png'}})
    flipped = render(media, tmp_path, 'inverted', overlay={**red, 'mask': {'image': 'half.png', 'invert': True}, 'blend': 'normal'})
    left, right = (40, 120), (280, 120)
    assert close(pixel(kept, 5, left, tmp_path), (255, 0, 0), 30) and not close(pixel(kept, 5, right, tmp_path), (255, 0, 0), 60)
    assert close(pixel(flipped, 5, right, tmp_path), (255, 0, 0), 30) and not close(pixel(flipped, 5, left, tmp_path), (255, 0, 0), 60)


def test_gain_keys_and_af(media, tmp_path):
    black = {'color': 'black', 'duration': 2}
    ramp = render(media, tmp_path, 'gainkeys', base=black, audio=[{'src': 'music.wav', 'keys': {'gain_db': [[0, 0], [1.5, -40]]}}])
    assert rms_db(ramp, 1.6, 0.3) < rms_db(ramp, 0.1, 0.3) - 30
    quiet = render(media, tmp_path, 'af', base=black, audio=[{'src': 'music.wav', 'af': 'volume=-20dB'}])
    loud = render(media, tmp_path, 'noaf', base=black, audio=[{'src': 'music.wav'}])
    assert abs((rms_db(loud, 0.5, 1) - rms_db(quiet, 0.5, 1)) - 20) < 1.5
