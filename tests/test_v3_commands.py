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
    (files / 'manifest.json').write_text(json.dumps({'version': 1, 'assets': assets}), encoding='utf-8')
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


def _discoverable_library(tmp_path, monkeypatch):
    """Overlays with categories, descriptions, previews and a "use", plus a LUT and an sfx without one."""
    files = tmp_path / 'lib'
    files.mkdir()
    entries = []
    for asset_id, kind, category, description, use in [
        ('overlay/leak-warm', 'overlay', 'light-leak', 'orange flare drifting across the frame', {'src': '{src}', 'style': {'mix-blend-mode': 'screen'}}),
        ('overlay/leak-cool', 'overlay', 'light-leak', 'blue streak wipe', None),
        ('overlay/dust-1', 'overlay', 'dust', 'film dust and scratches', {'src': '{src}', 'style': {'mix-blend-mode': 'multiply', 'opacity': 0.6}}),
        ('lut/bleach', 'lut', 'film', 'desaturated high contrast', None),
        ('sfx/whoosh', 'sfx', 'whoosh', 'fast air pass', None),
    ]:
        name = asset_id.replace('/', '-')
        body = name.encode()
        (files / f'{name}.bin').write_bytes(body)
        Image.new('RGB', (64, 36), (200, 120, 40)).save(files / f'{name}.jpg')
        entry = {'id': asset_id, 'kind': kind, 'category': category, 'title': name, 'description': description, 'tags': [category],
                 'license': 'CC0', 'source': 'test', 'url': f'{name}.bin', 'sha256': hashlib.sha256(body).hexdigest(), 'preview': f'{name}.jpg'}
        if use:
            entry['use'] = use
        entries.append(entry)
    (files / 'manifest.json').write_text(json.dumps({'version': 1, 'assets': entries}), encoding='utf-8')
    monkeypatch.setenv('RN_ASSETS_URL', str(files / 'manifest.json'))
    monkeypatch.setenv('RN_ASSETS_CACHE', str(tmp_path / 'cache'))


def test_assets_catalog_description_and_edl_use(tmp_path, monkeypatch):
    from rn.assets import get_command, search_command
    _discoverable_library(tmp_path, monkeypatch)
    # no words: the catalog counts each kind's categories, most first
    listing = search_command('', kind=None, limit=20)
    assert listing['catalog'] == {'lut': {'film': 1}, 'overlay': {'light-leak': 2, 'dust': 1}, 'sfx': {'whoosh': 1}}
    assert 'catalog' not in search_command('flare', kind=None, limit=20)
    # the description is searched, so "flare" finds a light leak whose id and tags never say it
    assert [a['id'] for a in search_command('flare', kind='overlay', limit=5)['assets']] == ['overlay/leak-warm']
    assert search_command('', kind='lut', limit=5)['assets'][0]['id'] == 'lut/bleach'
    # get: the manifest's "use" with the path filled in; a plain object for the kind otherwise
    files = {f['id']: f for f in get_command(['overlay/leak-warm', 'overlay/leak-cool', 'lut/bleach', 'sfx/whoosh'], out=str(tmp_path / 'out'))['files']}
    warm = files['overlay/leak-warm']
    assert warm['place'] == 'overlay clip' and warm['edl'] == {'src': warm['path'], 'style': {'mix-blend-mode': 'screen'}}
    assert files['overlay/leak-cool']['edl'] == {'src': files['overlay/leak-cool']['path']}
    assert files['lut/bleach']['edl'] == {'vf': f'lut3d=file={files["lut/bleach"]["path"]}'} and files['lut/bleach']['place'] == 'clip'
    assert files['sfx/whoosh']['edl'] == {'src': files['sfx/whoosh']['path'], 'role': 'sfx'}
    assert (tmp_path / 'out' / 'overlay' / 'leak-warm.bin').exists()  # "<category>/<name>" ids land in a subfolder


def test_assets_cli_proof_and_next(tmp_path, monkeypatch, capsys):
    _discoverable_library(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(['assets', 'search', 'leak', '--kind', 'overlay']) == 0
    out = capsys.readouterr().out
    assert 'proof: assets.proof.jpg' in out and (tmp_path / 'assets.proof.jpg').exists()
    assert 'next: rn assets get overlay/leak-cool overlay/leak-warm -o assets' in out
    assert main(['assets', 'search', '--kind', 'overlay']) == 0
    assert 'overlay: light-leak 2, dust 1' in capsys.readouterr().out
    assert main(['assets', 'get', 'overlay/dust-1']) == 0
    assert 'next: EDL overlay clip: {"src": "assets/overlay/dust-1.bin", "style": {"mix-blend-mode": "multiply", "opacity": 0.6}}' in capsys.readouterr().out
    assert main(['assets', 'get', 'lut/bleach']) == 0
    assert 'next: EDL clip: {"vf": "lut3d=file=assets/lut/bleach.bin"}  (path relative to the EDL)' in capsys.readouterr().out


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
    saved = json.loads((tmp_path / 'ref' / 'ref.json').read_text(encoding='utf-8'))
    assert saved['coverage']['sampled'] == 30 and saved['coverage']['frames'] == 60
    assert result['listen']['duration'] == pytest.approx(2.5, abs=0.05)
    assert all((tmp_path / 'ref' / p.split('/')[-1]).exists() for p in saved['coverage']['pages'])
    assert saved['coverage']['page_frames'][0][0] == 0 and saved['coverage']['page_frames'][-1][1] == 58
    assert (tmp_path / 'ref' / 'GUIDE.md').read_text(encoding='utf-8').startswith('# Reading a reference edit')


def test_cut_sync_measures_offsets_to_the_nearest_onset():
    from fractions import Fraction
    from rn.ref import cut_sync
    cuts = [{'time': 1.0}, {'time': 2.0}, {'time': 3.0}]
    sync = cut_sync(cuts, [0.95, 2.5, 3.04], Fraction(25))
    assert sync['offsets_frames'] == [1, -12, -1] and sync['share'] == 0.67
    assert cut_sync(cuts, [], Fraction(25)) is None


def test_a_one_frame_insert_merged_into_a_cut_stays_a_suspect(tmp_path):
    # Red, a 1-2 frame white insert, red: two strong changes closer than min_gap, so one is merged away.
    import subprocess
    from rn.cuts import detect
    clip = tmp_path / 'insert.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=64x64:r=24:d=0.5', '-f', 'lavfi',
                    '-i', 'color=white:s=64x64:r=24:d=0.0417', '-f', 'lavfi', '-i', 'color=red:s=64x64:r=24:d=0.5',
                    '-filter_complex', '[0][1][2]concat=n=3:v=1', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p',
                    str(clip)], check=True)
    result = detect(str(clip), 0.3, 0.3)
    (cut,) = result['cuts']
    assert any(0 < abs(s['frame'] - cut['frame']) <= 2 for s in result['suspects'])
