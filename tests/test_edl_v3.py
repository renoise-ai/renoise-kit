"""EDL additions in template v0.3.0: image sequences, timeline anchors, shot positions, keyframes (spec §3.1 / §4)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import ffmpeg, write_edl
from rn.assemble import assemble_command
from rn.cuts import detect
from rn.edl import load
from rn.ff import RnError
from rn.frames import extract


def frame_rgb(video: Path, index: int, tmp: Path) -> Image.Image:
    out = tmp / f'grab{index}'
    out.mkdir(exist_ok=True)
    (_, png), = extract(video, [index], out)
    return Image.open(png).convert('RGB')


def red_box(image: Image.Image) -> tuple[int, int] | None:
    """Left/right x of the strongly red columns along the middle row of the overlay band."""
    xs = [x for x in range(image.width) if (lambda p: p[0] > 180 and p[1] < 80 and p[2] < 80)(image.getpixel((x, 30)))]
    return (xs[0], xs[-1]) if xs else None


@pytest.fixture(scope='module')
def two_shots(media, tmp_path_factory) -> Path:
    """Bars for 2 s then a test pattern for 2 s, same size and rate: one hard cut at frame 60."""
    root = tmp_path_factory.mktemp('shots')
    ffmpeg('-i', str(media / 'bars.mp4'), '-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=30:d=2', '-filter_complex',
           '[0:v]fps=30,format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]concat=n=2:v=1:a=0[v]', '-map', '[v]',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(root / 'two.mp4'))
    return root / 'two.mp4'


def test_cuts_finds_the_hard_cut(two_shots):
    result = detect(str(two_shots))
    assert [c['frame'] for c in result['cuts']] == [60]
    assert [(s['start_frame'], s['end_frame']) for s in result['shots']] == [(0, 60), (60, 120)]


def test_shot_positions_resolve_inside_the_source(two_shots, tmp_path):
    shutil.copyfile(two_shots, tmp_path / 'two.mp4')
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 180, 'fps': 30},
                               'tracks': [{'clips': [{'src': 'two.mp4', 'in': {'shot': 1}, 'out': {'shot': 1, 'offset': -0.5}},
                                                     {'src': 'two.mp4', 'in': {'shot': 0, 'offset': 0.5}, 'out': {'shot': 0}}]}]})
    edit = load(edl)
    assert edit.base[0].start_s == pytest.approx(2.0)
    assert edit.base[0].frames == 45 and edit.base[1].frames == 45
    with pytest.raises(RnError, match='shot 2 does not exist'):
        load(write_edl(tmp_path, {'output': {'width': 320, 'height': 180, 'fps': 30},
                                  'tracks': [{'clips': [{'src': 'two.mp4', 'in': {'shot': 2}}]}]}, 'bad.json'))


def test_timeline_anchors(media, tmp_path):
    (tmp_path / 'listen.json').write_text(json.dumps({'beats': {'times': [0.5, 1.25]}, 'words': [{'word': 'hi', 'start': 0.4, 'end': 0.9}]}), encoding='utf-8')
    shutil.copyfile(media / 'red.png', tmp_path / 'red.png')
    shutil.copyfile(media / 'bars.mp4', tmp_path / 'bars.mp4')
    edl = write_edl(tmp_path, {
        'output': {'width': 320, 'height': 240, 'fps': 30},
        'markers': {'drop': 1.0},
        'tracks': [
            {'clips': [{'color': '#000000', 'duration': 1.0},
                       {'src': 'bars.mp4', 'in': 0, 'out': 1.5, 'transition': {'type': 'fade', 'duration': 0.2}}]},
            {'clips': [{'image': 'red.png', 'at': {'clip': 1}, 'until': {'clip_end': 1, 'offset': -0.1}},
                       {'image': 'red.png', 'at': {'marker': 'drop', 'offset': 0.5}, 'duration': 0.2},
                       {'image': 'red.png', 'at': {'beat': 1, 'listen': 'listen.json'}, 'duration': 0.2},
                       {'image': 'red.png', 'at': {'word': 0, 'listen': 'listen.json', 'edge': 'end'}, 'duration': 0.2}]}],
    })
    edit = load(edl)
    second = edit.base[1]
    assert second.timeline_start == 24  # 30 frames minus the 6-frame fade
    title, marker, beat, word = edit.overlays
    assert title.timeline_start == 24 and title.frames == 24 + 45 - 3 - 24
    assert (marker.timeline_start, beat.timeline_start, word.timeline_start) == (45, 38, 27)


@pytest.mark.parametrize('anchor, message', [
    ({'clip': 5}, 'main-track clip index'),
    ({'marker': 'nope'}, 'not in the top-level "markers"'),
    ({'beat': 0}, 'expected a file path'),
    ({'clip': 0, 'marker': 'x'}, 'one of clip'),
])
def test_bad_anchors_explain_themselves(media, tmp_path, anchor, message):
    shutil.copyfile(media / 'red.png', tmp_path / 'red.png')
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30},
                               'tracks': [{'clips': [{'color': 'black', 'duration': 1}]},
                                          {'clips': [{'image': 'red.png', 'at': anchor, 'duration': 0.2}]}]})
    with pytest.raises(RnError, match=message):
        load(edl)


def _write_sequence(directory: Path, count: int, *, alpha: bool) -> None:
    directory.mkdir()
    for k in range(count):
        if alpha:
            image = Image.new('RGBA', (320, 240), (0, 0, 0, 0))
            image.paste((255, 0, 0, 255), (0, 0, 80, 60))
        else:
            image = Image.new('RGB', (320, 240), (k * 20, k * 20, k * 20))
        image.save(directory / f'{k + 1:05d}.png')


def test_sequence_on_the_main_track_keeps_every_frame(tmp_path):
    _write_sequence(tmp_path / 'seq', 10, alpha=False)
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30},
                               'tracks': [{'clips': [{'frames': 'seq/%05d.png'}]}], 'mix': {'lufs': None}})
    out = tmp_path / 'out.mp4'
    assemble_command(str(edl), str(out), dry_run=False, keep=False)
    for k in (0, 4, 9):
        grey = frame_rgb(out, k, tmp_path).getpixel((160, 120))[0]
        assert abs(grey - k * 20) <= 4, (k, grey)


def test_sequence_overlay_keeps_alpha(tmp_path):
    _write_sequence(tmp_path / 'layer', 15, alpha=True)
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30},
                               'tracks': [{'clips': [{'color': '#0000ff', 'duration': 1}]},
                                          {'clips': [{'frames': 'layer/%05d.png', 'at': 0.2}]}]})
    out = tmp_path / 'out.mp4'
    assemble_command(str(edl), str(out), dry_run=False, keep=False)
    image = frame_rgb(out, 10, tmp_path)
    assert image.getpixel((40, 30))[0] > 200  # the opaque square
    assert image.getpixel((200, 150))[2] > 200  # transparent elsewhere: the blue plate shows


def test_sequence_errors(tmp_path):
    (tmp_path / 'seq').mkdir()
    for k in (1, 2, 4):
        Image.new('RGB', (16, 16)).save(tmp_path / 'seq' / f'{k:05d}.png')
    for pattern, message in (('seq/%05d.png', 'gap after'), ('seq/none_%05d.png', 'no files match'), ('seq/x.png', 'numbered pattern')):
        edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30}, 'tracks': [{'clips': [{'frames': pattern}]}]})
        with pytest.raises(RnError, match=message):
            load(edl)


def test_overlay_keys_move_and_fade(media, tmp_path):
    shutil.copyfile(media / 'red.png', tmp_path / 'red.png')
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30},
                               'tracks': [{'clips': [{'color': '#000000', 'duration': 2}]},
                                          {'clips': [{'image': 'red.png', 'duration': 2, 'x': 0, 'y': 0,
                                                      'keys': {'x': [[0, 0], [1, 200]]}}]},
                                          {'clips': [{'image': 'red.png', 'duration': 2, 'x': 0, 'y': 120,
                                                      'keys': {'opacity': [[0, 0], [1, 1, 'out']]}}]}]})
    out = tmp_path / 'out.mp4'
    assemble_command(str(edl), str(out), dry_run=False, keep=False)
    start, moved, held = (frame_rgb(out, k, tmp_path) for k in (0, 30, 50))
    assert red_box(start)[0] <= 2 and abs(red_box(moved)[0] - 200) <= 3 and abs(red_box(held)[0] - 200) <= 3
    assert start.getpixel((40, 150))[0] < 40  # opacity 0 at t=0
    assert moved.getpixel((40, 150))[0] > 200  # opacity 1 from t=1


def test_main_track_keys_zoom_the_plate(media, tmp_path):
    shutil.copyfile(media / 'bars.mp4', tmp_path / 'bars.mp4')
    edl = write_edl(tmp_path, {'output': {'width': 640, 'height': 360, 'fps': 30},
                               'tracks': [{'clips': [{'src': 'bars.mp4', 'keys': {'scale': [[0, 1], [1, 2, 'inout']]}}]}],
                               'mix': {'lufs': None}})
    out = tmp_path / 'out.mp4'
    assemble_command(str(edl), str(out), dry_run=False, keep=False)
    plain = frame_rgb(media / 'bars.mp4', 45, tmp_path).resize((640, 360))
    zoomed = frame_rgb(out, 45, tmp_path)
    first = frame_rgb(out, 0, tmp_path)
    # At 2x the left edge shows what sat a quarter of the way in; frame 0 is untouched.
    assert first.getpixel((5, 60)) == pytest.approx(plain.getpixel((5, 60)), abs=12)
    assert zoomed.getpixel((5, 60)) == pytest.approx(plain.getpixel((160 + 2, 60 // 2 + 90)), abs=20)


@pytest.mark.parametrize('keys, message', [
    ({'opacity': [[0, 1]]}, 'keyable here'),
    ({'scale': [[0, 1], [0, 2]]}, 'times must increase'),
    ({'scale': [[0, 1], [1, 2, 'bounce']]}, 'ease one of'),
    ({'scale': [[0, 0]]}, 'must be > 0'),
])
def test_bad_keys(media, tmp_path, keys, message):
    shutil.copyfile(media / 'bars.mp4', tmp_path / 'bars.mp4')
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 180, 'fps': 30},
                               'tracks': [{'clips': [{'src': 'bars.mp4', 'keys': keys}]}]})
    with pytest.raises(RnError, match=message):
        load(edl)
