"""Template v0.3.0 commands: fonts, assets, sheet --around/--diff, ref, and their CLI wiring."""
import hashlib
import json
import shutil

import pytest
from PIL import Image

from rn.cli import main
from rn.ff import RnError


@pytest.mark.skipif(not shutil.which('fc-list'), reason='needs fontconfig')
def test_fonts_reports_real_weights(capsys):
    from rn.fonts import css_weight, fonts_command
    assert (css_weight(80), css_weight(200), css_weight(210), css_weight(106.4)) == (400, 700, 900, 500)
    result = fonts_command(family=None, script=None, licenses=False)
    assert result['count'] > 0
    assert all(row['weights'] and row['files'] for row in result['fonts'])
    with pytest.raises(RnError, match='no installed family'):
        fonts_command(family='No Such Family 123', script=None, licenses=False)
    assert main(['fonts', '--script', 'latin']) == 0
    assert 'families' in capsys.readouterr().out


def _library(tmp_path, monkeypatch):
    files = tmp_path / 'lib'
    files.mkdir()
    (files / 'whoosh.wav').write_bytes(b'RIFF-fake-whoosh')
    (files / 'pop.wav').write_bytes(b'RIFF-fake-pop')
    assets = [{'id': 'sfx-whoosh-fast', 'kind': 'sfx', 'title': 'Fast whoosh', 'tags': ['whoosh', 'transition'], 'duration': 0.6,
               'license': 'CC0', 'source': 'test', 'url': 'whoosh.wav', 'sha256': hashlib.sha256(b'RIFF-fake-whoosh').hexdigest()},
              {'id': 'sfx-pop', 'kind': 'sfx', 'title': 'Pop', 'tags': ['ui', 'pop'], 'license': 'CC0', 'source': 'test',
               'url': 'pop.wav', 'sha256': 'deadbeef'}]
    (files / 'manifest.json').write_text(json.dumps({'version': 1, 'assets': assets}))
    monkeypatch.setenv('RN_ASSETS_URL', str(files / 'manifest.json'))
    monkeypatch.setenv('RN_ASSETS_CACHE', str(tmp_path / 'cache'))


def test_assets_search_get_and_checksum(tmp_path, monkeypatch):
    from rn.assets import get_command, search_command
    _library(tmp_path, monkeypatch)
    found = search_command('whoosh', kind='sfx', limit=5)
    assert [a['id'] for a in found['assets']] == ['sfx-whoosh-fast']
    got = get_command(['sfx-whoosh-fast'], out=str(tmp_path / 'out'))
    assert (tmp_path / 'out' / 'sfx-whoosh-fast.wav').read_bytes() == b'RIFF-fake-whoosh'
    assert got['files'][0]['license'] == 'CC0'
    with pytest.raises(RnError, match='checksum mismatch'):
        get_command(['sfx-pop'], out=str(tmp_path / 'out'))
    with pytest.raises(RnError, match='unknown asset'):
        get_command(['nope'], out=str(tmp_path / 'out'))


def test_assets_unpublished_library_says_so(tmp_path, monkeypatch):
    from rn.assets import search_command
    monkeypatch.setenv('RN_ASSETS_URL', str(tmp_path / 'missing' / 'manifest.json'))
    with pytest.raises(RnError):
        search_command('x', kind=None, limit=5)


def test_sheet_around_and_diff(media, tmp_path):
    from rn.frames import sheet_command
    around = sheet_command([str(media / 'ramp.mp4')], out=str(tmp_path / 'a.jpg'), every=None, rate=None, indices=None,
                           seconds=None, count=None, width=160, columns=6, max_kb=125, around=[1.0], span=2)
    assert around['pages'][0]['indices'] == [22, 23, 24, 25, 26]
    diff = sheet_command([str(media / 'ramp.mp4')], out=str(tmp_path / 'd.jpg'), every=None, rate=None, indices=[0, 8, 9],
                         seconds=None, count=None, width=160, columns=3, max_kb=125, diff=True)
    assert diff['pages'][0]['indices'] == [0, 8, 9]
    assert Image.open(tmp_path / 'd.jpg').size[0] > 0


def test_cuts_cli(media, capsys):
    assert main(['cuts', str(media / 'bars.mp4')]) == 0
    assert '1 shots' in capsys.readouterr().out


def test_ref_writes_a_breakdown(media, tmp_path):
    pytest.importorskip('librosa')
    pytest.importorskip('matplotlib')
    from rn.ref import ref_command
    result = ref_command(str(media / 'ramp.mp4'), out=str(tmp_path / 'ref'), every=2, transcribe=False, language=None, model='small')
    saved = json.loads((tmp_path / 'ref' / 'ref.json').read_text())
    assert saved['coverage']['sampled'] == 30 and saved['coverage']['frames'] == 60
    assert result['listen']['duration'] == pytest.approx(2.5, abs=0.05)
    assert all((tmp_path / 'ref' / p.split('/')[-1]).exists() for p in saved['coverage']['pages'])
