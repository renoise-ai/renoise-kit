"""v0.7.0: rn look (tone and colour as numbers, against references) and rn grade (a .cube LUT toward references)."""
from pathlib import Path
import json

import numpy as np
import pytest
from conftest import ffmpeg, write_edl
from PIL import Image

from rn.cli import main
from rn.edl import filter_files, load



def lut3d(path) -> str:
    """lut3d on a file by absolute path, quoted so a Windows drive colon survives the graph and option parsers."""
    return "lut3d=file='" + Path(path).as_posix().replace(':', '\\:') + "'"

def rn(*args) -> int:
    return main([str(a) for a in args])


def look(capsys, *args) -> dict:
    capsys.readouterr()  # drop earlier output
    path = args[-1]
    assert rn('look', *args[:-1], '--json', path) == 0
    return json.loads(open(path, encoding='utf-8').read())


@pytest.fixture(scope='module')
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp('color')
    # Muted, moving gradients: tones from near black to near white, a few gentle hues (no saturated test bars).
    base = 'gradients=s=320x240:r=24:d=1:c0=0x101418:c1=0xe8e0d0:c2=0x50708c:c3=0x8c6a4a:nb_colors=4:speed=0.05:seed=7'
    # The reference: cool and contrasty. The source: warm and flat, as another generation call might come out.
    ffmpeg('-f', 'lavfi', '-i', base, '-vf', 'eq=contrast=1.25,colorbalance=rs=-0.15:bs=0.2:rm=-0.1:bm=0.15',
           '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', str(root / 'ref.mp4'))
    ffmpeg('-f', 'lavfi', '-i', base, '-vf', 'eq=contrast=0.7:brightness=0.06,colorbalance=rs=0.25:bs=-0.2:rm=0.2:bm=-0.15:rh=0.1',
           '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', str(root / 'src.mp4'))
    return root


def test_look_measures_tone_casts_and_says_how_far_from_the_reference(tmp_path, clips, capsys):
    result = look(capsys, clips / 'src.mp4', clips / 'ref.mp4', '--ref', clips / 'ref.mp4', tmp_path / 'look.json')
    src, ref = result['inputs']
    assert len(src['samples']) == 8
    s = src['stats']
    assert s['black'] < s['mid'] < s['white'] and s['contrast'] == pytest.approx(s['white'] - s['black'], abs=0.02)
    assert set(s['zones']) == {'shadows', 'mids', 'highlights'}
    # Warm source: its shadows lean yellow against the reference's blue shadows; its contrast is lower.
    assert src['vs_ref']['zones']['shadows']['b'] > 20 and src['vs_ref']['tone']['contrast'] < -5
    assert src['vs_ref']['match'] == 'far' and src['vs_ref']['distance'] > 12
    assert ref['vs_ref']['match'] == 'close' and ref['vs_ref']['distance'] < 1 and ref['vs_ref']['de76'] < 1
    out = capsys.readouterr().out
    assert 'src.mp4  L* ' in out and '-> ref: FAR' in out and '-> ref: CLOSE' in out and 'distance = tone + cast' in out
    # The verdict threshold is the agent's to move.
    loose = look(capsys, clips / 'src.mp4', '--ref', clips / 'ref.mp4', '--tolerance', 50, tmp_path / 'loose.json')
    assert loose['inputs'][0]['vs_ref']['match'] == 'close'


def test_look_reads_images_folders_and_flags_clipping(tmp_path, capsys):
    stills = tmp_path / 'stills'
    stills.mkdir()
    half = np.zeros((100, 100, 3), np.uint8)
    half[:, 50:] = (200, 120, 60)
    Image.fromarray(half).save(stills / 'a.png')
    Image.new('RGB', (100, 100), (255, 255, 255)).save(stills / 'b.png')
    result = look(capsys, stills, stills / 'a.png', tmp_path / 'l.json')
    folder, single = result['inputs']
    assert folder['samples'] == ['a.png', 'b.png'] and single['samples'] == ['a.png']
    assert single['stats']['clipped_black'] == pytest.approx(0.5) and 'crushed blacks 50.0%' in single['stats']['flags']
    assert any(f.startswith('blown highlights') for f in folder['stats']['flags'])
    assert single['stats']['zones']['highlights']['a'] is None  # no pixels there: no cast to report


def test_look_per_shot(tmp_path, capsys):
    video = tmp_path / 'two.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=0xc04020:s=160x120:r=24:d=1', '-f', 'lavfi', '-i', 'color=c=0x2050c0:s=160x120:r=24:d=1',
           '-filter_complex', '[0:v][1:v]concat=n=2:v=1[v]', '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv444p', str(video))
    shots = tmp_path / 'shots.json'
    shots.write_text(json.dumps({'shots': [{'shot': 0, 'start_frame': 0, 'end_frame': 24}, {'shot': 1, 'start_frame': 24, 'end_frame': 48}]}))
    result = look(capsys, video, '--shots', shots, tmp_path / 'l.json')
    first, second = result['inputs']
    assert (first['label'], first['samples'], second['samples']) == ('shot 0', ['#11'], ['#35'])
    assert first['stats']['mean_lab'][1] > 20 and second['stats']['mean_lab'][2] < -20  # red then blue


def test_grade_writes_a_lut_that_ffmpeg_applies_and_moves_the_clip_toward_the_reference(tmp_path, clips, capsys):
    cube = tmp_path / 'looks' / 'src.cube'
    preview = tmp_path / 'preview.png'
    assert rn('grade', clips / 'src.mp4', '--ref', clips / 'ref.mp4', '-o', cube, '--size', 17, '--preview', preview,
              '--json', tmp_path / 'g.json') == 0
    lines = cube.read_text().splitlines()
    assert 'LUT_3D_SIZE 17' in lines and len([l for l in lines if l[:1].isdigit()]) == 17 ** 3
    report = json.loads((tmp_path / 'g.json').read_text())
    assert report['after']['distance'] < report['before']['distance'] / 2
    assert Image.open(preview).size == (320, 240)
    assert 'lut3d=file=src.cube' in capsys.readouterr().out
    graded = tmp_path / 'graded.mp4'
    ffmpeg('-i', str(clips / 'src.mp4'), '-vf', lut3d(cube), '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', str(graded))
    before = look(capsys, clips / 'src.mp4', '--ref', clips / 'ref.mp4', tmp_path / 'b.json')['inputs'][0]['vs_ref']['distance']
    after = look(capsys, graded, '--ref', clips / 'ref.mp4', tmp_path / 'a.json')['inputs'][0]['vs_ref']['distance']
    assert after < before / 2


def test_grade_at_strength_zero_is_the_identity(tmp_path, clips):
    cube = tmp_path / 'id.cube'
    assert rn('grade', clips / 'src.mp4', '--ref', clips / 'ref.mp4', '-o', cube, '--size', 5, '--strength', 0) == 0
    values = np.array([[float(v) for v in l.split()] for l in cube.read_text().splitlines() if l[:1].isdigit()])
    axis = np.linspace(0, 1, 5)
    b, g, r = np.meshgrid(axis, axis, axis, indexing='ij')
    assert np.abs(values - np.stack([r, g, b], -1).reshape(-1, 3)).max() < 1e-4


def test_edl_lut_paths_are_relative_to_the_edl_on_main_track_and_overlays(tmp_path, clips, monkeypatch, capsys):
    project = tmp_path / 'project'
    (project / 'looks').mkdir(parents=True)
    assert rn('grade', clips / 'src.mp4', '--ref', clips / 'ref.mp4', '-o', project / 'looks' / 's.cube', '--size', 17) == 0
    vf = 'lut3d=file=looks/s.cube:interp=tetrahedral'
    edl = write_edl(project, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [
        {'clips': [{'id': 'm', 'src': str(clips / 'src.mp4'), 'vf': vf}]},
        {'clips': [{'id': 'o', 'src': str(clips / 'src.mp4'), 'at': 0, 'style': {'left': 0, 'top': 0, 'width': 160}, 'vf': vf}]}]})
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # not the EDL's folder: the LUT must still be found
    out = project / 'out.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    # the path is absolute after load, and quoted for ffmpeg (a Windows drive colon escaped): read it back as rn does
    assert [f.resolve() for f in filter_files(load(edl).base[0].vf)] == [(project / 'looks' / 's.cube').resolve()]
    graded = look(capsys, out, '--ref', clips / 'ref.mp4', tmp_path / 'o.json')['inputs'][0]['vs_ref']['distance']
    raw = look(capsys, clips / 'src.mp4', '--ref', clips / 'ref.mp4', tmp_path / 'r.json')['inputs'][0]['vs_ref']['distance']
    assert graded < raw / 2


def test_a_changed_lut_renders_the_slot_again(tmp_path, clips):
    from rn.assemble import _slot_key
    (tmp_path / 'a.cube').write_text('LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n')
    edl = write_edl(tmp_path, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24},
                               'tracks': [{'clips': [{'src': str(clips / 'src.mp4'), 'vf': 'lut3d=file=a.cube'}]}]})
    edit = load(edl)
    first = _slot_key(edit.base[0], edit, 'video')
    (tmp_path / 'a.cube').write_text('LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n0.9 0.9 0.9\n')
    edit = load(edl)
    assert _slot_key(edit.base[0], edit, 'video') != first
