"""v0.5.0 batch 1, data and execution: ids and meta, anchors by id, rn edit, the slot cache, nested edits, placeholders."""
import json
import shutil

import pytest

from conftest import ramp_index, write_edl
from rn.cli import main
from rn.edl import load
from rn.ff import RnError, probe


def rn(*args) -> int:
    return main([str(a) for a in args])


def base_edl(media, name='e.json', **extra):
    return write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'id': 'a', 'src': 'ramp.mp4', 'in': 0, 'out': 1},
        {'id': 'b', 'src': 'ramp.mp4', 'in': {'frame': 30}, 'out': 2},
    ]}], **extra}, name)


def frame_index(video, index, tmp_path) -> int:
    out = tmp_path / f'f{index}.png'
    assert rn('frame', video, '--index', index, '-o', out) == 0
    return ramp_index(out)


def test_ids_meta_and_anchor_by_id(media):
    edl = base_edl(media, 'ids.json')
    raw = json.loads(edl.read_text(encoding='utf-8'))
    raw['tracks'][0]['clips'][1]['meta'] = {'prompt': 'anything', 'take': 3}
    raw['tracks'].append({'id': 'titles', 'clips': [{'id': 't', 'color': 'white', 'w': 20, 'h': 20, 'at': {'clip': 'b', 'offset': 0.25},
                                                     'until': {'clip_end': 'b'}}]})
    raw['audio'] = [{'id': 'hit', 'src': 'voice.wav', 'at': {'clip': 't'}}]
    edl.write_text(json.dumps(raw), encoding='utf-8')
    edit = load(edl)
    assert edit.base[1].meta == {'prompt': 'anything', 'take': 3}
    assert (edit.overlays[0].timeline_start, edit.overlays[0].frames) == (24 + 6, 12)
    assert edit.audio[0].at == pytest.approx(30 / 24)
    raw['audio'][0]['id'] = 'a'
    edl.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(RnError, match='used twice'):
        load(edl)
    raw['audio'][0]['id'] = 'hit'
    raw['tracks'][1]['clips'][0]['at'] = {'clip': 'nope'}
    edl.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(RnError, match='no clip with id "nope"'):
        load(edl)


def test_edit_split_keeps_every_frame(media, tmp_path):
    edl = base_edl(media, 'split.json')
    whole = tmp_path / 'whole.mp4'
    assert rn('assemble', edl, '-o', whole, '--no-cache') == 0
    assert rn('edit', edl, 'split', 'a', '--at', 0.5) == 0
    clips = json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips']
    assert [c['id'] for c in clips] == ['a', 'a-2', 'b']
    assert clips[0]['out'] == pytest.approx(0.5) and clips[1]['in'] == pytest.approx(0.5)
    parts = tmp_path / 'parts.mp4'
    assert rn('assemble', edl, '-o', parts, '--no-cache') == 0
    assert probe(parts, exact=True)['video']['frames'] == probe(whole, exact=True)['video']['frames'] == 24 + 18
    for index in (11, 12, 13, 30):
        assert frame_index(parts, index, tmp_path) == frame_index(whole, index, tmp_path)


def test_edit_set_delete_insert_move(media, capsys):
    edl = base_edl(media, 'ops.json')
    before = edl.read_text(encoding='utf-8')
    assert rn('edit', edl, 'set', 'b', 'speed=99') == 1  # invalid: nothing written
    assert edl.read_text(encoding='utf-8') == before
    assert rn('edit', edl, 'set', 'b', 'meta.label=hero shot', 'out=1.5') == 0
    clip = json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips'][1]
    assert clip['meta'] == {'label': 'hero shot'} and clip['out'] == 1.5
    assert rn('edit', edl, 'delete', 'a', '--lift') == 0
    assert json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips'][0] == {'color': '#000000', 'duration': 1.0}
    assert rn('edit', edl, 'delete', '0:0') == 0
    assert load(edl).total_frames == 6  # b: 1.25-1.5 s
    assert rn('edit', edl, 'insert', '--before', 'b', '--clip', '{"id": "c", "color": "red", "duration": 0.5}') == 0
    assert rn('edit', edl, 'move', 'c', '--track', '1') == 0
    raw = json.loads(edl.read_text(encoding='utf-8'))
    assert [c['id'] for c in raw['tracks'][0]['clips']] == ['b'] and raw['tracks'][1]['clips'][0]['id'] == 'c'
    assert rn('edit', edl, 'ids') == 0
    assert 'ids added: none' in capsys.readouterr().out


def test_slot_cache_reuses_unchanged_clips(media, tmp_path, monkeypatch):
    monkeypatch.setenv('RN_CACHE', str(tmp_path / 'cache'))
    edl = base_edl(media, 'cache.json')
    assert rn('assemble', edl, '-o', tmp_path / 'one.mp4') == 0
    slots = sorted((tmp_path / 'cache' / 'slots').iterdir())
    assert len(slots) == 4  # video + audio for each clip
    stamps = {p.name: p.stat().st_mtime_ns for p in slots}
    assert rn('edit', edl, 'set', 'b', 'out=1.5') == 0
    assert rn('assemble', edl, '-o', tmp_path / 'two.mp4') == 0
    after = {p.name: p.stat().st_mtime_ns for p in (tmp_path / 'cache' / 'slots').iterdir()}
    assert len(after) == 6 and all(after[name] == stamp for name, stamp in stamps.items())


def test_nested_edit_plays_as_a_source(media, tmp_path, monkeypatch):
    monkeypatch.setenv('RN_CACHE', str(tmp_path / 'cache'))
    inner = base_edl(media, 'inner.json')
    outer = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'src': 'inner.json', 'in': 0.5}, {'src': 'ramp.mp4', 'in': 0, 'out': 0.5}]}]}, 'outer.json')
    out = tmp_path / 'outer.mp4'
    assert rn('assemble', outer, '-o', out) == 0
    assert probe(out, exact=True)['video']['frames'] == 30 + 12  # inner is 42 frames, from 0.5 s
    assert frame_index(out, 0, tmp_path) == 12  # inner frame 12 is ramp frame 12
    loop = json.loads(inner.read_text(encoding='utf-8'))
    loop['tracks'][0]['clips'].append({'src': 'outer.json'})
    inner.write_text(json.dumps(loop), encoding='utf-8')
    with pytest.raises(RnError, match='includes itself'):
        load(outer)


def test_placeholders_stand_in_for_missing_sources(media, tmp_path, monkeypatch):
    monkeypatch.setenv('RN_CACHE', str(tmp_path / 'cache'))
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'src': 'ramp.mp4', 'out': 1},
        {'id': 'shot3', 'src': 'not-yet.mp4', 'duration': 1.5, 'meta': {'prompt': '一只猫跳上窗台 / a cat on the sill'}},
    ]}]}, 'ph.json')
    assert rn('assemble', edl, '-o', tmp_path / 'x.mp4', '--dry-run') == 1
    out = tmp_path / 'ph.mp4'
    assert rn('assemble', edl, '-o', out, '--placeholders') == 0
    assert probe(out, exact=True)['video']['frames'] == 24 + 36
    cards = list((tmp_path / 'cache' / 'placeholders').glob('*.png'))
    assert len(cards) == 1 and cards[0].stat().st_size > 1000
    shutil.rmtree(tmp_path / 'cache')
