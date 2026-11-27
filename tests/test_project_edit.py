"""rn edit X.mmkproj: the operations made on an MMK project itself, one MMK transaction each (needs mmk 0.3.0)."""
import json
import os
import shutil

import pytest

from conftest import write_edl
from rn.cli import main
from rn.mmk import _mmk

needs_mmk = pytest.mark.skipif(not (os.environ.get('RN_MMK') or shutil.which('mmk')), reason='mmk is not installed')
OUT = {'width': 320, 'height': 240, 'fps': 24}
S = 120000  # ticks a second


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture
def project(media, tmp_path, monkeypatch):
    """c1 (ramp 0-1.5) + c2 (bars 0.25-1.25, own sound), overlay o1 at 0.5, music a1: exported, then edited as a project."""
    monkeypatch.chdir(media)
    edl = write_edl(tmp_path, {'output': OUT, 'tracks': [
        {'clips': [{'id': 'c1', 'src': str(media / 'ramp.mp4'), 'out': 1.5},
                   {'id': 'c2', 'src': str(media / 'bars.mp4'), 'in': 0.25, 'out': 1.25}]},
        {'clips': [{'id': 'o1', 'image': str(media / 'red.png'), 'at': 0.5, 'duration': 0.5}]}],
        'audio': [{'id': 'a1', 'src': str(media / 'music.wav'), 'role': 'music'}], 'mix': {'lufs': None}})
    package = tmp_path / 'cut.mmkproj'
    assert rn('export', edl, '-o', package) == 0
    return package


def timeline(package) -> dict:
    """id -> (track, start s, end s, source start s) for every clip and transition."""
    info = _mmk('info', str(package))
    return {i['id']: (t['id'], i['range']['start'] / S, i['range']['end'] / S, (i.get('source_range') or {}).get('start', 0) / S)
            for t in info['tracks'] for i in t['items']}


def revision(package) -> int:
    return _mmk('info', str(package))['revision']


def prop(package, clip_id: str, name: str):
    entry = json.loads((package / 'project.json').read_text())['extensions']['clips'][clip_id]['properties'][name]
    return 'keyed' if 'driver_ref' in entry else entry['base_value']


@needs_mmk
def test_trims_ripple_the_main_track_and_its_sound_only(project):
    before = revision(project)
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    t = timeline(project)
    assert t['c1'][1:3] == (0, 1.0) and t['c2'][1:3] == (1.0, 2.0)
    assert t['c2_audio'][1:3] == (1.0, 2.0)  # the sound follows its picture
    assert t['o1'][1:3] == (0.5, 1.0) and t['a1'][1] == 0  # overlays and music stay
    assert revision(project) == before + 1  # one transaction
    # in= moves the head: the start stays, the out point too
    assert rn('edit', project, 'set', 'c2', 'in=0.5') == 0
    t = timeline(project)
    assert t['c2'][1:4] == (1.0, 1.75, 0.5)


@needs_mmk
def test_speed_keeps_the_source_and_changes_the_length(project):
    assert rn('edit', project, 'set', 'c2', 'speed=2') == 0
    t = timeline(project)
    assert t['c2'][1:3] == (1.5, 2.0)  # 1 s of source at 2x
    item = next(i for tr in _mmk('info', str(project))['tracks'] for i in tr['items'] if i['id'] == 'c2')
    assert item['speed'] == {'num': 2, 'den': 1}


@needs_mmk
def test_split_delete_and_lift(project):
    assert rn('edit', project, 'split', 'c1', '--at', '0.5') == 0
    t = timeline(project)
    assert t['c1'][1:3] == (0, 0.5) and t['c1-2'][1:4] == (0.5, 1.5, 0.5)
    assert rn('edit', project, 'delete', 'c1-2') == 0
    t = timeline(project)
    assert 'c1-2' not in t and t['c2'][1] == 0.5
    assert rn('edit', project, 'delete', 'c1', '--lift') == 0
    assert timeline(project)['c2'][1] == 0.5  # gap kept


@needs_mmk
def test_insert_on_the_main_track_ripples_picture_and_sound(project, media):
    clip = json.dumps({'id': 'n1', 'src': str(media / 'bars.mp4'), 'out': 0.5})
    assert rn('edit', project, 'insert', '--after', 'c1', '--clip', clip) == 0
    t = timeline(project)
    assert t['n1'][1:3] == (1.5, 2.0) and t['c2'][1:3] == (2.0, 3.0) and t['c2_audio'][1:3] == (2.0, 3.0)
    assert t['n1_audio'][1:3] == (1.5, 2.0)
    assert t['o1'][1] == 0.5  # stays
    groups = json.loads((project / 'project.json').read_text())['link_groups']
    assert ['n1', 'n1_audio'] in groups
    locked = [tr['id'] for tr in _mmk('info', str(project))['tracks'] if tr.get('locked')]
    assert locked == []  # the locks around the ripple are lifted again


@needs_mmk
def test_overlay_and_audio_inserts_take_anchors_and_roles(project, media):
    overlay = json.dumps({'id': 'o2', 'image': str(media / 'red.png'), 'at': {'clip': 'c2', 'offset': 0.25}, 'duration': 0.5,
                          'style': {'left': 10, 'top': 20}})
    assert rn('edit', project, 'insert', '--track', '1', '--clip', overlay) == 0
    voice = json.dumps({'id': 'v1', 'src': str(media / 'voice.wav'), 'at': 0.5, 'role': 'voice'})
    assert rn('edit', project, 'insert', '--track', 'audio', '--clip', voice) == 0
    t = timeline(project)
    assert t['o2'][:3] == ('track_V2', 1.75, 2.25)
    assert t['v1'][0] == 'track_voice' and t['v1'][1] == 0.5
    assert prop(project, 'o2', 'transform/position') == [10 + 40, 20 + 30]  # rn's left/top: the image's top-left corner


@needs_mmk
def test_transition_is_centred_and_says_how_much_source_is_missing(project, capsys):
    assert rn('edit', project, 'set', 'c2', 'transition.type=fade', 'transition.duration=0.5') == 0
    t = timeline(project)
    transition = next(v for k, v in t.items() if k.startswith('t_c2') and v[0] == 'track_V1')
    assert transition[1:3] == (1.25, 1.75)  # centred on the cut at 1.5
    assert t['c2'][1] == 1.5  # the cut stays where it was
    capsys.readouterr()
    assert rn('edit', project, 'set', 'c2', 'transition.type=fade', 'transition.duration=1.5') == 1
    assert 's of source to spare around the cut' in capsys.readouterr().err
    assert rn('edit', project, 'set', 'c2', 'transition=none') == 0
    assert not any(k.startswith('t_c2') for k in timeline(project))


@needs_mmk
def test_move_reorders_with_the_sound(project):
    assert rn('edit', project, 'move', 'c2', '--before', 'c1') == 0
    t = timeline(project)
    assert t['c2'][1:3] == (0, 1.0) and t['c1'][1:3] == (1.0, 2.5) and t['c2_audio'][1:3] == (0, 1.0)


@needs_mmk
def test_properties_follow_rn_fields_and_leave_others(project):
    assert rn('edit', project, 'set', 'o1', 'style.opacity=0.5') == 0
    assert prop(project, 'o1', 'opacity') == 0.5
    assert rn('edit', project, 'set', 'o1', 'keys.scale=[[0,1],[0.5,2]]') == 0
    assert prop(project, 'o1', 'transform/scale') == 'keyed'
    assert prop(project, 'o1', 'opacity') == 0.5  # kept: keys do not decide it
    assert rn('edit', project, 'set', 'a1', 'gain_db=-6') == 0
    assert prop(project, 'a1', 'volume') == -6.0


@needs_mmk
def test_batch_is_one_step_and_writes_nothing_on_a_failure(project, capsys):
    before = revision(project)
    assert rn('edit', project, 'batch', 'split c1 --at 0.5', 'set c1-2 out=1.25', 'delete c2') == 0
    assert revision(project) == before + 1
    t = timeline(project)
    assert t['c1-2'][1:3] == (0.5, 1.25) and 'c2' not in t
    assert rn('edit', project, 'batch', 'split c1 --at 0.25', 'delete nope') == 1
    assert 'step 2' in capsys.readouterr().err
    assert revision(project) == before + 1 and 'c1-2' in timeline(project)
    assert not list(project.parent.glob('.*rn-batch'))


@needs_mmk
def test_undo_takes_back_rns_own_steps(project):
    start = timeline(project)
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    assert rn('edit', project, 'split', 'c2', '--at', '1.5') == 0
    assert rn('edit', project, 'undo', '2') == 0
    assert timeline(project) == start


@needs_mmk
def test_show_writes_a_proof_sheet_and_edit_json_is_left_alone(project, tmp_path, capsys):
    source = (project / 'rn' / 'edit.json').read_text()
    out = tmp_path / 'show.jpg'
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    capsys.readouterr()
    assert rn('edit', project, 'show', '-o', out) == 0
    text = capsys.readouterr().out
    assert out.exists() and f'proof: {out}' in text and 'c1 0-1' in text
    assert (project / 'rn' / 'edit.json').read_text() == source


@needs_mmk
def test_errors_are_one_line_and_name_the_fix(project, capsys):
    assert rn('edit', project, 'split', 'c1', '--at', '9') == 1
    assert 'split inside it' in capsys.readouterr().err
    assert rn('edit', project, 'set', 'o1', 'mask.src=red.png') == 1
    assert 'not on an MMK project yet' in capsys.readouterr().err
    assert rn('edit', project, 'split', 'nope', '--at', '1') == 1
    assert 'no clip "nope"' in capsys.readouterr().err


def listen_file(tmp_path, **data):
    path = tmp_path / f'listen-{len(list(tmp_path.glob("listen-*")))}.json'
    path.write_text(json.dumps(data))
    return path


@needs_mmk
def test_an_anchored_overlay_follows_edits_until_someone_moves_it(project, media, capsys):
    overlay = json.dumps({'id': 'o2', 'image': str(media / 'red.png'), 'at': {'clip': 'c2'}, 'duration': 0.25})
    assert rn('edit', project, 'insert', '--track', '2', '--clip', overlay) == 0
    assert timeline(project)['o2'][1] == 1.5
    before = revision(project)
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    assert timeline(project)['o2'][1] == 1.0  # follows c2
    assert revision(project) == before + 2  # the edit, then the follow; undone together
    assert rn('edit', project, 'undo') == 0
    assert timeline(project)['o2'][1] == 1.5 and timeline(project)['c1'][2] == 1.5
    # moved by hand (here: by mmk, as the app would): it keeps its place from then on
    _mmk('edit', str(project), '--request', '-', request={'api_version': 'mmk-edit@1', 'base_revision': revision(project),
         'mode': 'commit', 'operations': [{'op': 'move_clip', 'clip_id': 'o2', 'to_track': 'track_V3', 'to': 2 * S, 'ripple': 'none'}]})
    capsys.readouterr()
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    assert timeline(project)['o2'][1] == 2.0
    assert 'o2 was moved, so it stays where it is' in capsys.readouterr().out


@needs_mmk
def test_hits_at_cuts_land_on_each_cut_and_follow(project, media):
    assert rn('edit', project, 'hits', media / 'voice.wav', '--at', 'cuts', '--pivot', '0.5', '--gain', '-6') == 0
    t = timeline(project)
    assert t['sfx1'][0] == 'track_sfx' and t['sfx1'][1] == 1.0  # its 0.5 s lands on the cut at 1.5
    assert rn('edit', project, 'set', 'c1', 'out=1.25') == 0
    assert timeline(project)['sfx1'][1] == 0.75


@needs_mmk
def test_hits_overlapping_each_other_take_lanes(project, media, tmp_path):
    listen = listen_file(tmp_path, beats={'times': [0.25, 0.5, 0.75]})
    assert rn('edit', project, 'hits', media / 'voice.wav', '--at', 'beats', '--listen', listen) == 0
    t = timeline(project)
    assert [t[f'sfx{n}'][0] for n in (1, 2, 3)] == ['track_sfx', 'track_sfx_2', 'track_sfx_3']


@needs_mmk
def test_silences_cut_a_clip_into_the_pieces_that_stay(project, tmp_path):
    # c1 plays 0..1.5 of its source; a long pause at 0.5..1.0 goes, the pads stay
    listen = listen_file(tmp_path, silences=[{'start': 0.5, 'end': 1.0}])
    assert rn('edit', project, 'silences', 'c1', '--listen', listen, '--min', '0.3', '--pad', '0.125') == 0
    t = timeline(project)
    assert t['c1'][1:4] == (0, 0.625, 0) and t['c1-2'][1:4] == (0.625, 1.25, 0.875)
    assert t['c2'][1] == 1.25 and t['c2_audio'][1] == 1.25


@needs_mmk
def test_snap_rolls_cuts_onto_beats(project, tmp_path):
    listen = listen_file(tmp_path, beats={'times': [0.5, 1.375, 3.0]})
    assert rn('edit', project, 'snap', '--listen', listen, '--tolerance', '0.2') == 0
    t = timeline(project)
    assert t['c1'][2] == 1.375 and t['c2'][1:4] == (1.375, 2.5, 0.125)  # rolled: the total stays


@needs_mmk
def test_marks_put_beats_on_the_clip_and_anchors_use_them(project, media, tmp_path):
    listen = listen_file(tmp_path, beats={'times': [0.5, 1.0, 2.0]})
    assert rn('edit', project, 'marks', '--on', 'a1', '--listen', listen) == 0
    overlay = json.dumps({'id': 'o2', 'image': str(media / 'red.png'), 'at': {'marker': 'beat 1'}, 'duration': 0.25})
    assert rn('edit', project, 'insert', '--track', '2', '--clip', overlay) == 0
    assert timeline(project)['o2'][1] == 1.0


@needs_mmk
def test_init_makes_a_project_of_placeholder_slots_filled_by_set_src(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shots = tmp_path / 'shots.json'
    shots.write_text(json.dumps({'fps': 24, 'frames': 48, 'size': [320, 240], 'shots': [
        {'start_frame': 0, 'end_frame': 23, 'start': 0, 'end': 1.0},
        {'start_frame': 24, 'end_frame': 47, 'start': 1.0, 'end': 2.0}]}))
    package = tmp_path / 'remake.mmkproj'
    assert rn('edit', package, 'init', '--from-shots', shots) == 0
    t = timeline(package)
    assert t['s01'][1:3] == (0, 1.0) and t['s02'][1:3] == (1.0, 2.0)
    assert not (package / 'rn' / 'edit.json').exists()
    assert rn('edit', package, 'set', 's02', f'src={media / "bars.mp4"}') == 0
    t = timeline(package)
    assert t['s02'][1:3] == (1.0, 2.0)
    assets = json.loads((package / 'project.json').read_text())['assets']
    assert any(a['uri'].endswith('bars.mp4') for a in assets.values())


def frame_at(package, seconds, tmp_path):
    png = tmp_path / f'f{seconds}.png'
    import subprocess

    from rn.mmk import mmk_binary
    subprocess.run([mmk_binary(), 'render', str(package), '--time', str(round(seconds * S)), '--out', str(png)], check=True,
                   capture_output=True)
    from PIL import Image
    return Image.open(png).convert('RGB')


@needs_mmk
def test_a_colour_clip_is_an_mmk_scene(project, tmp_path):
    assert rn('edit', project, 'insert', '--after', 'c1', '--clip', json.dumps({'id': 'k1', 'color': '#ff0000', 'duration': 0.5})) == 0
    t = timeline(project)
    assert t['k1'][1:3] == (1.5, 2.0) and t['c2'][1] == 2.0
    r, g, b = frame_at(project, 1.75, tmp_path).getpixel((10, 10))
    assert r > 240 and g < 15 and b < 15


@needs_mmk
def test_vf_is_rendered_into_the_clip_and_unset_puts_the_source_back(project, tmp_path):
    assert rn('edit', project, 'set', 'c2', 'vf=hue=s=0') == 0  # bars in grey
    t = timeline(project)
    assert t['c2'][1:3] == (1.5, 2.5)
    assert list((project / 'rn' / 'baked').glob('c2-*.mp4'))
    r, g, b = frame_at(project, 2.0, tmp_path).getpixel((40, 60))
    assert abs(r - g) < 8 and abs(g - b) < 8
    assert rn('edit', project, 'set', 'c2', '--unset', 'vf') == 0
    assets = json.loads((project / 'project.json').read_text())['assets']
    clip = next(i for tr in _mmk('info', str(project))['tracks'] for i in tr['items'] if i['id'] == 'c2')
    assert assets[clip['asset']]['uri'].endswith('bars.mp4')
    assert timeline(project)['c2'][1:4] == (1.5, 2.5, 0.25)


@needs_mmk
def test_an_xfade_wipe_is_rendered_over_the_cut_and_can_go_again(project, tmp_path):
    assert rn('edit', project, 'set', 'c2', 'transition.type=wipeleft', 'transition.duration=0.5') == 0
    t = timeline(project)
    f = 1 / 24  # a frame of each clip on its own either side
    assert t['c1'][2] == pytest.approx(1.25 - f) and t['x_c2'][1:3] == pytest.approx((1.25 - f, 1.75 + f))
    assert t['c2'][1:4] == pytest.approx((1.75 + f, 2.5, 0.5 + f))
    assert t['c2'][2] == 2.5  # the total stays
    assert rn('edit', project, 'set', 'c2', 'transition=none') == 0
    t = timeline(project)
    assert 'x_c2' not in t and t['c1'][2] == 1.5 and t['c2'][1:4] == (1.5, 2.5, 0.25)


@needs_mmk
def test_duck_lowers_the_music_under_the_voice(project, media):
    voice = json.dumps({'id': 'v1', 'src': str(media / 'voice.wav'), 'at': 0, 'role': 'voice'})
    assert rn('edit', project, 'insert', '--track', 'audio', '--clip', voice) == 0
    assert rn('edit', project, 'set', 'a1', 'gain_db=-3') == 0
    assert rn('edit', project, 'duck', '--depth', '12') == 0
    entry = json.loads((project / 'project.json').read_text())['extensions']['clips']['a1']['properties']['volume']
    drivers = json.loads((project / 'project.json').read_text())['drivers']
    values = [k['value'] for k in drivers[entry['driver_ref']]['keyframes']]
    assert min(values) == pytest.approx(-15, abs=0.01) and max(values) == pytest.approx(-3, abs=0.01)


@needs_mmk
def test_subtitles_become_text_clips(project, tmp_path):
    srt = tmp_path / 'c.srt'
    srt.write_text('1\n00:00:00,500 --> 00:00:01,000\nHello\n\n2\n00:00:01,500 --> 00:00:02,000\nThere\n')
    assert rn('edit', project, 'subtitles', srt) == 0
    t = timeline(project)
    texts = [v for v in t.values() if v[0] == 'sub_track']
    assert [(a, b) for _, a, b, _ in sorted(texts, key=lambda v: v[1])] == [(0.5, 1.0), (1.5, 2.0)]


@needs_mmk
def test_duck_twice_ducks_once(project, media):
    voice = json.dumps({'id': 'v1', 'src': str(media / 'voice.wav'), 'at': 0, 'role': 'voice'})
    assert rn('edit', project, 'insert', '--track', 'audio', '--clip', voice) == 0
    assert rn('edit', project, 'duck') == 0
    assert rn('edit', project, 'duck') == 0
    doc = json.loads((project / 'project.json').read_text())
    keys = doc['drivers'][doc['extensions']['clips']['a1']['properties']['volume']['driver_ref']]['keyframes']
    assert min(k['value'] for k in keys) == pytest.approx(-10, abs=0.01)


@needs_mmk
def test_assemble_and_qa_read_the_project(project, tmp_path, capsys):
    out = tmp_path / 'cut.mp4'
    assert rn('assemble', project, '-o', out) == 0
    assert f'rn qa {out} --edl {project}' in capsys.readouterr().out
    report = tmp_path / 'qa.json'
    assert rn('qa', out, '--edl', project, '--json', report) == 0
    checks = {c['check']: c for c in json.loads(report.read_text())['checks']}
    assert checks['frames']['status'] == 'PASS' and 'expected 60' in checks['frames']['detail']


@needs_mmk
def test_init_in_a_workspace_keeps_its_materials(media, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / 'edit-1'
    (workspace / 'assets').mkdir(parents=True)
    shutil.copyfile(media / 'bars.mp4', workspace / 'assets' / 'material-7.mp4')
    (workspace / '.renoise').mkdir()
    (workspace / '.renoise' / 'state.json').write_text('{}')  # a Renoise workspace (rn export takes it as a project folder)
    shots = tmp_path / 'shots.json'
    shots.write_text(json.dumps({'fps': 24, 'frames': 24, 'size': [320, 240], 'shots': [
        {'start_frame': 0, 'end_frame': 23, 'start': 0, 'end': 1.0}]}))
    assert rn('edit', workspace, 'init', '--from-shots', shots) == 0
    assert (workspace / 'assets' / 'material-7.mp4').exists() and (workspace / '.renoise').exists()
    assert rn('edit', workspace, 'set', 's01', 'src=edit-1/assets/material-7.mp4') == 0


# -- review fixes --
@needs_mmk
def test_move_after_a_later_clip_and_past_two(project, media):
    assert rn('edit', project, 'move', 'c1', '--after', 'c2') == 0
    t = timeline(project)
    assert t['c2'][1:3] == (0, 1.0) and t['c1'][1:3] == (1.0, 2.5)
    clip = json.dumps({'id': 'c3', 'src': str(media / 'bars.mp4'), 'out': 0.5})
    assert rn('edit', project, 'insert', '--clip', clip) == 0  # c2 c1 c3
    assert rn('edit', project, 'move', 'c2', '--after', 'c3') == 0
    t = timeline(project)
    assert [k for k, v in sorted(t.items(), key=lambda kv: kv[1][1]) if v[0] == 'track_V1'] == ['c1', 'c3', 'c2']
    assert t['c1'][1:3] == (0, 1.5) and t['c3'][1:3] == (1.5, 2.0) and t['c2'][1:3] == (2.0, 3.0)


@needs_mmk
def test_inside_the_project_folder(project, monkeypatch, media):
    monkeypatch.chdir(project)
    assert rn('edit', '.', 'batch', 'set c1 out=1.0', 'split c2 --at 1.5') == 0
    voice = json.dumps({'id': 'v1', 'src': str(media / 'voice.wav'), 'at': 0, 'role': 'voice'})
    assert rn('edit', '.', 'insert', '--track', 'audio', '--clip', voice) == 0
    assert rn('edit', '.', 'duck') == 0
    assert rn('edit', '.', 'show') == 0
    assert (project / 'sheets' / 'show-cut.jpg').exists()


@needs_mmk
def test_undo_puts_rns_records_back(project):
    assert rn('edit', project, 'set', 'c2', 'transition.type=wipeleft', 'transition.duration=0.5') == 0
    assert rn('edit', project, 'undo') == 0
    assert rn('edit', project, 'set', 'c2', 'transition.type=fade', 'transition.duration=0.5') == 0
    assert rn('edit', project, 'set', 'a1', 'gain_db=-6') == 0
    assert rn('edit', project, 'undo') == 0
    assert rn('edit', project, 'set', 'a1', 'fade_in=0.5') == 0
    doc = json.loads((project / 'project.json').read_text())
    keys = doc['drivers'][doc['extensions']['clips']['a1']['properties']['volume']['driver_ref']]['keyframes']
    assert max(k['value'] for k in keys) == 0.0  # the undone -6 dB stays undone


@needs_mmk
def test_batch_keeps_anchors_and_rendered_transitions(project, media):
    overlay = json.dumps({'id': 'o2', 'image': str(media / 'red.png'), 'at': {'clip': 'c2'}, 'duration': 0.25})
    assert rn('edit', project, 'batch', f"insert --track 2 --clip '{overlay}'", 'set c2 transition.type=wipeleft transition.duration=0.5') == 0
    assert rn('edit', project, 'set', 'c2', 'transition=none') == 0
    t = timeline(project)
    assert 'x_c2' not in t and t['c1'][2] == 1.5 and t['c2'][1] == 1.5
    assert rn('edit', project, 'set', 'c1', 'out=1.0') == 0
    assert timeline(project)['o2'][1] == 1.0  # still follows c2


@needs_mmk
def test_set_in_on_an_audio_item_keeps_its_place(project):
    assert rn('edit', project, 'set', 'a1', 'in=0.5') == 0
    assert timeline(project)['a1'][1] == 0 and timeline(project)['a1'][3] == 0.5
    assert rn('edit', project, 'set', 'a1', 'in=1.0', 'duration=2') == 0
    assert timeline(project)['a1'][1:4] == (0, 2.0, 1.0)


@needs_mmk
def test_speed_with_a_duration(project):
    assert rn('edit', project, 'set', 'c2', 'speed=2', 'duration=0.25') == 0
    assert timeline(project)['c2'][1:3] == (1.5, 1.75)


@needs_mmk
def test_a_new_source_keeps_the_transition(project, media):
    assert rn('edit', project, 'set', 'c2', 'transition.type=fade', 'transition.duration=0.5') == 0
    assert rn('edit', project, 'set', 'c2', f'src={media / "ramp.mp4"}', 'in=0.5') == 0
    t = timeline(project)
    assert any(v[0] == 'track_V1' and k.startswith('t_c2') for k, v in t.items())
    assert t['c2'][1:3] == (1.5, 2.5)


@needs_mmk
def test_timing_on_a_rendered_clip_renders_it_again(project):
    assert rn('edit', project, 'set', 'c2', 'vf=hue=s=0') == 0
    assert rn('edit', project, 'set', 'c2', 'in=0.5') == 0
    state = json.loads((project / 'rn' / 'state.json').read_text())
    assert state['baked']['c2']['in'] == 0.5 and state['baked']['c2']['vf'] == 'hue=s=0'
    assert timeline(project)['c2'][1:3] == (1.5, 2.25)  # same out point, 0.25 s shorter


@needs_mmk
def test_a_clips_own_sound_is_not_deleted_alone(project, capsys):
    assert rn('edit', project, 'delete', 'c2_audio') == 1
    assert 'set c2 audio=false' in capsys.readouterr().err
    assert 'c2' in timeline(project)


@needs_mmk
def test_a_failed_insert_leaves_no_media_behind(project, media):
    before = set(os.listdir(project / 'assets'))
    clip = json.dumps({'id': 'o9', 'src': str(media / 'silent.mp4'), 'at': 0.5, 'duration': 0.5, 'style': {'mix-blend-mode': 'multiply'}})
    assert rn('edit', project, 'insert', '--track', '1', '--clip', clip) == 1
    assert set(os.listdir(project / 'assets')) == before
