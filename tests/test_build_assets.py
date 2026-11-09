import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from conftest import ffmpeg
from rn import assets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_assets  # noqa: E402

LICENCE = {'license': 'CC0-1.0', 'source': 'https://example.org/pack', 'author': 'Someone'}


def test_build_assets_round_trip(tmp_path, monkeypatch):
    src = tmp_path / 'src'
    for d in ('sfx', 'fx', 'tex', 'lut'):
        (src / d).mkdir(parents=True)
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.9*sin(2*PI*880*t)*exp(-8*t)':s=48000:d=1", str(src / 'sfx' / 'Ding_01.wav'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=25:d=1', '-pix_fmt', 'yuv420p', str(src / 'fx' / 'Warm Leak.mp4'))
    Image.new('RGBA', (64, 64), (255, 200, 0, 128)).save(src / 'tex' / 'spark.png')
    Image.new('RGB', (64, 64), (200, 180, 150)).save(src / 'tex' / 'paper.jpg')
    (src / 'lut' / 'Warm.cube').write_text('LUT_3D_SIZE 2\n' + '\n'.join(
        f'{r} {g} {b * 0.8}' for b in (0, 1) for g in (0, 1) for r in (0, 1)) + '\n')
    selection = {'sets': [
        {'dir': 'sfx', 'glob': '*.wav', 'kind': 'sfx', 'category': 'ding', 'tags': ['ui'], 'strip': r'_\d+$', **LICENCE},
        {'dir': 'fx', 'glob': '*.mp4', 'kind': 'overlay', 'category': 'light-leak', 'description': '{title}, warm glow',
         'use': {'src': '{src}', 'style': {'mix-blend-mode': 'screen'}}, **LICENCE},
        {'dir': 'tex', 'glob': '*', 'kind': 'texture', 'category': 'paper', **LICENCE},
        {'dir': 'lut', 'glob': '*.cube', 'kind': 'lut', 'category': 'film', **LICENCE},
    ]}
    (tmp_path / 'sel.json').write_text(json.dumps(selection))
    out = tmp_path / 'out'
    build_assets.main([str(tmp_path / 'sel.json'), str(src), str(out), '--jobs', '2'])

    manifest = json.loads((out / 'manifest.json').read_text())
    by_id = {a['id']: a for a in manifest['assets']}
    assert set(by_id) == {'ding/ding', 'light-leak/warm-leak', 'paper/spark', 'paper/paper', 'film/warm'}
    assert by_id['paper/spark']['url'].endswith('.png') and by_id['paper/paper']['url'].endswith('.jpg')  # alpha kept
    assert by_id['ding/ding']['peak_db'] <= -1 and by_id['light-leak/warm-leak']['width'] == 320
    assert by_id['light-leak/warm-leak']['description'] == 'Warm Leak, warm glow'
    for a in manifest['assets']:
        assert (out / a['preview']).stat().st_size > 0

    monkeypatch.setenv('RN_ASSETS_URL', str(out / 'manifest.json'))
    monkeypatch.setenv('RN_ASSETS_CACHE', str(tmp_path / 'cache'))
    assert assets.search_command('warm', kind=None, limit=10)['total'] == 2
    got = assets.get_command(['light-leak/warm-leak', 'film/warm'], out=str(tmp_path / 'got'))['files']  # sha256 checked
    assert got[0]['edl']['style'] == {'mix-blend-mode': 'screen'}
    assert got[1]['edl']['vf'].startswith('lut3d=file=')

    build_assets.main([str(tmp_path / 'sel.json'), str(src), str(out)])  # a re-run keeps what is built
    assert json.loads((out / 'manifest.json').read_text()) == manifest

    sent = []
    monkeypatch.setattr(build_assets.urllib.request, 'urlopen', lambda req, timeout: sent.append(req.full_url) or io.BytesIO())
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'acct')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'token')
    build_assets.main([str(tmp_path / 'sel.json'), str(src), str(out), '--upload'])
    assert len(sent) == 2 * 5 + 2 and sent[-1].endswith('/static/rn-assets/manifest.json')  # files, previews, LICENSES, manifest last
    sent.clear()
    build_assets.main([str(tmp_path / 'sel.json'), str(src), str(out), '--upload'])  # nothing changed: only the manifest
    assert [u.rsplit('/', 1)[1] for u in sent] == ['manifest.json']

    selection['sets'][0]['license'] = 'Mixkit'
    (tmp_path / 'sel.json').write_text(json.dumps(selection))
    with pytest.raises(SystemExit, match='redistribution'):
        build_assets.main([str(tmp_path / 'sel.json'), str(src), str(out)])
