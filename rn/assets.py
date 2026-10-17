"""rn assets: licensed sound effects and real overlay footage (film grain scans, light leaks, dust, paper) from the
Renoise asset library on the CDN. Not baked into the image: the files are large, each needs its licence recorded, and a
template upgrade replaces every user's sandbox, so content that changes often must not ride on the image.

Manifest (JSON at RN_ASSETS_URL): {"version": 1, "assets": [{"id", "kind": "sfx"|"overlay"|"texture"|"music", "title",
"tags": [...], "duration"?, "width"?, "height"?, "license", "source", "url" (absolute or relative to the manifest),
"sha256", "bytes"}]}. Every file is checked against its sha256 and cached by it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .ff import RnError

DEFAULT_URL = 'https://assets.renoise.ai/static/rn-assets/manifest.json'
KINDS = ('sfx', 'overlay', 'texture', 'music')


def _url() -> str:
    return os.environ.get('RN_ASSETS_URL', DEFAULT_URL)


def _cache() -> Path:
    return Path(os.environ.get('RN_ASSETS_CACHE', Path.home() / '.cache' / 'rn-assets'))


def _open(url: str):
    try:
        return urllib.request.urlopen(url, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code in (403, 404):
            raise RnError(f'the asset library is not published at {url} yet ({error.code})') from None
        raise RnError(f'{url}: HTTP {error.code}') from None
    except (urllib.error.URLError, OSError) as error:
        raise RnError(f'{url}: {getattr(error, "reason", error)}') from None


def fetch_checked(source: str, digest: str, cached: Path, *, name: str) -> Path:
    """Downloads `source` to `cached` once, refusing content whose sha256 is not `digest`."""
    if cached.exists():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(f'{cached.name}.part')
    hasher = hashlib.sha256()
    with _open(source) as response, open(partial, 'wb') as file:
        while chunk := response.read(1 << 20):
            hasher.update(chunk)
            file.write(chunk)
    if hasher.hexdigest() != digest:
        partial.unlink()
        raise RnError(f'{name}: checksum mismatch, refusing the download')
    partial.replace(cached)  # rename() refuses to overwrite on Windows
    return cached


def manifest() -> tuple[str, list[dict]]:
    url = _url()
    if '://' not in url:
        url = Path(url).resolve().as_uri()
    with _open(url) as response:
        try:
            data = json.loads(response.read())
        except json.JSONDecodeError as error:
            raise RnError(f'{url}: not JSON ({error})') from None
    assets = data.get('assets') if isinstance(data, dict) else None
    if not isinstance(assets, list):
        raise RnError(f'{url}: expected {{"version": 1, "assets": [...]}}')
    return url, assets


def search_command(query: str | None, *, kind: str | None, limit: int) -> dict:
    if kind and kind not in KINDS:
        raise RnError(f'--kind: one of {", ".join(KINDS)}')
    url, assets = manifest()
    words = [w for w in (query or '').lower().split() if w]
    scored = []
    for asset in assets:
        if kind and asset.get('kind') != kind:
            continue
        haystack = ' '.join([asset.get('id', ''), asset.get('title', ''), *asset.get('tags', [])]).lower()
        score = sum(haystack.count(w) for w in words)
        if words and not score:
            continue
        scored.append((score, asset))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get('id', '')))
    keep = ('id', 'kind', 'title', 'tags', 'duration', 'width', 'height', 'license')
    return {'manifest': url, 'total': len(scored), 'assets': [{k: a[k] for k in keep if k in a} for _, a in scored[:limit]]}


def get_command(ids: list[str], *, out: str) -> dict:
    url, assets = manifest()
    by_id = {a.get('id'): a for a in assets}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise RnError(f'unknown asset id(s) {missing}; find ids with rn assets search')
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = _cache()
    cache.mkdir(parents=True, exist_ok=True)
    files = []
    for asset_id in ids:
        asset = by_id[asset_id]
        digest = asset.get('sha256', '')
        source = urllib.parse.urljoin(url, asset['url'])
        cached = fetch_checked(source, digest, cache / digest, name=asset_id)
        target = out_dir / f'{asset_id}{Path(urllib.parse.urlparse(source).path).suffix}'
        shutil.copyfile(cached, target)
        files.append({'id': asset_id, 'path': str(target), 'license': asset.get('license'), 'source': asset.get('source')})
    return {'files': files}


def summary_lines(result: dict) -> list[str]:
    if 'files' in result:
        return [f'{f["path"]}  ({f["license"]})' for f in result['files']]
    lines = [f'{a["id"]}  [{a["kind"]}] {a.get("title", "")}' + (f'  {a["duration"]}s' if a.get('duration') else '')
             + f'  tags: {", ".join(a.get("tags", []))}' for a in result['assets']]
    lines.append(f'{len(result["assets"])} of {result["total"]} matches')
    return lines
