"""rn capture: render an HTML page frame by frame to an RGBA PNG sequence (the EDL takes it as {"frames": ...}).

Contract (after Katana's render.py + page.html): the page defines `window.renderFrame(i)` (may return a Promise) that
draws frame i completely; the frame index IS the time, so nothing depends on the wall clock and any frame renders the
same in any order or shard. Before the page's scripts run, `window.RN = {fps, frames, width, height, shard}` is set
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


def _init_script(fps: float, frames: int, width: int, height: int, shard: int) -> str:
    from .fx import js_path
    meta = json.dumps({'fps': fps, 'frames': frames, 'width': width, 'height': height, 'shard': shard})
    return f'window.RN = {meta};\n' + js_path().read_text()


def _shard(args: tuple) -> dict:
    url, out_dir, indices, fps, frames, width, height, shard, scale = args
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    errors: list[str] = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-gpu', '--font-render-hinting=none'])
        try:
            page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=scale)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script(_init_script(fps, frames, width, height, shard))
            page.goto(url, wait_until='load', timeout=READY_TIMEOUT_MS)
            page.evaluate('async () => { if (window.rnReady) await window.rnReady; await document.fonts.ready; }')
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
                    start: int = 0, scale: float = 1.0) -> dict:
    source = Path(page)
    if not source.exists():
        raise RnError(f'no such page: {page}')
    if frames < 1 or width < 2 or height < 2:
        raise RnError('--frames, --size must be positive')
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RnError("rn capture needs playwright (extra \"capture\"): uv tool install 'renoise-kit[all]' && playwright install chromium") from None
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('*.png'):
        stale.unlink()
    indices = list(range(start, start + frames))
    shards = max(1, min(shards, len(indices)))
    # Contiguous runs per shard: each Chromium boots once (~1-3 s), so short renders gain nothing from more shards.
    size = -(-len(indices) // shards)
    jobs = [(source.resolve().as_uri(), str(out_dir), indices[k * size:(k + 1) * size], fps, start + frames, width, height, k, scale)
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
