"""v0.7.0: rn capture URL, real screenshots of a live site (full page, sections, a frame-exact scroll video)."""
import functools
import http.server
import threading

import pytest
from PIL import Image

from rn.cli import main
from rn.ff import RnError, probe

pytest.importorskip('playwright')
from rn.capture import _scroll_at, web_command  # noqa: E402
from rn.agent import PROOF_KB, proof
from types import SimpleNamespace
from pathlib import Path

PAGE = '''<!doctype html><meta name=viewport content="width=device-width"><style>
body{margin:0;font:20px sans-serif} section{height:700px} h1,h2{margin:0;padding:20px}
#a{background:#e03030} #b{background:#30a030} #c{background:#3030e0}
#cookie{position:fixed;bottom:0;left:0;right:0;height:200px;background:#ffff00}</style>
<section id=a><h1>Hero</h1></section><section id=b><h2>Pricing</h2></section><section id=c><h2>Reviews</h2></section>
<div id=cookie>We use cookies</div>'''


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp('site')
    (root / 'index.html').write_text(PAGE, encoding='utf-8')
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    try:
        web_command(url, out=str(root / 'probe'), full=True)
    except RnError as error:
        if 'Executable' in str(error) or 'install' in str(error):
            pytest.skip(f'no Chromium for Playwright: {error}')
        raise
    yield url
    server.shutdown()


def colour(image, x, y):
    return image.convert('RGB').getpixel((x, y))


def near(pixel, want, tol=30):
    return all(abs(a - b) <= tol for a, b in zip(pixel, want))


def test_full_page_on_a_phone_with_the_banner_hidden(site, tmp_path):
    result = web_command(site, out=str(tmp_path), full=True, hide=['#cookie'])
    image = Image.open(result['full'])
    assert image.width == 720 and image.height == 2 * 2100  # 360 CSS px at 2x, three 700 px sections
    assert near(colour(image, 20, 300), (224, 48, 48)) and near(colour(image, 20, image.height - 20), (48, 48, 224))


def test_sections_by_heading_and_the_ones_missing(site, tmp_path):
    result = web_command(site, out=str(tmp_path), sections=['pricing', 'Reviews', 'Careers'])
    assert [s['label'] for s in result['sections']] == ['pricing', 'Reviews'] and result['missing'] == ['Careers']
    shot = Image.open(result['sections'][0]['path'])
    assert shot.size == (720, 1400) and near(colour(shot, 20, 700), (48, 160, 48))
    assert 'full' not in result


def test_scroll_video_is_frame_exact_top_to_bottom(site, tmp_path):
    video = tmp_path / 'scroll.mp4'
    result = web_command(site, out=str(tmp_path), scroll_video=str(video), duration=1, fps=30, hide=['#cookie'])
    info = probe(str(video), exact=True)['video']
    assert info['frames'] == 30 and (info['width'], info['height']) == (720, 1280)
    assert result['scroll_video']['scrolled_px'] == (2100 - 640) * 2
    assert _scroll_at(0, 30, 100) == 0 and _scroll_at(29, 30, 100) == 100 and _scroll_at(15, 30, 100) == pytest.approx(50, abs=8)
    first, last = tmp_path / 'f.png', tmp_path / 'l.png'
    assert main(['frame', str(video), '--index', '0', '-o', str(first)]) == 0
    assert main(['frame', str(video), '--last', '-o', str(last)]) == 0
    assert near(colour(Image.open(first), 360, 640), (224, 48, 48)) and near(colour(Image.open(last), 360, 640), (48, 48, 224))
    shown = proof(SimpleNamespace(command='capture', page=site, out=str(tmp_path), no_proof=False), result)
    assert Path(shown).exists() and Path(shown).stat().st_size <= PROOF_KB * 1024


def test_desktop_and_the_page_mode_still_needs_frames(site, tmp_path, capsys):
    assert main(['capture', site, '-o', str(tmp_path / 'd'), '--desktop']) == 0
    assert Image.open(tmp_path / 'd' / 'full.png').width == 1280
    assert 'desktop 1280x720 CSS px' in capsys.readouterr().out
    page = tmp_path / 'p.html'
    page.write_text('<script>window.renderFrame = () => {}</script>')
    assert main(['capture', str(page), '-o', str(tmp_path / 'p')]) == 1
    assert '--frames' in capsys.readouterr().err
