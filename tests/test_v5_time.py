"""v0.5.0 batch 2, time: ranges, speed ramps, pivot, --range, mix.cut_fade."""
import json
import re
import subprocess

import pytest

from conftest import ramp_index, write_edl
from rn.cli import main
from rn.edl import load
from rn.ff import RnError, probe

EDL = {'output': {'width': 320, 'height': 240, 'fps': 24}}


def rn(*args) -> int:
    return main([str(a) for a in args])


def frame_index(video, index, tmp_path) -> int:
    out = tmp_path / f'f{index}.png'
    assert rn('frame', video, '--index', index, '-o', out) == 0
    return ramp_index(out)


def rms_db(video, start: float, seconds: float) -> float:
    log = subprocess.run(['ffmpeg', '-hide_banner', '-ss', str(start), '-t', str(seconds), '-i', str(video), '-map', '0:a:0',
                          '-af', 'astats=measure_perchannel=none', '-f', 'null', '-'], capture_output=True, text=True).stderr
    value = re.findall(r'RMS level dB: (-?inf|-?[\d.]+)', log)[-1]
    return float('-inf') if 'inf' in value else float(value)


def test_ranges_play_spans_back_to_back(media, tmp_path):
    edl = write_edl(media, {**EDL, 'tracks': [
        {'clips': [{'id': 'r', 'src': 'ramp.mp4', 'ranges': [[0, 0.5], [{'frame': 36}, 2.0]]}, {'color': 'black', 'duration': 0.5}]},
        {'clips': [{'color': 'white', 'w': 10, 'h': 10, 'at': {'clip': 'r'}, 'until': {'clip_end': 'r'}}]}]}, 'ranges.json')
    edit = load(edl)
    assert [c.id for c in edit.base] == ['r', 'r.1', None] and [c.frames for c in edit.base] == [12, 12, 12]
    assert edit.overlays[0].frames == 24
    out = tmp_path / 'ranges.mp4'
    assert rn('assemble', edl, '-o', out, '--no-cache') == 0
    assert [frame_index(out, i, tmp_path) for i in (11, 12, 13)] == [11, 36, 37]


def test_speed_ramp_maps_frames(media, tmp_path):
    # 1x for 0.5 s (12 frames, source 0..11), then 3x: the remaining 48 source frames take 16 output frames.
    edl = write_edl(media, {**EDL, 'tracks': [{'clips': [
        {'src': 'ramp.mp4', 'keys': {'speed': [[0, 1], [0.5, 3, 'hold']]}}]}]}, 'ramp.json')
    edit = load(edl)
    assert edit.base[0].frames == 28 and edit.base[0].audio is False
    assert any('speed ramp drops' in w for w in edit.warnings)
    out = tmp_path / 'ramp.mp4'
    assert rn('assemble', edl, '-o', out, '--no-cache') == 0
    assert probe(out, exact=True)['video']['frames'] == 28
    got = [frame_index(out, i, tmp_path) for i in (5, 11, 13, 20)]
    assert got[0] == 5 and got[1] == 11
    assert abs(got[2] - 15) <= 1 and abs(got[3] - 36) <= 1


def test_pivot_lands_a_point_on_the_anchor(media):
    edl = write_edl(media, {**EDL, 'tracks': [{'clips': [{'color': 'black', 'duration': 3}]},
                                              {'clips': [{'color': 'white', 'w': 10, 'h': 10, 'duration': 1, 'at': 1.0, 'pivot': 0.5}]}],
                            'audio': [{'src': 'music.wav', 'at': 2.0, 'pivot': 1.25},
                                      {'src': 'voice.wav', 'in': 0.5, 'at': 0.25, 'pivot': 1.0, 'duration': 1}]}, 'pivot.json')
    edit = load(edl)
    assert edit.overlays[0].timeline_start == 12
    assert edit.audio[0].at == pytest.approx(0.75)
    voice = edit.audio[1]  # its 1.0 s point at 0.25 s: starts 0.25 s early, so 0.25 s is cut from its head
    assert (voice.at, voice.start_s, voice.duration) == (0.0, pytest.approx(0.75), pytest.approx(0.75))
    raw = json.loads(edl.read_text(encoding='utf-8'))
    raw['audio'][1]['pivot'] = 0.2
    edl.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(RnError, match='before "in"'):
        load(edl)


def test_range_renders_part_of_the_timeline(media, tmp_path):
    edl = write_edl(media, {**EDL, 'tracks': [{'clips': [{'src': 'ramp.mp4', 'out': 1}, {'src': 'ramp.mp4', 'in': 1, 'out': 2}]}],
                            'audio': [{'src': 'music.wav', 'role': 'music'}]}, 'range.json')
    full, part = tmp_path / 'full.mp4', tmp_path / 'part.mp4'
    assert rn('assemble', edl, '-o', full) == 0
    assert rn('assemble', edl, '-o', part, '--range', '0.5-1.5') == 0
    assert probe(part, exact=True)['video']['frames'] == 24
    assert [frame_index(part, i, tmp_path) for i in (0, 23)] == [frame_index(full, 12, tmp_path), frame_index(full, 35, tmp_path)]
    assert abs(rms_db(part, 0, 1) - rms_db(full, 0.5, 1)) < 1.0, 'the excerpt keeps the full mix level'


def test_cut_fade_softens_hard_cuts(media, tmp_path):
    def render(fade: float):
        edl = write_edl(media, {**EDL, 'tracks': [{'clips': [{'src': 'bars.mp4', 'out': 1}, {'src': 'bars.mp4', 'in': 0.5, 'out': 1.5}]}],
                                'mix': {'lufs': None, 'cut_fade': fade}}, f'fade{fade}.json')
        out = tmp_path / f'fade{fade}.mp4'
        assert rn('assemble', edl, '-o', out) == 0
        return rms_db(out, 0.98, 0.04)
    assert render(0.03) < render(0) - 6
