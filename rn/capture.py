"""rn capture: render an HTML page frame by frame to an RGBA PNG sequence (the EDL takes it as {"frames": ...}).

Contract (after Katana's render.py + page.html): the page defines `window.renderFrame(i)` (may return a Promise) that
draws frame i completely; the frame index IS the time, so nothing depends on the wall clock and any frame renders the
same in any order or shard. Before the page's scripts run, `window.RN = {fps, frames, width, height, shard, params}` is set
and the effects library is available as `window.RNFX` (rn fx). An optional `window.rnReady` Promise is awaited once
(fonts, images, data). The page background stays transparent unless the page paints it.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

from .ff import RnError

READY_TIMEOUT_MS = 30_000
FRAME_TIMEOUT_MS = 20_000


PACK_FONTS = 'https://rn.pack/fonts/'  # answered from rn/packs/fonts by a route, never fetched from the network


def _init_script(fps: float, frames: int, width: int, height: int, shard: int, params: dict | None = None) -> str:
    from .fx import js_path
    from .pack import font_faces
    faces = [{'family': f['family'], 'url': PACK_FONTS + Path(f['file']).name, 'weight': f['weight']} for f in font_faces()]
    meta = json.dumps({'fps': fps, 'frames': frames, 'width': width, 'height': height, 'shard': shard, 'params': params or {},
                       'fonts': sorted({f['family'] for f in faces})})
    # Every rn pack font is registered and loaded before the page's frames, so canvas text can use it at once.
    fonts = ('window.rnPackFonts = Promise.all(' + json.dumps(faces) + '.map(f => { const face = new FontFace(f.family, '
             '`url(${f.url})`, {weight: f.weight}); document.fonts.add(face); return face.load().catch(() => null); }));\n')
    return f'window.RN = {meta};\n' + fonts + js_path().read_text(encoding='utf-8')


def _serve_pack_font(route) -> None:
    from .pack import font_dir
    path = font_dir() / route.request.url.removeprefix(PACK_FONTS).split('?')[0]
    if path.parent != font_dir() or not path.is_file():
        route.fulfill(status=404)
        return
    route.fulfill(status=200, body=path.read_bytes(), headers={'Content-Type': 'font/ttf', 'Access-Control-Allow-Origin': '*'})


def _shard(args: tuple) -> dict:
    url, out_dir, indices, fps, frames, width, height, shard, scale, params = args
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    errors: list[str] = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-gpu', '--font-render-hinting=none'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=scale)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route(PACK_FONTS + '**', _serve_pack_font)
            page.add_init_script(_init_script(fps, frames, width, height, shard, params))
            page.goto(url, wait_until='load', timeout=READY_TIMEOUT_MS)
            page.evaluate('async () => { await window.rnPackFonts; if (window.rnReady) await window.rnReady; await document.fonts.ready; }')
            if not page.evaluate('typeof window.renderFrame === "function"'):
                raise RnError(f'{url}: the page does not define window.renderFrame(i)')
            for index in indices:
                page.evaluate('async (i) => { await window.renderFrame(i); }', index)
                page.screenshot(path=str(Path(out_dir) / f'{index + 1:05d}.png'), omit_background=True,
                                timeout=FRAME_TIMEOUT_MS, animations='disabled')
                if errors:
                    raise RnError(f'frame {index}: page error: {errors[0]}')
        except PlaywrightError as error:
            raise RnError(f'shard {shard}: {str(error).splitlines()[0]}') from None
        finally:
            browser.close()
    return {'shard': shard, 'frames': len(indices), 'seconds': round(time.monotonic() - started, 2)}


def capture_command(page: str, *, out: str, frames: int, width: int, height: int, fps: float, shards: int,
                    start: int = 0, scale: float = 1.0, params: dict | None = None) -> dict:
    source = Path(page)
    if not source.exists():
        raise RnError(f'no such page: {page}')
    if frames < 1 or width < 2 or height < 2:
        raise RnError('--frames, --size must be positive')
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RnError('rn capture needs playwright (extra "capture"): uv tool install "renoise-kit[all]" && playwright install chromium') from None
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('*.png'):
        stale.unlink()
    indices = list(range(start, start + frames))
    shards = max(1, min(shards, len(indices)))
    # Contiguous runs per shard: each Chromium boots once (~1-3 s), so short renders gain nothing from more shards.
    size = -(-len(indices) // shards)
    jobs = [(source.resolve().as_uri(), str(out_dir), indices[k * size:(k + 1) * size], fps, start + frames, width, height, k, scale, params)
            for k in range(shards) if indices[k * size:(k + 1) * size]]
    started = time.monotonic()
    if len(jobs) == 1:
        reports = [_shard(jobs[0])]
    else:
        with mp.get_context('spawn').Pool(len(jobs)) as pool:
            reports = pool.map(_shard, jobs)
    written = sorted(out_dir.glob('*.png'))
    if len(written) != frames:
        raise RnError(f'wrote {len(written)} frames, expected {frames}')
    elapsed = time.monotonic() - started
    return {'dir': str(out_dir), 'pattern': str(out_dir / '%05d.png'), 'first_number': start + 1, 'frames': frames,
            'fps': fps, 'size': [round(width * scale), round(height * scale)], 'shards': reports,
            'seconds': round(elapsed, 2), 'fps_rendered': round(frames / elapsed, 1) if elapsed else None,
            'edl': {'frames': f'{out_dir.name}/%05d.png', 'fps': fps}}


def summary_lines(result: dict) -> list[str]:
    return [f'{result["pattern"]}  {result["frames"]} frames from #{result["first_number"]:05d}  '
            f'{result["size"][0]}x{result["size"][1]}  {result["seconds"]}s ({result["fps_rendered"]} fps, {len(result["shards"])} shards)',
            f'EDL: {json.dumps(result["edl"])}']


# --- live web pages: rn capture URL -------------------------------------------------------------------------------
# A real browser at a phone or desktop size, for app / SaaS ads: the full page, sections found by their heading text,
# and a scroll-through video whose scroll position is a function of the frame index (frame-exact, any speed).
WEB_SCALE = {'mobile': 3.0, 'desktop': 1.5}  # device pixels per CSS pixel: 1080x1920 -> a 360x640 phone viewport
WEB_SIZE = {'mobile': (1080, 1920), 'desktop': (1920, 1080)}
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
             'Version/17.5 Mobile/15E148 Safari/604.1')
HOLD = 0.12  # share of the scroll video held still at the top and at the bottom


def _slug(text: str) -> str:
    import re
    return re.sub(r'[^\w]+', '-', text.strip().lower()).strip('-') or 'section'


def _scroll_at(i: int, frames: int, distance: float) -> float:
    """Scroll offset of frame i: held at the top, an ease-in-out run, held at the bottom."""
    if frames < 2:
        return 0.0
    u = min(max((i / (frames - 1) - HOLD) / (1 - 2 * HOLD), 0.0), 1.0)
    return distance * u * u * (3 - 2 * u)


def web_command(url: str, *, out: str, device: str = 'mobile', size: tuple[int, int] | None = None, full: bool = False,
                sections: list[str] | None = None, scroll_video: str | None = None, duration: float = 8.0, fps: float = 30,
                hide: list[str] | None = None) -> dict:
    import subprocess
    import tempfile
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise RnError('rn capture needs playwright (extra "capture"): uv tool install "renoise-kit[all]" && playwright install chromium') from None
    width, height = size or WEB_SIZE[device]
    scale = WEB_SCALE[device]
    viewport = {'width': round(width / scale), 'height': round(height / scale)}
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (full or sections or scroll_video):
        full = True
    result: dict = {'url': url, 'device': device, 'viewport': viewport, 'scale': scale, 'size': [width, height],
                    'dir': str(out_dir), 'sections': [], 'missing': []}
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-gpu', '--font-render-hinting=none', '--hide-scrollbars'])
        try:
            context = browser.new_context(viewport=viewport, device_scale_factor=scale, is_mobile=device == 'mobile',
                                          has_touch=device == 'mobile', user_agent=MOBILE_UA if device == 'mobile' else None)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                page.goto(url, wait_until='networkidle', timeout=READY_TIMEOUT_MS)
            except PlaywrightError:  # pages that never go quiet (analytics, sockets): take what loaded
                page.goto(url, wait_until='load', timeout=READY_TIMEOUT_MS)
            css = 'html{scroll-behavior:auto !important}' + ''.join(f'{s}{{display:none !important}}' for s in hide or [])
            page.add_style_tag(content=css)
            # Walk down once so lazy images and scroll-triggered content load, then back to the top.
            page.evaluate('''async () => { const step = innerHeight * 0.8;
                for (let y = 0; y < document.documentElement.scrollHeight; y += step) { scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
                scrollTo(0, 0); await document.fonts.ready; }''')
            page.wait_for_timeout(300)
            page_height = page.evaluate('document.documentElement.scrollHeight')
            result['page_height'] = page_height
            if full:
                target = out_dir / 'full.png'
                page.screenshot(path=str(target), full_page=True, animations='disabled')
                result['full'] = str(target)
            for label in sections or []:
                handle = page.evaluate_handle('''(label) => {
                    const want = label.trim().toLowerCase();
                    const all = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading],summary,legend,strong,b,p,span,a,div')];
                    const own = el => [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim().toLowerCase();
                    const hit = all.find(el => /^H[1-6]$/.test(el.tagName) && el.innerText.trim().toLowerCase().includes(want))
                             || all.find(el => own(el).includes(want));
                    if (!hit) return null;
                    return hit.closest('section,article,header,footer,aside,main > div') || hit.parentElement || hit; }''', label)
                element = handle.as_element()
                if element is None:
                    result['missing'].append(label)
                    continue
                target = out_dir / f'section-{_slug(label)}.png'
                element.scroll_into_view_if_needed()
                element.screenshot(path=str(target), animations='disabled')
                box = element.bounding_box() or {}
                result['sections'].append({'label': label, 'path': str(target),
                                           'height_px': round((box.get('height') or 0) * scale)})
            if scroll_video:
                frames = max(2, round(duration * fps))
                distance = max(0, page_height - viewport['height'])
                with tempfile.TemporaryDirectory() as tmp:
                    for i in range(frames):
                        page.evaluate('(y) => new Promise(r => { scrollTo(0, y); requestAnimationFrame(() => requestAnimationFrame(r)); })',
                                      _scroll_at(i, frames, distance))
                        page.screenshot(path=str(Path(tmp) / f'{i + 1:05d}.png'), animations='disabled')
                    Path(scroll_video).parent.mkdir(parents=True, exist_ok=True)
                    cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-framerate', str(fps), '-i', str(Path(tmp) / '%05d.png'),
                           '-vf', f'scale={width}:{height}:flags=lanczos,format=yuv420p', '-c:v', 'libx264', '-crf', '16',
                           '-preset', 'medium', '-movflags', '+faststart', '-y', scroll_video]
                    encoded = subprocess.run(cmd, capture_output=True, text=True)
                    if encoded.returncode:
                        raise RnError(f'encoding {scroll_video}: {encoded.stderr.strip()[-300:]}')
                result['scroll_video'] = {'path': scroll_video, 'frames': frames, 'fps': fps, 'seconds': round(frames / fps, 3),
                                          'scrolled_px': round(distance * scale)}
        except PlaywrightError as error:
            raise RnError(f'{url}: {str(error).splitlines()[0]}') from None
        finally:
            browser.close()
    result['page_errors'] = errors[:5]
    return result


def web_summary_lines(result: dict) -> list[str]:
    lines = [f'{result["url"]}  {result["device"]} {result["viewport"]["width"]}x{result["viewport"]["height"]} CSS px '
             f'x{result["scale"]:g}  page {result["page_height"]} CSS px tall']
    if result.get('full'):
        lines.append(f'  full page: {result["full"]}')
    lines += [f'  section "{s["label"]}": {s["path"]} ({s["height_px"]} px tall)' for s in result['sections']]
    if result['missing']:
        lines.append(f'  not found on the page: {", ".join(result["missing"])} (match heading text as shown)')
    if result.get('scroll_video'):
        video = result['scroll_video']
        lines.append(f'  scroll video: {video["path"]}  {video["frames"]} frames at {video["fps"]:g} fps, '
                     f'{video["scrolled_px"]} px scrolled' + ('  (page shorter than the screen: no scroll)' if not video['scrolled_px'] else ''))
    lines += [f'warning: page error: {e}' for e in result['page_errors']]
    return lines
