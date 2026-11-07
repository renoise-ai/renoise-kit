"""v0.7.0: rn edit EDL hits, one sound effect placed as audio items at every cut, every (Nth) beat or given seconds."""
import json
import re
import subprocess

import pytest
from conftest import ffmpeg, write_edl

from rn.cli import main
from rn.edl import load


def rn(*args) -> int:
    return main([str(a) for a in args])


def three_clips(directory, name='e.json', ids=('a', 'b', 'c'), audio=None, **top):
    """Three 1 s clips at 24 fps; the third dissolves in over 0.25 s, so the main track is 2.75 s and its cut is at 1.75 s."""
    clips = [{'color': 'red', 'duration': 1}, {'color': 'green', 'duration': 1},
             {'color': 'blue', 'duration': 1, 'transition': {'type': 'fade', 'duration': 0.25}}]
    for clip, ident in zip(clips, ids):
        if ident:
            clip['id'] = ident
    edl = {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': clips}], **top}
    if audio is not None:
        edl['audio'] = audio
    return write_edl(directory, edl, name)


@pytest.fixture
def sfx(tmp_path):
    path = tmp_path / 'whoosh.wav'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:duration=0.15', str(path))
    return path


def audio_items(edl):
    return json.loads(edl.read_text(encoding='utf-8')).get('audio', [])


def test_cuts_are_anchored_to_the_clips_they_start(tmp_path, sfx, capsys):
    edl = three_clips(tmp_path)
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts', '--gain', -6, '--offset', -0.1) == 0
    assert audio_items(edl) == [
        {'id': 'sfx1', 'src': 'whoosh.wav', 'role': 'sfx', 'at': {'clip': 'b', 'offset': -0.1}, 'gain_db': -6.0},
        {'id': 'sfx2', 'src': 'whoosh.wav', 'role': 'sfx', 'at': {'clip': 'c', 'offset': -0.1}, 'gain_db': -6.0}]
    out = capsys.readouterr().out
    assert 'added 2 x whoosh.wav at cuts: sfx1..sfx2' in out and 'hits at 0.9, 1.65 s' in out
    edit = load(edl)
    assert [(a.id, a.role, a.gain_db) for a in edit.audio] == [('sfx1', 'sfx', -6.0), ('sfx2', 'sfx', -6.0)]
    assert [a.at for a in edit.audio] == [pytest.approx(0.9), pytest.approx(1.65)]  # c's cut is where its dissolve begins
    # The anchors follow a later edit: a longer first clip moves both hits.
    assert rn('edit', edl, 'set', 'a', 'duration=1.5') == 0
    assert [a.at for a in load(edl).audio] == [pytest.approx(1.4), pytest.approx(2.15)]
    assert rn('assemble', edl, '--dry-run') == 0


def test_clips_without_ids_are_anchored_by_index_and_say_so(tmp_path, sfx, capsys):
    edl = three_clips(tmp_path, ids=(None, None, None))
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts') == 0
    assert [a['at'] for a in audio_items(edl)] == [{'clip': 1}, {'clip': 2}]
    assert 'warning: clips without an id are anchored by index' in capsys.readouterr().out


def test_given_seconds_with_offset_and_pivot(tmp_path, sfx, capsys):
    edl = three_clips(tmp_path)
    assert rn('edit', edl, 'hits', sfx, '--at', '0.5,1.25,2.6', '--offset', 0.25, '--pivot', 0.1, '--id-prefix', 'boom') == 0
    items = audio_items(edl)
    assert [(a['id'], a['at'], a['pivot']) for a in items] == [('boom1', 0.75, 0.1), ('boom2', 1.5, 0.1)]
    assert 'gain_db' not in items[0]
    out = capsys.readouterr().out
    assert 'not placed, at or after the end of the main track (2.750s): 2.6s (2.85s)' in out
    assert [a.at for a in load(edl).audio] == [pytest.approx(0.65), pytest.approx(1.4)]  # the pivot lands on each point


def test_beats_every_nth_from_a_listen_result(tmp_path, sfx):
    edl = three_clips(tmp_path)
    (tmp_path / 'listen').mkdir()
    listen = tmp_path / 'listen' / 'music.json'
    listen.write_text(json.dumps({'beats': {'bpm': 120, 'times': [0.2, 0.7, 1.2, 1.7, 2.2, 2.7, 3.2], 'clear': True}}), encoding='utf-8')
    assert rn('edit', edl, 'hits', sfx, '--at', 'beats', '--listen', listen, '--every', 2) == 0
    items = audio_items(edl)
    assert [a['at'] for a in items] == [{'beat': n, 'listen': 'listen/music.json'} for n in (0, 2, 4)]  # beat 6 (3.2 s) is past the end
    assert [a.at for a in load(edl).audio] == [pytest.approx(0.2), pytest.approx(1.2), pytest.approx(2.2)]
    # Beat times are the analysed file's; the music placed at 0.5 s moves them with --offset.
    assert rn('edit', edl, 'hits', sfx, '--at', 'beats', '--listen', listen, '--every', 3, '--offset', 0.5) == 0
    assert [a['at'] for a in audio_items(edl)[3:]] == [{'beat': 0, 'listen': 'listen/music.json', 'offset': 0.5},
                                                       {'beat': 3, 'listen': 'listen/music.json', 'offset': 0.5}]
    assert [a.at for a in load(edl).audio[3:]] == [pytest.approx(0.7), pytest.approx(2.2)]


def test_ids_are_fresh_against_every_id_in_the_edit(tmp_path, sfx):
    edl = three_clips(tmp_path, ids=('a', 'sfx2', None), audio=[{'id': 'sfx1', 'src': str(sfx), 'at': 0.1}])
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts') == 0
    assert rn('edit', edl, 'hits', sfx, '--at', '0.3') == 0
    ids = [a['id'] for a in audio_items(edl)]
    assert ids == ['sfx1', 'sfx3', 'sfx4', 'sfx5'] and len(set(ids + ['a', 'sfx2'])) == 6
    assert audio_items(edl)[1]['at'] == {'clip': 'sfx2'} and audio_items(edl)[2]['at'] == {'clip': 2}


def test_paths_are_written_relative_to_the_edit(tmp_path, sfx, monkeypatch):
    (tmp_path / 'edit').mkdir()
    edl = three_clips(tmp_path / 'edit')
    monkeypatch.chdir(tmp_path)
    assert rn('edit', 'edit/e.json', 'hits', 'whoosh.wav', '--at', '1') == 0
    assert audio_items(edl)[0]['src'] == '../whoosh.wav'
    load(edl)


def test_bad_requests_write_nothing(tmp_path, sfx, capsys):
    edl = three_clips(tmp_path)
    before = edl.read_text(encoding='utf-8')
    assert rn('edit', edl, 'hits', sfx, '--at', 'beats') == 1
    assert '--at beats needs --listen' in capsys.readouterr().err
    assert rn('edit', edl, 'hits', sfx, '--at', '0.1', '--offset', -0.3) == 1
    assert 'no hit lands inside the main track' in capsys.readouterr().err
    assert rn('edit', edl, 'hits', sfx, '--at', 'downbeats') == 1
    assert rn('edit', edl, 'hits', tmp_path / 'missing.wav', '--at', 'cuts') == 1
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts', '--pivot', 2) == 1  # the sound is 0.15 s long
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts', '--id-prefix', 'no space') == 1
    (tmp_path / 'bad.json').write_text(json.dumps({'beats': {'times': [0.5, 'one']}}), encoding='utf-8')
    assert rn('edit', edl, 'hits', sfx, '--at', 'beats', '--listen', tmp_path / 'bad.json') == 1
    assert 'beats.times is not a list of seconds' in capsys.readouterr().err
    single = write_edl(tmp_path, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24},
                                  'tracks': [{'clips': [{'color': 'red', 'duration': 1}]}]}, 'single.json')
    assert rn('edit', single, 'hits', sfx, '--at', 'cuts') == 1
    assert edl.read_text(encoding='utf-8') == before
    assert not list(tmp_path.glob('.*.rn-edit.json'))


def test_hits_inside_a_batch(tmp_path, sfx, capsys):
    edl = three_clips(tmp_path, ids=(None, None, None))
    assert rn('edit', edl, 'batch', 'ids', f'hits {sfx} --at cuts --offset -0.05 --gain -3') == 0
    assert [a['at'] for a in audio_items(edl)] == [{'clip': 'c2', 'offset': -0.05}, {'clip': 'c3', 'offset': -0.05}]
    assert '2 steps: ids added: c1, c2, c3; added 2 x whoosh.wav at cuts: sfx1..sfx2' in capsys.readouterr().out


def rms_db(video, start: float, seconds: float) -> float:
    log = subprocess.run(['ffmpeg', '-hide_banner', '-ss', str(start), '-t', str(seconds), '-i', str(video), '-map', '0:a:0',
                          '-af', 'astats=measure_perchannel=none', '-f', 'null', '-'], capture_output=True, text=True).stderr
    value = re.findall(r'RMS level dB: (-?inf|-?[\d.]+)', log)[-1]
    return float('-inf') if 'inf' in value else float(value)


def test_the_render_has_a_sound_at_each_cut(tmp_path, sfx):
    edl = three_clips(tmp_path, mix={'lufs': None})
    assert rn('edit', edl, 'hits', sfx, '--at', 'cuts') == 0
    out = tmp_path / 'hits.mp4'
    assert rn('assemble', edl, '-o', out, '--preview') == 0
    assert rms_db(out, 1.02, 0.1) > -30 and rms_db(out, 1.77, 0.1) > -30  # the cuts: 1.0 s and 1.75 s (a sine at 1/8 is -21 dB RMS)
    assert rms_db(out, 0.3, 0.5) < -60 and rms_db(out, 1.3, 0.3) < -60  # nothing between them
