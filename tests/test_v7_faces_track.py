"""v0.7.0: rn faces --track / --reframe. The tracking and the crop are pure functions, tested without the model."""
import numpy as np
from conftest import ffmpeg
from PIL import Image

from rn.ff import probe
from rn.faces import reframe, render_reframe, track_faces

W, H, FPS = 1920, 1080, 30


def face(x, y, size=200):
    return {'box': [x - size / 2, y - size / 2, size, size], 'score': 0.9}


def samples(n=60, gap=(), other=True):
    """The subject walks right from x 300 to x 1600; a smaller bystander stands at x 1700 (largest = the subject)."""
    out = []
    for f in range(n):
        x = 300 + f * (1300 / (n - 1))
        faces = [] if f in gap else [face(x, 500)]
        if other:
            faces.append(face(1700, 400, size=120))
        out.append({'f': f, 't': f / FPS, 'faces': faces})
    return out


def test_track_picks_follows_fills_and_smooths():
    track = track_faces(samples(gap=range(20, 26)), width=W, height=H, fps=FPS, max_gap=12, smooth=0.1)
    assert all(t['box'] for t in track)
    assert [t['filled'] for t in track[19:27]] == [False, *[True] * 6, False]
    xs = [t['center'][0] for t in track]
    assert all(b > a for a, b in zip(xs, xs[1:]))            # never jumps to the bystander
    assert abs(xs[23] - (300 + 23 * 1300 / 59)) < 3           # the gap is filled on the line
    assert abs(xs[0] - 300) < 1 and abs(xs[-1] - 1600) < 1    # run ends are not dragged by the smoothing


def test_long_gap_is_left_open_and_pick_rules():
    track = track_faces(samples(gap=range(10, 40), other=False), width=W, height=H, fps=FPS, max_gap=12)
    assert track[25]['box'] is None and not track[25]['filled']
    assert track[40]['box'] and not track[40]['filled']  # picked again when the face returns
    right = track_faces(samples(), width=W, height=H, fps=FPS, pick='right', smooth=0)
    assert right[0]['center'] == [1700.0, 400.0]


def test_jittery_detections_are_smoothed():
    rng = np.random.default_rng(0)
    noisy = [{'f': f, 't': f / FPS, 'faces': [face(960 + rng.normal(0, 15), 540)]} for f in range(90)]
    raw = np.std([s['faces'][0]['box'][0] for s in noisy])
    smoothed = np.std([t['box'][0] for t in track_faces(noisy, width=W, height=H, fps=FPS, smooth=0.25)])
    assert smoothed < raw / 3


def test_reframe_keeps_the_face_in_a_clamped_window():
    track = track_faces(samples(), width=W, height=H, fps=FPS)
    plan = reframe(track, width=W, height=H, out_w=1080, out_h=1920, fps=FPS)
    cw, ch = plan['crop']
    assert (cw, ch) == (606, 1080)  # full height, 9:16, even
    for f, x, y in plan['keys']:
        cx = track[f]['center'][0]
        assert 0 <= x <= W - cw and y == 0
        assert x <= cx <= x + cw
        if cw / 2 < cx < W - cw / 2:
            assert abs(x + cw / 2 - cx) < 1                 # centred where the picture allows
    assert plan['keys'][0][0] == 0 and plan['keys'][-1][0] == 59 and len(plan['keys']) == 5  # every 0.5 s + the last
    zoomed = reframe(track, width=W, height=H, out_w=1080, out_h=1920, fps=FPS, zoom=2)
    assert zoomed['crop'] == [302, 540] and all(0 <= y <= H - 540 for _, _, y in zoomed['keys'])


def test_render_follows_a_moving_square(tmp_path):
    """A red square moving right on grey; the rendered 9:16 crop keeps it in the middle."""
    folder = tmp_path / 'png'
    folder.mkdir()
    n, w, h = 20, 640, 360
    fake = []
    for f in range(n):
        rgb = np.full((h, w, 3), 90, np.uint8)
        x = 80 + f * 24
        rgb[150:210, x - 30:x + 30] = (220, 20, 20)
        Image.fromarray(rgb).save(folder / f'{f:03d}.png')
        fake.append({'f': f, 't': f / 10, 'faces': [{'box': [x - 30, 150, 60, 60]}]})
    clip = tmp_path / 'move.mp4'
    ffmpeg('-framerate', '10', '-i', str(folder / '%03d.png'), '-c:v', 'libx264', '-crf', '10', '-pix_fmt', 'yuv420p', str(clip))
    plan = reframe(track_faces(fake, width=w, height=h, fps=10, smooth=0), width=w, height=h, out_w=270, out_h=480, fps=10,
                   key_every=0.1)
    out = tmp_path / 'vertical.mp4'
    assert render_reframe(str(clip), plan, str(out))['frames'] == n
    info = probe(out)['video']
    assert (info['width'], info['height']) == (270, 480)
    frame = tmp_path / 'f10.png'
    ffmpeg('-i', str(out), '-vf', 'select=eq(n\\,10)', '-frames:v', '1', str(frame))
    rgb = np.asarray(Image.open(frame).convert('RGB')).astype(int)
    red = np.nonzero((rgb[..., 0] > 150) & (rgb[..., 1] < 80))
    assert abs(red[1].mean() - 135) < 12  # the square sits in the middle across
