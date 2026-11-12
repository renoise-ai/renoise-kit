"""rn compare --shots flags: where a cut's shot moves unlike its reference (opposite pan or zoom, a much faster or slower
pan, a speed ramp the reference does not have). Rules on numbers first, then one measured pair of real clips."""
import json

import pytest

from conftest import ffmpeg
from rn.cli import main
from rn.compare import shot_flags


def motion(pan=0.0, deg=0.0, zoom=0.0):
    return {'pan_widths_per_s': pan, 'pan_dir_deg': deg, 'zoom_per_s': zoom, 'rigid_fit': 0.9}


def test_same_motion_is_not_flagged():
    assert shot_flags(motion(0.2, 10), motion(0.2, 0), 1.2, 1.1) == []


def test_opposite_pan_is_flagged_across_the_wrap():
    assert any(f.startswith('pan opposite') for f in shot_flags(motion(0.2, 175), motion(0.2, -5)))
    assert not any(f.startswith('pan opposite') for f in shot_flags(motion(0.2, 170), motion(0.2, -170)))


def test_a_pan_too_slow_to_measure_has_no_direction():
    assert shot_flags(motion(0.01, 180), motion(0.2, 0)) == []


def test_pan_speed_far_from_the_reference_is_flagged_both_ways():
    assert any('2.5x' in f for f in shot_flags(motion(0.5, 0), motion(0.2, 0)))
    assert any('0.4x' in f for f in shot_flags(motion(0.08, 0), motion(0.2, 0)))


def test_opposite_zoom_is_flagged_only_when_both_zoom():
    assert any(f.startswith('zoom opposite') for f in shot_flags(motion(zoom=0.05), motion(zoom=-0.04)))
    assert shot_flags(motion(zoom=0.05), motion(zoom=-0.01)) == []


def test_a_speed_ramp_the_reference_lacks_is_flagged():
    assert any(f.startswith('speeds up') for f in shot_flags(motion(), motion(), 3.2, 1.2))
    assert shot_flags(motion(), motion(), 3.2, 2.6) == [], 'the reference ramps too'
    assert shot_flags(motion(), motion(), 1.6, 1.0) == [], 'a mild ramp is normal motion'


@pytest.fixture(scope='module')
def pans(tmp_path_factory):
    """A window panning right across a test pattern for 2 s, and the same clip played backwards (panning left)."""
    root = tmp_path_factory.mktemp('pans')
    right = root / 'right.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=24:d=2', '-vf', "crop=320:180:x='t*120':y=90",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(right))
    ffmpeg('-i', str(right), '-vf', 'reverse', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(root / 'left.mp4'))
    return root


def compare_shots(a, b, tmp_path) -> dict:
    report = tmp_path / 'report.json'
    assert main(['compare', str(a), str(b), '--count', '2', '-o', str(tmp_path / 'c.jpg'), '--shots', '--json', str(report)]) == 0
    return json.loads(report.read_text(encoding='utf-8'))['shots']


def test_a_clip_against_itself_is_not_flagged(pans, tmp_path):
    shots = compare_shots(pans / 'right.mp4', pans / 'right.mp4', tmp_path)
    assert shots['flagged'] == 0


def test_a_clip_against_its_reverse_is_flagged_as_an_opposite_pan(pans, tmp_path):
    shots = compare_shots(pans / 'left.mp4', pans / 'right.mp4', tmp_path)
    assert shots['flagged'] >= 1
    assert any(f.startswith('pan opposite') for p in shots['pairs'] for f in p.get('flags', []))


def test_a_shot_that_speeds_up_halfway_is_flagged(pans, tmp_path):
    """The first second as made, then the rest at four times the speed (a sped-up generation)."""
    soft, ramped = tmp_path / 'soft.mp4', tmp_path / 'ramped.mp4'
    # A blurred pattern: frame differences grow with the speed instead of saturating as on the sharp one
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=24:d=2', '-vf', "boxblur=12:2,crop=320:180:x='t*60':y=90",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(soft))
    ffmpeg('-i', str(soft), '-filter_complex',
           '[0:v]split[a][b];[a]trim=0:1,setpts=PTS-STARTPTS[s];[b]trim=1:2,setpts=(PTS-STARTPTS)/4[f];[s][f]concat=n=2:v=1[v]',
           '-map', '[v]', '-r', '24', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(ramped))
    shots = compare_shots(ramped, soft, tmp_path)
    flags = [f for p in shots['pairs'] for f in p.get('flags', [])]
    assert any(f.startswith('speeds up') for f in flags), flags
