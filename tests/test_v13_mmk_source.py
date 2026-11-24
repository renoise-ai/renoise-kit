"""v0.13.0: rn export -o x.mmkproj writes rn/ beside the project: the edit with every file it reads under rn/, paths
relative to it, so the folder alone holds the edit it was made from; nested edits are refused; --strict."""
import json
import os
import shutil

import pytest

from conftest import write_edl
from rn.cli import main
from rn.edl import load
from rn.ff import RnError
from rn.mmk import write_source

needs_mmk = pytest.mark.skipif(not (os.environ.get('RN_MMK') or shutil.which('mmk')), reason='mmk is not installed')
OUT = {'width': 320, 'height': 240, 'fps': 24}


def test_the_source_is_self_contained_and_loads_from_its_new_place(media, tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    shutil.copyfile(media / 'ramp.mp4', work / 'ramp.mp4')
    shutil.copyfile(media / 'bars.mp4', tmp_path / 'material-812.mp4')
    (work / 'grade.cube').write_text('LUT_3D_SIZE 2\n' + '0 0 0\n' * 8)
    (work / 'captions.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nhi\n', encoding='utf-8')
    edl = write_edl(work, {'output': OUT, 'tracks': [{'clips': [
        {'id': 'a', 'src': 'ramp.mp4', 'out': 1, 'vf': "lut3d=file='grade.cube'"},
        {'id': 'b', 'src': '../material-812.mp4', 'duration': 0.5}]}],
        'subtitles': {'file': 'captions.srt'}, 'mix': {'lufs': None}})
    package = tmp_path / 'edit-x'
    source = write_source(edl, package, package / 'rn')
    raw = json.loads(source.read_text(encoding='utf-8'))
    clips = raw['tracks'][0]['clips']
    assert [clips[0]['src'], clips[1]['src'], raw['subtitles']['file']] == ['files/ramp.mp4', 'files/material-812.mp4', 'files/captions.srt'], 'a material keeps its name'
    assert clips[0]['vf'] == "lut3d=file='files/grade.cube'"
    assert (package / 'rn' / 'files' / 'ramp.mp4').stat().st_ino == (work / 'ramp.mp4').stat().st_ino, 'hard links, no copies'
    # The working copy is gone (a fresh sandbox): the folder alone still loads
    shutil.rmtree(work)
    (tmp_path / 'material-812.mp4').unlink()
    assert load(source).total_frames == 36
    # Exporting from rn/edit.json itself keeps every file where it is
    again = write_source(source, package, tmp_path / 'staging')
    assert json.loads(again.read_text(encoding='utf-8')) == raw


def test_a_nested_edit_is_refused(media, tmp_path):
    inner = write_edl(media, {'output': OUT, 'tracks': [{'clips': [{'src': 'ramp.mp4', 'out': 0.5}]}]}, 'inner.json')
    outer = write_edl(media, {'output': OUT, 'tracks': [{'clips': [{'src': inner.name}]}]}, 'outer.json')
    with pytest.raises(RnError, match='render it to a clip first'):
        write_source(outer, tmp_path / 'p', tmp_path / 'p' / 'rn')


@needs_mmk
def test_export_writes_rn_beside_the_project_and_strict_refuses_what_mmk_leaves_out(media, tmp_path):
    edl = write_edl(media, {'output': OUT, 'tracks': [{'clips': [{'id': 'a', 'src': 'ramp.mp4', 'out': 1, 'vf': 'hue=s=0'}]}], 'mix': {'lufs': None}}, 'mmk_src.json')
    package = tmp_path / 'edit-y'  # a Renoise workspace: a folder that exists (edit_video makes it)
    package.mkdir()
    assert main(['export', str(edl), '-o', str(package), '--strict']) != 0, 'vf is dropped: --strict refuses'
    assert list(package.iterdir()) == [], 'and changes nothing'
    assert main(['export', str(edl), '-o', str(package)]) == 0
    assert json.loads((package / 'rn' / 'edit.json').read_text(encoding='utf-8'))['tracks'][0]['clips'][0]['src'] == 'files/ramp.mp4'
    assert main(['export', str(package / 'rn' / 'edit.json'), '-o', str(package)]) == 0, 'the source exports again from its new place'
    nested = write_edl(media, {'output': OUT, 'tracks': [{'clips': [{'src': edl.name}]}]}, 'mmk_nested.json')
    assert main(['export', str(nested), '-o', str(tmp_path / 'n.mmkproj')]) != 0
