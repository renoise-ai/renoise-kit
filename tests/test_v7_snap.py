"""v0.7.0: rn edit EDL snap, main-track cuts moved onto beats by rolling edits."""
import json

from conftest import write_edl

from rn.cli import main
from rn.edl import load


def rn(*args) -> int:
    return main([str(a) for a in args])


def beats(directory, times, downbeats=None, name='music.json'):
    path = directory / name
    path.write_text(json.dumps({'beats': {'times': times, 'downbeats': downbeats or times[::4], 'clear': True}}), encoding='utf-8')
    return path


def colours(directory, transition=None):
    clips = [{'id': 'a', 'color': 'red', 'duration': 1}, {'id': 'b', 'color': 'green', 'duration': 1},
             {'id': 'c', 'color': 'blue', 'duration': 1, **({'transition': transition} if transition else {})}]
    return write_edl(directory, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': clips}]})


def starts(edl):
    return [c.timeline_start for c in load(edl).base]


def test_cuts_roll_onto_beats_and_the_length_stays(tmp_path, capsys):
    edl = colours(tmp_path)
    music = beats(tmp_path, [0.5, 0.9, 2.1, 2.6])
    assert rn('edit', edl, 'snap', '--listen', music) == 0
    assert starts(edl) == [0, 22, 50] and load(edl).total_frames == 72  # 0.9 s and 2.1 s at 24 fps (rounded to frames)
    out = capsys.readouterr().out
    assert 'snapped 2 cut(s) to beats, 0 kept' in out and 'b: 1.0s -> 0.9s (-2 frames)' in out


def test_video_clips_trim_their_sources(media, tmp_path):
    clip = str(media / 'ramp.mp4')  # 2.5 s at 24 fps
    edl = write_edl(tmp_path, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'id': 'a', 'src': clip, 'out': 1.0}, {'id': 'b', 'src': clip, 'in': 0.5, 'out': 2.0}]}]})
    assert rn('edit', edl, 'snap', '--listen', beats(tmp_path, [1.125])) == 0
    raw = json.loads(edl.read_text())['tracks'][0]['clips']
    assert raw[0]['out'] == 1.125 and raw[1]['in'] == 0.625 and raw[1]['out'] == 2.0
    assert starts(edl) == [0, 27] and load(edl).total_frames == 60


def test_cuts_that_cannot_move_stay_with_a_reason(media, tmp_path, capsys):
    clip = str(media / 'ramp.mp4')
    edl = write_edl(tmp_path, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'id': 'a', 'src': clip, 'in': 1.5}, {'id': 'b', 'src': clip, 'out': 1.0},  # a ends where its source does
        {'id': 'c', 'src': clip, 'out': 1.0}, {'id': 'd', 'color': 'red', 'duration': 1}]}]})
    before = edl.read_text()
    # a|b at 1.0 s: a beat 0.1 s later needs source a does not have; b|c at 2.0 s: a beat 0.1 s earlier needs c to start
    # before its source does; c|d at 3.0 s: the nearest beat is 0.5 s away.
    assert rn('edit', edl, 'snap', '--listen', beats(tmp_path, [1.1, 1.9, 3.5])) == 1
    err = capsys.readouterr().err
    assert 'no cut moved' in err and 'no source to spare' in err and 'before its source begins' in err and 'tolerance' in err
    assert edl.read_text() == before


def test_downbeats_only_offset_and_transitions(tmp_path, capsys):
    edl = colours(tmp_path, transition={'type': 'fade', 'duration': 0.25})
    # The b|c cut is a 6-frame fade starting at 1.75 s: its middle is 1.875 s. Music starts at 0.5 s on the timeline.
    music = beats(tmp_path, [0.0, 0.5, 1.0, 1.5], downbeats=[0.0, 1.45])
    assert rn('edit', edl, 'snap', '--listen', music, '--to', 'downbeats', '--offset', 0.5, '--only', 'c') == 0
    assert starts(edl) == [0, 24, 44]  # the fade's middle moved from 1.875 s to 1.95 s (+2 frames)
    assert 'c: 1.875s -> 1.95s (+2 frames)' in capsys.readouterr().out


def test_snap_inside_a_batch(tmp_path):
    edl = colours(tmp_path)
    music = beats(tmp_path, [0.9, 2.1])
    assert rn('edit', edl, 'batch', f'snap --listen {music.name} --only b', 'set c meta.snapped=true') == 0
    assert starts(edl) == [0, 22, 48]


def test_a_listen_result_without_beats_is_refused(tmp_path):
    edl = colours(tmp_path)
    (tmp_path / 'empty.json').write_text('{"beats": {"times": []}}')
    assert rn('edit', edl, 'snap', '--listen', tmp_path / 'empty.json') == 1
