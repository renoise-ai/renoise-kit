"""rn compare --video --mode side | stack | blend | diff, checked from the pictures it writes."""
import json

import pytest
from PIL import Image, ImageStat

from conftest import ffmpeg
from rn.cli import main
from rn.ff import probe


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def clips(tmp_path_factory):
    """160x120 at 24 fps: black for 1 s, white for 1.5 s (so one is shorter), a moving test pattern for 1 s, and the
    same pattern 160x90 (a different shape) for 1 s."""
    root = tmp_path_factory.mktemp('modes')
    for name, source in (('black', 'color=c=black:s=160x120:r=24:d=1'), ('white', 'color=c=white:s=160x120:r=24:d=1.5'),
                         ('pattern', 'testsrc2=s=160x120:r=24:d=1'), ('wide', 'testsrc2=s=160x90:r=24:d=1')):
        ffmpeg('-f', 'lavfi', '-i', source, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(root / f'{name}.mp4'))
    return root


def compare(a, b, tmp_path, mode: str | None) -> tuple[dict, str]:
    video, report = tmp_path / f'{mode}.mp4', tmp_path / f'{mode}.json'
    args = ['compare', a, b, '--count', 2, '-o', tmp_path / f'{mode}.jpg', '--video', video, '--json', report]
    assert rn(*args, *(['--mode', mode] if mode else [])) == 0
    return json.loads(report.read_text(encoding='utf-8')), str(video)


def frame_at(video: str, seconds: float, directory) -> Image.Image:
    png = directory / f'{seconds}.png'
    ffmpeg('-ss', str(seconds), '-i', video, '-frames:v', '1', str(png))
    return Image.open(png).convert('RGB')


def test_side_is_unchanged_and_named(clips, tmp_path, capsys):
    """No --mode is today's side by side: same height next to each other, the shorter clip held to the longer's end."""
    result, video = compare(clips / 'black.mp4', clips / 'white.mp4', tmp_path, None)
    assert result['video'] == video and result['video_mode'] == 'side'
    side = probe(video, exact=True)['video']
    assert (side['width'], side['height']) == (320, 120) and side['frames'] == 36
    assert f'side by side: {video}  (A left, B right)' in capsys.readouterr().out
    picture = frame_at(video, 1.25, tmp_path)  # after A ended: A's last (black) frame left, B (white) right
    assert ImageStat.Stat(picture.crop((0, 0, 160, 120))).mean[0] < 10
    assert ImageStat.Stat(picture.crop((160, 0, 320, 120))).mean[0] > 245


def test_stack_puts_a_above_b_at_twice_the_height(clips, tmp_path, capsys):
    result, video = compare(clips / 'black.mp4', clips / 'white.mp4', tmp_path, 'stack')
    assert result['video_mode'] == 'stack'
    stacked = probe(video, exact=True)['video']
    assert (stacked['width'], stacked['height']) == (160, 240) and stacked['frames'] == 36
    assert 'stacked: ' in capsys.readouterr().out
    picture = frame_at(video, 0.5, tmp_path)
    assert ImageStat.Stat(picture.crop((0, 0, 160, 120))).mean[0] < 10
    assert ImageStat.Stat(picture.crop((0, 120, 160, 240))).mean[0] > 245


def test_blend_of_black_and_white_is_mid_grey(clips, tmp_path, capsys):
    result, video = compare(clips / 'black.mp4', clips / 'white.mp4', tmp_path, 'blend')
    assert result['video_mode'] == 'blend'
    blended = probe(video, exact=True)['video']
    assert (blended['width'], blended['height']) == (160, 120) and blended['frames'] == 36
    assert 'blend: ' in capsys.readouterr().out
    for t in (0.5, 1.25):  # and still grey once the shorter black clip holds its last frame
        assert all(abs(channel - 128) < 8 for channel in ImageStat.Stat(frame_at(video, t, tmp_path)).mean)
    other = tmp_path / 'other'
    other.mkdir()
    _, video = compare(clips / 'white.mp4', clips / 'black.mp4', other, 'blend')  # the shorter one is now underneath
    assert probe(video, exact=True)['video']['frames'] == 36


def test_diff_of_a_clip_with_itself_is_black_and_of_black_and_white_is_bright(clips, tmp_path):
    result, video = compare(clips / 'pattern.mp4', clips / 'pattern.mp4', tmp_path, 'diff')
    assert result['video_mode'] == 'diff'
    assert max(ImageStat.Stat(frame_at(video, 0.5, tmp_path)).mean) < 3
    other = tmp_path / 'other'
    other.mkdir()
    _, video = compare(clips / 'black.mp4', clips / 'white.mp4', other, 'diff')
    assert min(ImageStat.Stat(frame_at(video, 0.5, other)).mean) > 245


def test_diff_fits_a_different_shape_inside_a_without_stretching(clips, tmp_path):
    """B is 16:9 against a 4:3 A: it is letterboxed into A's frame, so the bars differ from A (bright) and the output keeps
    A's size."""
    _, video = compare(clips / 'pattern.mp4', clips / 'wide.mp4', tmp_path, 'diff')
    out = probe(video, exact=True)['video']
    assert (out['width'], out['height']) == (160, 120)
    picture = frame_at(video, 0.5, tmp_path)
    assert min(ImageStat.Stat(picture.crop((0, 0, 160, 14))).mean) > 40  # A's top rows against B's black bar


def test_mode_needs_video(clips, tmp_path, capsys):
    assert rn('compare', clips / 'black.mp4', clips / 'white.mp4', '--count', 1, '-o', tmp_path / 'x.jpg', '--mode', 'diff') == 1
    assert '--mode goes with --video' in capsys.readouterr().err
