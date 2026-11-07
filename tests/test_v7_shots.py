"""v0.7.0: rn shots (boundaries with dissolves and fades, per-shot picture measurements) and rn qa --placeholders."""
import json

import pytest

from conftest import ffmpeg, write_edl
from rn.cli import main

pytest.importorskip('cv2')


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def edit_film(tmp_path_factory):
    """24 fps, 320x240: moving test pattern | hard cut | frozen bars | 0.5 s dissolve | a still zoomed in steadily |
    1 s fade out, 0.5 s black, 1 s fade in | moving test pattern."""
    root = tmp_path_factory.mktemp('shots')
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=1280x960:r=24:d=1', '-frames:v', '1', str(root / 'still.png'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=1.5', '-pix_fmt', 'yuv420p', str(root / 'a.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'smptebars=s=320x240:r=24:d=2', '-pix_fmt', 'yuv420p', str(root / 'b.mp4'))
    # a still zoomed 6% a second about its centre (at 4x size, so the zoom is smooth at 320): every frame is the previous
    # one scaled, nothing moves inside
    ffmpeg('-loop', '1', '-framerate', '24', '-t', '4', '-i', str(root / 'still.png'),
           '-vf', "scale=w='trunc(1280*(1+0.06*t)/2)*2':h='trunc(960*(1+0.06*t)/2)*2':eval=frame,crop=1280:960,scale=320:240,"
           'fade=t=out:st=3:d=1', '-pix_fmt', 'yuv420p', str(root / 'c.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=1.5', '-vf', 'hue=h=90,fade=t=in:st=0:d=1',
           '-pix_fmt', 'yuv420p', str(root / 'd.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'color=black:s=320x240:r=24:d=0.5', '-pix_fmt', 'yuv420p', str(root / 'black.mp4'))
    film = root / 'film.mp4'
    ffmpeg('-i', str(root / 'a.mp4'), '-i', str(root / 'b.mp4'), '-i', str(root / 'c.mp4'), '-i', str(root / 'black.mp4'),
           '-i', str(root / 'd.mp4'), '-filter_complex',
           '[1:v][2:v]xfade=transition=dissolve:duration=0.5:offset=1.5[bc];[0:v][bc][3:v][4:v]concat=n=4:v=1[v]',
           '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(film))
    return film


def test_shots_finds_cuts_dissolves_and_fades(edit_film, tmp_path):
    report = tmp_path / 'shots.json'
    assert rn('shots', edit_film, '--json', report, '--jobs', 2, '--sheet', tmp_path / 'shots.jpg') == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    shots = result['shots']
    assert [s['transition_in'] for s in shots] == [None, 'cut', 'dissolve', 'fade']
    assert shots[1]['start'] == pytest.approx(1.5, abs=0.05)
    assert shots[2]['start'] == pytest.approx(1.5 + 1.75, abs=0.3), 'the dissolve is centred in its 0.5 s overlap'
    assert shots[-1]['end_frame'] == result['frames']
    assert (tmp_path / 'shots.jpg').exists() and result['sheet']


def test_shots_measures_what_the_picture_does(edit_film, tmp_path):
    report = tmp_path / 'shots.json'
    assert rn('shots', edit_film, '--json', report, '--jobs', 1) == 0
    moving, bars, zoom, _ = json.loads(report.read_text(encoding='utf-8'))['shots']
    assert bars['still']['identical_pairs'] > 0.85, 'bars hold still (the shot ends inside the dissolve, so its last frames blend)'
    assert bars['texture']['flat_blocks'] > 0.5 and bars['texture']['palette95'] < 40, 'bars are flat fields of few colours'
    assert zoom['still']['identical_pairs'] < 0.2
    assert zoom['motion']['rigid_fit'] > 0.45, 'one zoom of the whole frame explains a zoomed still'
    assert zoom['motion']['zoom_per_s'] > 0.02
    assert moving['motion']['rigid_fit'] < 0.45, 'the test pattern moves inside the frame'


def test_shots_is_the_same_in_parallel(edit_film, tmp_path):
    one, two = tmp_path / 'one.json', tmp_path / 'two.json'
    assert rn('shots', edit_film, '--json', one, '--jobs', 1) == 0
    assert rn('shots', edit_film, '--json', two, '--jobs', 3) == 0
    strip = lambda p: [{k: v for k, v in s.items()} for s in json.loads(p.read_text(encoding='utf-8'))['shots']]  # noqa: E731
    assert strip(one) == strip(two)


def test_qa_reads_an_edit_with_placeholders(tmp_path):
    edl = write_edl(tmp_path, {'version': 2, 'output': {'width': 160, 'height': 120, 'fps': 24},
                               'tracks': [{'clips': [{'color': '#204080', 'duration': 1},
                                                     {'src': 'not-made-yet.mp4', 'duration': 1, 'meta': {'label': 'G2 pending'}}]}]})
    out = tmp_path / 'out.mp4'
    assert rn('assemble', edl, '-o', out, '--placeholders') == 0
    assert rn('qa', out, '--edl', edl, '--json', tmp_path / 'no.json') == 1 and not (tmp_path / 'no.json').exists(), \
        'without --placeholders the missing source is an error'
    report = tmp_path / 'qa.json'
    rn('qa', out, '--edl', edl, '--placeholders', '--json', report)
    checks = {c['check']: c for c in json.loads(report.read_text(encoding='utf-8'))['checks']}
    assert checks['frames']['status'] == 'PASS'


def test_a_steady_zoom_is_rigid_whatever_its_translation_noise():
    """A zoom about the centre has next to no translation: the spread of that noise must not count as an unsteady move."""
    from rn.shots import _motion

    rng = __import__('numpy').random.default_rng(0)
    fits = [{'scale': 1.0025, 'tx': float(tx), 'ty': float(ty), 'corner': 0.5 + 0.02 * float(rng.standard_normal()),
             'flow': 0.6, 'resid': 0.08, 'inliers': 0.98, 'local': 0.0, 'left': 3.0}
            for tx, ty in rng.normal(0, 0.05, size=(20, 2))]
    motion = _motion(fits, 24.0, 1.0)
    assert motion['rigid_fit'] > 0.9 and motion['zoom_per_s'] > 0.05


def test_shots_reports_what_the_remake_rules_read(edit_film, tmp_path):
    """The measurements a remake's route rules read (soft gradients, vivid colour, how smooth light and dark parts are)."""
    report = tmp_path / 'shots.json'
    assert rn('shots', edit_film, '--json', report, '--jobs', 2) == 0
    moving, bars, zoom, _ = json.loads(report.read_text(encoding='utf-8'))['shots']
    for shot in (moving, bars, zoom):
        for key in ('flat_not_dark', 'gentle_slopes', 'vivid_share', 'lit_share', 'lit_smooth', 'ink_share', 'ink_smooth'):
            assert 0.0 <= shot['texture'][key] <= 1.0, key
        assert shot['still']['pair_diff_p90'] >= 0
        assert 'faces' not in shot, 'faces only when asked for'
    assert bars['texture']['vivid_share'] > 0.4, 'bars are mostly saturated colour'
    assert bars['texture']['flat_not_dark'] > 0.5 and bars['still']['pair_diff_p90'] < 0.6
    assert zoom['still']['pair_diff_p90'] > bars['still']['pair_diff_p90']


def test_shots_faces_counts_frames_with_a_face(edit_film, tmp_path):
    from rn.faces import face_model_path

    try:
        face_model_path()
    except Exception as error:  # no network for the model outside the sandbox
        pytest.skip(f'face model unavailable: {error}')
    report = tmp_path / 'shots.json'
    assert rn('shots', edit_film, '--json', report, '--jobs', 1, '--faces') == 0
    for shot in json.loads(report.read_text(encoding='utf-8'))['shots']:
        assert shot['faces']['frames_with_face'] == 0.0 and shot['faces']['checked'] >= 1, 'test patterns have no faces'


def test_shots_reads_a_fast_dissolve_as_a_dissolve(tmp_path):
    """A fast dissolve between very different pictures changes enough on one frame to score as a hard cut; the frames
    that blend the two sides make it a dissolve (rn shots read it as one or two cuts before 0.7.0)."""
    a, b, film = tmp_path / 'a.mp4', tmp_path / 'b.mp4', tmp_path / 'film.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'smptebars=s=320x240:r=24:d=3', '-pix_fmt', 'yuv420p', str(a))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=3', '-pix_fmt', 'yuv420p', str(b))
    ffmpeg('-i', str(a), '-i', str(b), '-filter_complex', '[0:v][1:v]xfade=transition=dissolve:duration=0.5:offset=2.75[v]',
           '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(film))
    report = tmp_path / 'shots.json'
    assert rn('shots', film, '--json', report, '--jobs', 1) == 0
    shots = json.loads(report.read_text(encoding='utf-8'))['shots']
    assert [s['transition_in'] for s in shots] == [None, 'dissolve']
    assert shots[1]['start'] == pytest.approx(3.0, abs=0.2), 'the dissolve is centred in its overlap'


def test_compare_shots_pairs_each_shot_with_its_transition_and_camera(edit_film, tmp_path):
    """rn compare --shots: the same film against itself matches shot for shot; with the dissolve made a hard cut, one
    shot begins differently and the zoom is still there."""
    root = edit_film.parent
    recut = tmp_path / 'recut.mp4'
    ffmpeg('-i', str(root / 'a.mp4'), '-i', str(root / 'b.mp4'), '-i', str(root / 'c.mp4'), '-i', str(root / 'black.mp4'),
           '-i', str(root / 'd.mp4'), '-filter_complex', '[1:v]trim=duration=1.75,setpts=PTS-STARTPTS[b];[2:v]trim=start=0.25,setpts=PTS-STARTPTS[c];'
           '[0:v][b][c][3:v][4:v]concat=n=5:v=1[v]', '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(recut))
    same, report = tmp_path / 'same.json', tmp_path / 'recut.json'
    assert rn('compare', edit_film, edit_film, '--shots', '--count', 2, '-o', tmp_path / 'same.jpg', '--json', same) == 0
    shots = json.loads(same.read_text(encoding='utf-8'))['shots']
    assert shots['a'] == shots['b'] == len(shots['pairs']) and shots['transitions_differ'] == 0 and not shots['only_b']
    assert all(p['covered'] == 1.0 and p['motion']['a'] == p['motion']['b'] for p in shots['pairs'])
    assert rn('compare', edit_film, recut, '--shots', '--count', 2, '-o', tmp_path / 'recut.jpg', '--json', report) == 0
    shots = json.loads(report.read_text(encoding='utf-8'))['shots']
    zoom = shots['pairs'][2]
    assert (zoom['a']['transition_in'], zoom['b']['transition_in']) == ('dissolve', 'cut') and shots['transitions_differ'] == 1
    assert zoom['a']['transition']['frames'] > 2 and 'transition' not in zoom['b']  # the dissolve's measured length comes along
    assert zoom['motion']['a']['zoom_per_s'] > 0.02 and zoom['motion']['b']['zoom_per_s'] > 0.02


@pytest.mark.parametrize('xfade, edge', [('wipeleft', 'left'), ('wipeup', 'up')])
def test_shots_reads_a_wipe_with_its_edge_and_length(tmp_path, xfade, edge):
    """A straight wipe (0.6 s, 14 frames at 24 fps) between two moving pictures: one boundary half way through it, with
    the direction its edge runs and how many frames it takes."""
    a, b, film = tmp_path / 'a.mp4', tmp_path / 'b.mp4', tmp_path / 'film.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=3', '-pix_fmt', 'yuv420p', str(a))
    ffmpeg('-f', 'lavfi', '-i', 'mandelbrot=s=320x240:r=24', '-t', '3', '-pix_fmt', 'yuv420p', str(b))
    ffmpeg('-i', str(a), '-i', str(b), '-filter_complex', f'[0:v][1:v]xfade=transition={xfade}:duration=0.6:offset=2.4[v]',
           '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(film))
    report = tmp_path / 'shots.json'
    assert rn('shots', film, '--json', report, '--jobs', 1) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    shots = result['shots']
    assert [s['transition_in'] for s in shots] == [None, 'wipe'] and result['transitions']['wipe'] == 1
    assert shots[1]['start'] == pytest.approx(2.7, abs=0.2)
    assert shots[1]['transition']['edge'] == edge
    assert 10 <= shots[1]['transition']['frames'] <= 18


def test_shots_says_how_long_dissolves_and_fades_take(edit_film, tmp_path):
    """The film's 0.5 s dissolve (12 frames) and its fade through black (1 s out, 0.5 s black, 1 s in)."""
    report = tmp_path / 'shots.json'
    assert rn('shots', edit_film, '--json', report, '--jobs', 2) == 0
    shots = json.loads(report.read_text(encoding='utf-8'))['shots']
    dissolve, fade = shots[2]['transition'], shots[3]['transition']
    assert 9 <= dissolve['frames'] <= 16 and dissolve['seconds'] == pytest.approx(dissolve['frames'] / 24, abs=0.01)
    assert fade['through'] == 'black' and fade['hold_frames'] >= 12
    assert fade['frames'] == fade['out_frames'] + fade['hold_frames'] + fade['in_frames'] and 24 <= fade['frames'] <= 64
    assert 'transition' not in shots[1], 'a hard cut takes no frames'


def test_shots_measures_speed_inside_a_shot(tmp_path):
    """A moving pattern slowed to a quarter speed half way, and one that freezes on its last frame for 1.5 s: the change
    per quarter, the fastest quarter over the slowest, and the longest hold."""
    ramp, hold = tmp_path / 'ramp.mp4', tmp_path / 'hold.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=4', '-vf', "setpts='if(lt(T,2),PTS,2/TB+(PTS-2/TB)*4)'", '-r', '24',
           '-t', '6', '-pix_fmt', 'yuv420p', str(ramp))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=3', '-vf', 'tpad=stop_mode=clone:stop_duration=1.5',
           '-pix_fmt', 'yuv420p', str(hold))
    speeds = {}
    for name, clip in (('ramp', ramp), ('hold', hold)):
        report = tmp_path / f'{name}.json'
        assert rn('shots', clip, '--json', report, '--jobs', 1) == 0
        (shot,) = json.loads(report.read_text(encoding='utf-8'))['shots']
        speeds[name] = shot['speed']
    first, *_, last = speeds['ramp']['change_per_quarter']
    assert first > 2.5 * last and speeds['ramp']['ramp'] >= 2.5 and speeds['ramp']['longest_hold_s'] < 0.5
    assert speeds['hold']['change_per_quarter'][-1] < 0.1 and speeds['hold']['longest_hold_s'] == pytest.approx(1.4, abs=0.15)


@pytest.fixture(scope='module')
def click_films(tmp_path_factory):
    """Four 1 s pictures cut at 1, 2 and 3 s over a click every 0.5 s (120 bpm): on the beat, and with the clicks
    shifted by a quarter second, between the beats."""
    root = tmp_path_factory.mktemp('beats')
    pictures = ['testsrc2', 'smptebars', 'mandelbrot', 'rgbtestsrc']
    films = {}
    for name, shift in (('on', 0.0), ('off', 0.25)):
        film = root / f'{name}.mp4'
        inputs = [arg for src in pictures for arg in ('-f', 'lavfi', '-t', '1', '-i', f'{src}=s=320x240:r=24')]
        ffmpeg(*inputs, '-f', 'lavfi', '-t', '4', '-i', f"aevalsrc='if(lt(mod(t+{shift},0.5),0.02),sin(2*PI*1500*t),0)':s=44100",
               '-filter_complex', '[0:v][1:v][2:v][3:v]concat=n=4:v=1[v]', '-map', '[v]', '-map', '4:a',
               '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(film))
        films[name] = film
    return films


def test_shots_beats_puts_each_cut_against_the_music(click_films, tmp_path):
    pytest.importorskip('librosa')
    results = {}
    for name, film in click_films.items():
        report = tmp_path / f'{name}.json'
        assert rn('shots', film, '--json', report, '--jobs', 1, '--beats') == 0
        results[name] = json.loads(report.read_text(encoding='utf-8'))
    on, off = results['on'], results['off']
    assert on['beats']['bpm'] == pytest.approx(120, rel=0.1) and on['beats']['boundaries'] == 3
    assert on['beats']['within_1_frame'] == 1.0 and on['beats']['chance_within_1_frame'] == pytest.approx(0.25, abs=0.05)
    assert off['beats']['within_2_frames'] == 0.0
    assert all(abs(abs(s['beat']['offset_s']) - 0.25) < 0.05 for s in off['shots'][1:]), 'a quarter second off the beat'


def test_shots_beats_reads_another_files_beat_grid(click_films, tmp_path):
    """Music for a recut: the beats of an rn listen --json result, written here by hand."""
    grid = tmp_path / 'music.json'
    grid.write_text(json.dumps({'beats': {'bpm': 60.0, 'clear': True, 'times': [0.0, 1.04, 2.0, 3.1]}}), encoding='utf-8')
    report = tmp_path / 'shots.json'
    assert rn('shots', click_films['on'], '--json', report, '--jobs', 1, '--beats', grid) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert [s['beat']['offset_frames'] for s in result['shots'][1:]] == pytest.approx([-0.96, 0.0, -2.4], abs=0.05)
    assert result['beats']['source'] == str(grid) and result['beats']['within_1_frame'] == pytest.approx(2 / 3, abs=0.01)
    bad = tmp_path / 'bad.json'
    bad.write_text('{}', encoding='utf-8')
    assert rn('shots', click_films['on'], '--beats', bad) != 0
