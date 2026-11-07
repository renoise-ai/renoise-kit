"""v0.7.0: rn shots --keyframes (one clear frame per shot) and rn edit EDL init --from-shots (the reference's shots as a
main track, its transitions made again)."""
import json

import pytest
from PIL import Image

from conftest import ffmpeg
from rn.cli import main

pytest.importorskip('cv2')


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def measured(tmp_path_factory):
    """24 fps, 320x240, 12 s: pattern | cut | zooming fractal | 0.5 s wipe left | counter | 0.5 s dissolve | pattern
    fading out (1 s), 0.5 s black, pattern fading in (1 s). Measured once with keyframes."""
    root = tmp_path_factory.mktemp('skeleton')
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2', '-pix_fmt', 'yuv420p', str(root / 'a.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'mandelbrot=s=320x240:r=24', '-t', '3', '-pix_fmt', 'yuv420p', str(root / 'b.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc=s=320x240:r=24:d=3', '-pix_fmt', 'yuv420p', str(root / 'c.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2.5', '-vf', 'hue=h=200,fade=t=out:st=1.5:d=1', '-pix_fmt', 'yuv420p', str(root / 'd.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'color=black:s=320x240:r=24:d=0.5', '-pix_fmt', 'yuv420p', str(root / 'black.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2', '-vf', 'hue=h=90,fade=t=in:st=0:d=1', '-pix_fmt', 'yuv420p', str(root / 'e.mp4'))
    film = root / 'film.mp4'
    ffmpeg(*(x for name in 'abcd' for x in ('-i', str(root / f'{name}.mp4'))), '-i', str(root / 'black.mp4'), '-i', str(root / 'e.mp4'),
           '-filter_complex', '[1:v][2:v]xfade=transition=wipeleft:duration=0.5:offset=2.5[bc];[bc][3:v]xfade=transition=fade:duration=0.5:offset=5[bcd];'
           '[0:v][bcd][4:v][5:v]concat=n=4:v=1[v]', '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(film))
    report = root / 'shots.json'
    assert rn('shots', film, '--jobs', 2, '--keyframes', root / 'kf', '--json', report) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert [s['transition_in'] for s in result['shots']] == [None, 'cut', 'wipe', 'dissolve', 'fade']
    return film, report, result


def test_keyframes_one_full_size_frame_per_shot_clear_of_transitions(measured):
    from rn.shots import interior

    film, _, result = measured
    shots = result['shots']
    for k, shot in enumerate(shots):
        kf = shot['keyframe']
        assert Image.open(kf['path']).size == (320, 240)
        lo, hi = interior(shots, k, 24.0)
        assert shot['start_frame'] <= lo <= kf['index'] < hi <= shot['end_frame']
        assert kf['time'] == pytest.approx(kf['index'] / 24, abs=1e-3) and kf['sharpness'] > 0
    wipe, dissolve, fade = shots[2]['transition'], shots[3]['transition'], shots[4]['transition']
    assert interior(shots, 1, 24.0)[1] == wipe['start_frame'], 'the shot before a wipe ends where the wipe begins'
    assert interior(shots, 2, 24.0)[0] == wipe['start_frame'] + wipe['frames']
    assert interior(shots, 3, 24.0) == (dissolve['start_frame'] + dissolve['frames'], shots[4]['start_frame'] - fade['hold_frames'] - fade['out_frames'])
    assert interior(shots, 4, 24.0)[0] == shots[4]['start_frame'] + fade['in_frames']


def test_keyframes_sharpest_beats_a_blurred_frame(tmp_path):
    """One shot, blurred for its first 1.5 s: the sharpest keyframe comes from the sharp part; --which first does not."""
    film = tmp_path / 'blur.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=3', '-vf', "boxblur=luma_radius=8:chroma_radius=8:enable='lt(t,1.5)'",
           '-pix_fmt', 'yuv420p', str(film))
    for which, check in (('sharpest', lambda i: i >= 36), ('first', lambda i: i == 0), ('last', lambda i: i == 71), ('mid', lambda i: i == 35)):
        report = tmp_path / f'{which}.json'
        assert rn('shots', film, '--jobs', 1, '--keyframes', tmp_path / which, '--which', which, '--json', report) == 0
        (shot,) = json.loads(report.read_text(encoding='utf-8'))['shots']
        assert check(shot['keyframe']['index']), (which, shot['keyframe'])
        assert sorted(p.name for p in (tmp_path / which).iterdir()) == ['shot_000.png']


def test_init_lays_the_shots_out_as_placeholders_of_the_reference_length(measured, tmp_path):
    film, report, result = measured
    edl = tmp_path / 'edit.json'
    assert rn('edit', edl, 'init', '--from-shots', report) == 0
    raw = json.loads(edl.read_text(encoding='utf-8'))
    clips = raw['tracks'][0]['clips']
    assert [c['id'] for c in clips] == ['s01', 's02', 's03', 's04', 's05'] and all(c['src'] == f'gen/{c["id"]}.mp4' for c in clips)
    assert [c.get('transition', {}).get('type') for c in clips] == [None, None, 'wipeleft', 'fade', None]
    assert clips[2]['transition']['duration'] == pytest.approx(result['shots'][2]['transition']['frames'] / 24, abs=1e-4)
    assert 'fade=t=out' in clips[3]['vf'] and clips[4]['vf'].startswith('fade=t=in') and 'color=black' in clips[4]['vf']
    assert clips[2]['meta']['transition']['edge'] == 'left' and clips[2]['meta']['keyframe'].endswith('shot_002.png')
    assert (raw['output']['width'], raw['output']['height'], raw['output']['fps']) == (320, 240, 24)
    assert rn('edit', edl, 'init', '--from-shots', report) == 1, 'an existing edit is not replaced without --force'
    out = tmp_path / 'out.mp4'
    assert rn('assemble', edl, '-o', out, '--placeholders') == 0
    assert rn('qa', out, '--edl', edl, '--placeholders', '--expect-frames', result['frames'], '--json', tmp_path / 'qa.json') == 0
    checks = {c['check']: c for c in json.loads((tmp_path / 'qa.json').read_text(encoding='utf-8'))['checks']}
    assert checks['frames']['status'] == 'PASS'
    # a slot is filled while the others are still missing
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=3', '-pix_fmt', 'yuv420p', str(tmp_path / 'g2.mp4'))
    assert rn('edit', edl, 'set', 's02', 'src=g2.mp4') == 0


def test_init_from_the_reference_rebuilds_it(measured, tmp_path):
    """--src cuts every clip from the reference: the rebuild has its frames, its cuts and its transitions."""
    film, report, result = measured
    edl = tmp_path / 'edit.json'
    assert rn('edit', edl, 'init', '--from-shots', report, '--src', film) == 0
    raw = json.loads(edl.read_text(encoding='utf-8'))
    assert raw['tracks'][0]['clips'][1]['in'] == {'frame': 48} and not any('vf' in c for c in raw['tracks'][0]['clips'])
    out = tmp_path / 'rebuilt.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    for name, path in (('ref', film), ('out', out)):
        assert rn('cuts', path, '--json', tmp_path / f'cuts-{name}.json') == 0
    cuts = [[c['frame'] for c in json.loads((tmp_path / f'cuts-{name}.json').read_text(encoding='utf-8'))['cuts']] for name in ('ref', 'out')]
    assert cuts[0] == cuts[1]
    assert rn('shots', out, '--jobs', 2, '--json', tmp_path / 'again.json') == 0
    again = json.loads((tmp_path / 'again.json').read_text(encoding='utf-8'))
    assert again['frames'] == result['frames']
    assert [(s['transition_in'], s['start_frame']) for s in again['shots']] == [(s['transition_in'], s['start_frame']) for s in result['shots']]


def test_init_makes_a_transition_with_no_room_a_cut(tmp_path):
    """Two dissolves measured so long that their overlaps would collide inside the shot between them: the second is made
    as a cut and listed."""
    shots = {'path': 'x.mp4', 'fps': '24', 'frames': 96, 'size': [320, 240], 'shots': [
        {'shot': 0, 'start_frame': 0, 'end_frame': 40, 'start': 0, 'end': 1.6667, 'transition_in': None},
        {'shot': 1, 'start_frame': 40, 'end_frame': 52, 'start': 1.6667, 'end': 2.1667, 'transition_in': 'dissolve',
         'transition': {'frames': 12, 'start_frame': 34}},
        {'shot': 2, 'start_frame': 52, 'end_frame': 96, 'start': 2.1667, 'end': 4, 'transition_in': 'dissolve',
         'transition': {'frames': 12, 'start_frame': 44}}]}
    (tmp_path / 'shots.json').write_text(json.dumps(shots), encoding='utf-8')
    edl = tmp_path / 'edit.json'
    assert rn('edit', '--json', tmp_path / 'r.json', edl, 'init', '--from-shots', tmp_path / 'shots.json') == 0
    result = json.loads((tmp_path / 'r.json').read_text(encoding='utf-8'))
    assert result['dropped'] == ['s03'] and result['timeline']['output']['frames'] == 96
    clips = json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips']
    assert clips[1]['transition']['type'] == 'fade' and 'transition' not in clips[2]
