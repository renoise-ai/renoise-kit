"""rn template: built-in motion templates (headline, lower third, callout, counter, bar chart, steps, text behind a
person), rendered with rn capture to an RGBA PNG sequence for an EDL overlay.

Each template is one readable HTML page in rn/templates/ following the rn capture contract (window.renderFrame(i)).
Its parameters, defaults and docs live in a JSON block at the top of the page; `params` override them, and the page
reads them as window.RN.params. Copy a page (rn template show NAME prints its path) to change the design.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .ff import RnError

TEMPLATES = Path(__file__).with_name('templates')


def _names() -> list[str]:
    return sorted(p.stem for p in TEMPLATES.glob('*.html'))


def _spec(name: str) -> dict:
    path = TEMPLATES / f'{name}.html'
    if not path.exists():
        raise RnError(f'no template "{name}"; rn template list shows: {", ".join(_names())}')
    match = re.search(r'<script type="application/json" id="rn-template">(.*?)</script>', path.read_text(encoding='utf-8'), re.S)
    return {'name': name, 'path': str(path), **json.loads(match.group(1))}


def _params(text: str | None) -> dict:
    if not text:
        return {}
    try:
        raw = Path(text[1:]).read_text(encoding='utf-8-sig') if text.startswith('@') else text
        params = json.loads(raw)
    except (OSError, ValueError) as error:
        raise RnError(f'--params: {error}') from None
    if not isinstance(params, dict):
        raise RnError('--params: a JSON object like {"text": "Hello"}')
    return params


def template_command(action: str, name: str | None, *, out: str | None = None, width: int = 1080, height: int = 1920, fps: float = 30,
                     duration: float = 3.0, params: str | None = None, shards: int = 4) -> dict:
    if action == 'list':
        return {'templates': [{k: v for k, v in _spec(n).items() if k != 'path'} for n in _names()]}
    if not name:
        raise RnError(f'rn template {action} NAME: one of {", ".join(_names())}')
    spec = _spec(name)
    if action == 'show':
        return spec
    if not out:
        raise RnError('rn template render NAME -o DIR')
    given = _params(params)
    unknown = sorted(set(given) - set(spec['params']))
    if unknown:
        raise RnError(f'{name}: unknown params {unknown}; it takes {", ".join(spec["params"])}')
    merged = {k: v['default'] for k, v in spec['params'].items()} | given
    if merged.get('font_file'):
        font = Path(merged['font_file'])
        if not font.exists():
            raise RnError(f'font_file {font} not found')
        merged['font_file'] = font.resolve().as_uri()
    frames = round(duration * fps)
    if frames < 1:
        raise RnError('--duration too short for one frame')
    from .capture import capture_command
    result = capture_command(spec['path'], out=out, frames=frames, width=width, height=height, fps=fps, shards=shards, params=merged)
    return result | {'template': name, 'params': merged}


def summary_lines(result: dict) -> list[str]:
    if 'templates' in result:
        lines = [f'{t["name"]:12} {t["what"]}' for t in result['templates']]
        return lines + ['rn template show NAME: params and the page to copy; rn template render NAME -o DIR --params \'{...}\'']
    if 'pattern' not in result:
        lines = [f'{result["name"]}: {result["what"]}', f'  page {result["path"]}']
        return lines + [f'  {k:12} {json.dumps(v["default"], ensure_ascii=False):28} {v["doc"]}' for k, v in result['params'].items()]
    from .capture import summary_lines as capture_lines
    return [f'template {result["template"]}', *capture_lines(result)]
