"""v0.8.0: rn shots reads a camera move in phases (a still start before a pan, a push-in that starts late) with a path an
EDL can take as keys, and the opening's numbers (first cut, boundaries in the first two seconds, how long the first frame
holds, how busy it is against the rest, when sound starts)."""
import json
import math
import subprocess

import numpy as np
import pytest

from conftest import ffmpeg
from rn.cli import main

cv2 = pytest.importorskip('cv2')

W, H, FPS, SECONDS = 480, 270, 24, 4


def rn(*args) -> int:
    return main([str(a) for a in args])


def _scene(seed: int = 7) -> np.ndarray:
    """A textured picture wider and taller than the frame: blurred noise with blocks, so corners track."""
    rng = np.random.default_rng(seed)
    big = (rng.random((H * 3, W * 3, 3)) * 255).astype(np.uint8)
    big = cv2.GaussianBlur(big, (0, 0), 2.0)
    for _ in range(120):
        x, y = int(rng.integers(0, W * 3 - 40)), int(rng.integers(0, H * 3 - 40))
        cv2.rectangle(big, (x, y), (x + 30, y + 20), tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return big


def _film(path, move) -> None:
    """Frames of the scene seen through a camera: move(t) -> (scale, dx, dy), the picture scaled about the frame's centre
    and shifted by dx, dy pixels (the picture's motion), exactly, then encoded."""
    scene = _scene()
    cx, cy = scene.shape[1] / 2, scene.shape[0] / 2
    frames = []
    for i in range(FPS * SECONDS):
        s, dx, dy = move(i / FPS)
        # the frame's centre sits on the scene's centre shifted against the picture's motion
        m = np.array([[s, 0, W / 2 - s * (cx - dx)], [0, s, H / 2 - s * (cy - dy)]], dtype=np.float64)
        frames.append(cv2.warpAffine(scene, m, (W, H), flags=cv2.INTER_LINEAR))
    proc = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
                           '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', str(path)], input=b''.join(f.tobytes() for f in frames))
    assert proc.returncode == 0


def _camera(tmp_path, name, move) -> dict:
    film, report = tmp_path / f'{name}.mp4', tmp_path / f'{name}.json'
    _film(film, move)
    assert rn('shots', film, '--jobs', 1, '--json', report) == 0
    (shot,) = json.loads(report.read_text(encoding='utf-8'))['shots']
    return shot['camera']


def test_a_still_start_then_a_pan_is_two_phases(tmp_path):
    speed = 0.3 * W  # the picture moves left 30% of the frame width a second, from 2 s
    camera = _camera(tmp_path, 'pan', lambda t: (1.0, -speed * max(0.0, t - 2.0), 0.0))
    still, pan = camera['phases']
    assert abs(still['end'] - 2.0) <= 0.3 and pan['end'] == pytest.approx(SECONDS, abs=0.05)
    assert still['pan_widths_per_s'] < 0.01 and abs(still['zoom_per_s']) < 0.005
    assert pan['pan_widths_per_s'] == pytest.approx(0.3, rel=0.15) and abs(abs(pan['pan_dir_deg']) - 180) < 10
    end = camera['path'][-1]
    assert end['t'] == pytest.approx(SECONDS, abs=0.05) and end['x'] == pytest.approx(-0.3 * (SECONDS - 2.0), rel=0.2)
    assert abs(end['y']) < 0.03 and end['scale'] == pytest.approx(1.0, abs=0.01)


def test_a_late_push_in_about_the_centre_is_a_zoom_with_no_pan(tmp_path):
    rate = 0.08  # 8% a second, from 1.5 s
    camera = _camera(tmp_path, 'push', lambda t: (math.exp(rate * max(0.0, t - 1.5)), 0.0, 0.0))
    still, push = camera['phases']
    assert abs(still['end'] - 1.5) <= 0.3 and abs(still['zoom_per_s']) < 0.01
    assert push['zoom_per_s'] == pytest.approx(rate, rel=0.2) and push['pan_widths_per_s'] < 0.01
    assert camera['path'][-1]['scale'] == pytest.approx(math.exp(rate * (SECONDS - 1.5)), rel=0.05)


def test_a_handheld_shake_stays_one_phase(tmp_path):
    camera = _camera(tmp_path, 'shake', lambda t: (1.0, 6 * math.sin(t * 19), 6 * math.cos(t * 23)))
    assert len(camera['phases']) == 1


def test_the_opening_in_numbers(tmp_path):
    """Three shots, cuts at 0.75 s and 1.5 s, silence until a tone at 0.5 s: the opening has its first cut, two boundaries,
    a first frame that holds until the first cut (the first shot is a still), and its sound start."""
    parts = []
    # a still colour, a busy test card, then a colour with a little grain (some motion, much less than the card)
    for k, (seconds, source, vf) in enumerate(((0.75, 'color=c=0x2050a0:r=24', 'null'), (0.75, 'testsrc2=r=24', 'null'),
                                               (2.5, 'color=c=0xa04020:r=24', 'noise=alls=4:allf=t'))):
        part = tmp_path / f'p{k}.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'{source}:s=320x240:d={seconds}', '-vf', vf, '-pix_fmt', 'yuv420p', str(part))
        parts.append(part)
    film = tmp_path / 'film.mp4'
    ffmpeg(*[x for p in parts for x in ('-i', str(p))], '-f', 'lavfi', '-i', 'aevalsrc=0.5*sin(2*PI*440*t)*gte(t\\,0.5):s=44100:d=4',
           '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1[v]', '-map', '[v]', '-map', '3:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-shortest', str(film))
    report = tmp_path / 'opening.json'
    assert rn('shots', film, '--jobs', 1, '--json', report) == 0
    opening = json.loads(report.read_text(encoding='utf-8'))['opening']
    assert opening['first_cut_s'] == pytest.approx(0.75, abs=0.05) and opening['boundaries_in_opening'] == 2
    assert opening['first_frame_holds_s'] == pytest.approx(0.75, abs=0.05)
    assert opening['busy_vs_rest'] > 1  # the opening has the cuts and the moving card
    assert opening['sound_from_s'] == pytest.approx(0.5, abs=0.03)
