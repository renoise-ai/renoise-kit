"""v0.11.0: rn export -o x.mmkproj, the edit as a MultiMediaKit project (needs mmk: RN_MMK or on PATH)."""
import json
import os
import shutil
import subprocess

import pytest

from conftest import ffmpeg, ramp_index, write_edl
from rn.cli import main
from rn.edl import Clip
from rn.ff import probe
from rn.mmk import _overlay_lanes, mmk_binary

needs_mmk = pytest.mark.skipif(not (os.environ.get('RN_MMK') or shutil.which('mmk')), reason='mmk is not installed')
CUT = {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
    {'id': 'a', 'src': 'ramp.mp4', 'in': 0.25, 'out': 1.0},
    {'id': 'b', 'src': 'ramp.mp4', 'in': 0.5, 'out': 2.0, 'speed': 2, 'transition': {'type': 'fade', 'duration': 0.25}},
    {'id': 'c', 'src': 'ramp.mp4', 'in': {'frame': 40}, 'duration': 0.5}]}]}


def rn(*args) -> int:
    return main([str(a) for a in args])


def mp4_frame(video, frame: int, out) -> int:
    ffmpeg('-i', str(video), '-vf', f'select=eq(n\\,{frame})', '-frames:v', '1', str(out))
    return ramp_index(out)


@needs_mmk
def test_mmk_project_shows_the_same_source_frames_as_assemble(media, tmp_path):
    edl = write_edl(media, CUT, 'mmk_cut.json')
    package, report = tmp_path / 'cut.mmkproj', tmp_path / 'r.json'
    assert rn('export', edl, '-o', package, '--json', report) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert result['dropped'] == [] and result['revision'] == 1 and 'cross-dissolve@1' in result['requires']
    assembled, exported = tmp_path / 'cut.mp4', tmp_path / 'mmk.mp4'
    assert rn('assemble', edl, '-o', assembled) == 0
    # mmk export, not mmk render: render's PNG goes through the sRGB display transform, the export stays BT.709 like rn's
    subprocess.run([mmk_binary(), 'export', str(package), str(exported)], check=True, capture_output=True)
    assert probe(exported)['video']['frames'] == probe(assembled)['video']['frames'] == 42
    # Outside the transition (frames 12-17): a from source 6, b at 2x from 12, c from 40
    for frame, source in ((3, 9), (10, 16), (20, 28), (26, 40), (31, 41), (40, 50)):
        assert mp4_frame(exported, frame, tmp_path / f'm{frame}.png') == source
        assert mp4_frame(assembled, frame, tmp_path / f'r{frame}.png') == source
    otio = json.loads((package / 'timeline.otio').read_text(encoding='utf-8'))
    main_track = otio['tracks']['children'][0]['children']
    assert [c['metadata']['mmk']['id'] for c in main_track if c['OTIO_SCHEMA'].startswith('Clip')] == ['a', 'b', 'c']
    assert any(c['OTIO_SCHEMA'].startswith('Transition') for c in main_track)


@needs_mmk
def test_mmk_lists_what_it_leaves_out(media, tmp_path):
    (media / 'mmk.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nhi\n', encoding='utf-8')
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [
        {'clips': [{'id': 'a', 'src': 'ramp.mp4', 'out': 1, 'vf': 'hue=s=0'},
                   {'id': 'b', 'color': '#ff0000', 'duration': 1, 'transition': {'type': 'wipeleft', 'duration': 0.25}},
                   {'id': 'c', 'src': 'ramp.mp4', 'out': 1, 'transition': {'type': 'fade', 'duration': 0.25}}]},
        {'clips': [{'id': 'o', 'image': 'red.png', 'at': 0.25, 'duration': 1, 'style': {'left': 10, 'opacity': 0.5}}]}],
        'audio': [{'id': 'm', 'src': 'music.wav', 'gain_db': -6, 'fade_out': 0.5}], 'subtitles': {'file': 'mmk.srt'}}, 'mmk_drop.json')
    package, report = tmp_path / 'drop.mmkproj', tmp_path / 'r.json'
    assert rn('export', edl, '-o', package, '--json', report) == 0
    dropped = {d['item']: d['dropped'] for d in json.loads(report.read_text(encoding='utf-8'))['dropped']}
    # c's dissolve has nothing to dissolve from once b is left empty
    assert dropped == {'a': ['vf'], 'b': ['transition wipeleft (a hard cut)', 'color clip (left empty)'],
                       'c': ['transition fade (a hard cut)'], 'subtitles': ['mmk.srt']}
    assert subprocess.run([mmk_binary(), 'validate', str(package)], capture_output=True).returncode == 0
    # Written again in place: edit.json is the source, the project follows it
    assert rn('export', edl, '-o', package) == 0


def test_mmk_will_not_replace_another_folder(media, tmp_path):
    edl = write_edl(media, CUT, 'mmk_cut.json')
    (tmp_path / 'other.mmkproj').mkdir()
    (tmp_path / 'other.mmkproj' / 'notes.txt').write_text('mine', encoding='utf-8')
    assert rn('export', edl, '-o', tmp_path / 'other.mmkproj') != 0
    assert (tmp_path / 'other.mmkproj' / 'notes.txt').exists()


def test_overlays_keep_their_stacking_order():
    def layer(start, frames):
        return Clip(where='', kind='image', source=None, color=None, start_s=0, speed=1, frames=frames, fit='cover',
                    audio=False, gain_db=0, timeline_start=start)

    # Third overlaps the first two: it must sit above both, even though it starts first
    first, second, third, fourth = layer(10, 10), layer(30, 10), layer(0, 50), layer(60, 5)
    lanes = _overlay_lanes([first, second, third, fourth])
    lane_of = {id(clip): n for n, lane in enumerate(lanes) for _, clip in lane}
    assert lane_of[id(first)] == lane_of[id(second)] == lane_of[id(fourth)] == 0 and lane_of[id(third)] == 1
