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
