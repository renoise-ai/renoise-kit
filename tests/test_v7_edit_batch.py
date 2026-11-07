"""v0.7.0: rn edit batch (several operations, one checked write), the per-process probe cache, rn sheet --crop."""
import io
import json

import pytest
from conftest import ffmpeg, write_edl
from PIL import Image

from rn import ff
from rn.cli import main
from rn.ff import probe


def rn(*args) -> int:
    return main([str(a) for a in args])


def base_edl(media, name):
    return write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'id': 'a', 'src': 'ramp.mp4', 'in': 0, 'out': 1},
        {'id': 'b', 'src': 'ramp.mp4', 'in': {'frame': 30}, 'out': 2},
    ]}]}, name)


def clips(edl):
    return json.loads(edl.read_text(encoding='utf-8'))['tracks'][0]['clips']


def test_batch_applies_every_step_and_writes_once(media, capsys):
    edl = base_edl(media, 'batch.json')
    assert rn('edit', edl, 'batch', 'set a out=0.5', 'set b transition.type=dissolve transition.duration=0.1', 'split b --at 1.0',
              'insert --after a --clip \'{"id": "c", "color": "red", "duration": 0.25}\'') == 0
    got = clips(edl)
    assert [c.get('id') for c in got] == ['a', 'c', 'b', 'b-2']
    assert got[0]['out'] == 0.5 and got[2]['transition'] == {'type': 'dissolve', 'duration': 0.1}
    assert '4 steps: set a; set b; split b into b + b-2; inserted c' in capsys.readouterr().out
    assert not list(media.glob('.batch.rn-batch*'))


def test_a_failing_step_writes_nothing(media, capsys):
    edl = base_edl(media, 'batch-fail.json')
    before = edl.read_text(encoding='utf-8')
    assert rn('edit', edl, 'batch', 'set a out=0.5', 'set b speed=99') == 1
    assert 'step 2 (set b speed=99)' in capsys.readouterr().err
    assert rn('edit', edl, 'batch', 'set a out=0.5', 'frobnicate a') == 1
    assert rn('edit', edl, 'batch', 'set a out=0.5', "set b meta.label='open") == 1  # unbalanced quote
    assert edl.read_text(encoding='utf-8') == before
    assert not list(media.glob('.batch-fail.rn-batch*'))


def test_batch_reads_one_operation_per_line_from_stdin(media, monkeypatch):
    edl = base_edl(media, 'batch-stdin.json')
    monkeypatch.setattr('sys.stdin', io.StringIO('# trims\nset a out=0.5\n\nset b meta.take=2\n'))
    assert rn('edit', edl, 'batch') == 0
    got = clips(edl)
    assert got[0]['out'] == 0.5 and got[1]['meta'] == {'take': 2}


def test_batch_probes_each_source_once(media, monkeypatch):
    calls = []
    real = ff.ffprobe_json
    monkeypatch.setattr(ff, 'ffprobe_json', lambda *a: calls.append(a) or real(*a))
    edl = base_edl(media, 'batch-probe.json')
    ff._PROBED.clear()
    assert rn('edit', edl, 'set', 'a', 'out=0.9') == 0
    one_step = len(calls)
    calls.clear()
    ff._PROBED.clear()
    assert rn('edit', edl, 'batch', *(f'set a out={0.5 + i / 10}' for i in range(4)), 'split b --at 1.2') == 0
    assert 0 < len(calls) == one_step


def test_probe_cache_sees_a_rewritten_file(tmp_path):
    clip = tmp_path / 'take.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=24:d=1', '-pix_fmt', 'yuv420p', str(clip))
    assert probe(clip)['video']['frames'] == 24
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=24:d=2', '-pix_fmt', 'yuv420p', str(clip))
    assert probe(clip)['video']['frames'] == 48


def test_sheet_crop_zooms_into_a_region(media, tmp_path):
    plain, cropped = tmp_path / 'plain.jpg', tmp_path / 'crop.jpg'
    assert rn('sheet', media / 'bars.mp4', '--count', 2, '--width', 100, '-o', plain) == 0
    assert rn('sheet', media / 'bars.mp4', '--count', 2, '--width', 100, '--crop', '200:400:0:0', '-o', cropped) == 0
    # 1280x720 scaled to 100 wide is 56 px tall; a 200x400 region scaled to 100 wide is 200 px tall
    assert Image.open(cropped).height - Image.open(plain).height == 200 - 56
    assert rn('sheet', media / 'bars.mp4', '--crop', '200:400:1200:0', '-o', tmp_path / 'out.jpg') == 1
    with pytest.raises(SystemExit):  # argparse usage error
        rn('sheet', media / 'bars.mp4', '--crop', '200x400', '-o', tmp_path / 'bad.jpg')


def test_sheet_crop_on_a_folder_of_stills(media, tmp_path):
    folder = tmp_path / 'stills'
    folder.mkdir()
    Image.open(media / 'card.png').save(folder / 'a.png')
    assert rn('sheet', folder, '--crop', '100:50:0:0', '--width', 100, '-o', tmp_path / 's.jpg') == 0
    assert rn('sheet', folder, '--crop', '100:50:350:0', '-o', tmp_path / 'x.jpg') == 1
