"""rn assets: licensed sound effects and real overlay footage (film grain scans, light leaks, dust, paper) from the
Renoise asset library on the CDN. Not baked into the image: the files are large, each needs its licence recorded, and a
template upgrade replaces every user's sandbox, so content that changes often must not ride on the image.

Manifest (JSON at RN_ASSETS_URL): {"version": 1, "assets": [{"id", "kind": "sfx"|"overlay"|"texture"|"music"|"lut",
"title", "tags": [...], "category"?, "description"?, "duration"?, "width"?, "height"?, "license", "source", "url"
(absolute or relative to the manifest), "sha256", "bytes", "preview"?, "use"?}]}. Every file is checked against its sha256
and cached by it.

An agent cannot watch the footage, so the manifest carries what it needs to choose and use an asset without opening it:
- "category" groups the catalog (`rn assets search` with no words counts each kind's categories; default: the first tag);
  "description" is searched with the id, title and tags, so it should say what the asset looks or sounds like and what for.
- "preview": a small JPEG (one still, or a strip of frames) that search tiles into its proof image.
- "use": the EDL object to drop in, with "{src}" where the downloaded file's path goes, e.g. a light leak
  {"src": "{src}", "style": {"mix-blend-mode": "screen"}}. Without it, `get` writes a plain one for the kind.
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

from . import __version__
from .ff import RnError

DEFAULT_URL = 'https://assets.renoise.ai/static/rn-assets/manifest.json'
KINDS = ('sfx', 'overlay', 'texture', 'music', 'lut')
PREVIEWS = 8  # tiles in the search proof
# Where a kind's file goes in an EDL, and the plain object `get` writes when the manifest gives no "use"
PLACES = {'sfx': 'audio item', 'music': 'audio item', 'lut': 'clip', 'overlay': 'overlay clip', 'texture': 'overlay clip'}


def _url() -> str:
    return os.environ.get('RN_ASSETS_URL', DEFAULT_URL)


def _cache() -> Path:
    return Path(os.environ.get('RN_ASSETS_CACHE', Path.home() / '.cache' / 'rn-assets'))


def _open(url: str):
    try:
        # Cloudflare in front of assets.renoise.ai refuses urllib's default User-Agent (Python-urllib/x.y) with a 403
        return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': f'rn/{__version__}'}), timeout=30)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RnError(f'the asset library is not published at {url} yet (404)') from None
        if error.code == 403:
            raise RnError(f'{url}: refused (403)') from None
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


def _category(asset: dict) -> str:
    return asset.get('category') or (asset.get('tags') or ['other'])[0]


def search_command(query: str | None, *, kind: str | None, limit: int) -> dict:
    if kind and kind not in KINDS:
        raise RnError(f'--kind: one of {", ".join(KINDS)}')
    url, assets = manifest()
    words = [w for w in (query or '').lower().split() if w]
    scored = []
    catalog: dict[str, dict[str, int]] = {}
    for asset in assets:
        if kind and asset.get('kind') != kind:
            continue
        per_kind = catalog.setdefault(asset.get('kind', '?'), {})
        per_kind[_category(asset)] = per_kind.get(_category(asset), 0) + 1
        haystack = ' '.join([asset.get('id', ''), asset.get('title', ''), asset.get('description', ''), *asset.get('tags', [])]).lower()
        score = sum(haystack.count(w) for w in words)
        if words and not score:
            continue
        scored.append((score, asset))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get('id', '')))
    keep = ('id', 'kind', 'category', 'title', 'description', 'tags', 'duration', 'width', 'height', 'license')
    shown = [a for _, a in scored[:limit]]
    result = {'manifest': url, 'total': len(scored), 'assets': [{k: a[k] for k in keep if k in a} for a in shown],
              'previews': [{'id': a['id'], 'url': urllib.parse.urljoin(url, a['preview'])} for a in shown if a.get('preview')][:PREVIEWS]}
    if not words:  # what the library holds, so the next search uses its words
        result['catalog'] = {k: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for k, v in sorted(catalog.items())}
    return result


def fetch_preview(url: str) -> Path:
    """A preview JPEG, cached by its URL (previews carry no checksum: they are only looked at)."""
    cached = _cache() / 'previews' / f'{hashlib.sha256(url.encode()).hexdigest()[:24]}.jpg'
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        with _open(url) as response:
            cached.write_bytes(response.read())
    return cached


def _filled(value, src: str):
    if isinstance(value, str):
        return value.replace('{src}', src)
    if isinstance(value, dict):
        return {k: _filled(v, src) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v, src) for v in value]
    return value


def edl_use(asset: dict, path: str) -> dict:
    """The EDL object for a downloaded asset: the manifest's "use" with the path filled in, else a plain one for its kind."""
    kind = asset.get('kind', 'overlay')
    if isinstance(asset.get('use'), dict):
        edl = _filled(asset['use'], path)
    elif kind in ('sfx', 'music'):
        edl = {'src': path, 'role': kind}
    elif kind == 'lut':
        edl = {'vf': f'lut3d=file={path}'}
    else:
        edl = {'src': path}
    return {'place': PLACES.get(kind, 'overlay clip'), 'edl': edl}


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
        target.parent.mkdir(parents=True, exist_ok=True)  # ids may be "<category>/<name>"
        shutil.copyfile(cached, target)
        path = target.as_posix()  # EDL paths and ffmpeg filter arguments take forward slashes on every OS
        files.append({'id': asset_id, 'path': path, 'license': asset.get('license'), 'source': asset.get('source'),
                      **edl_use(asset, path)})
    return {'files': files}


def summary_lines(result: dict) -> list[str]:
    if 'files' in result:
        return [f'{f["path"]}  ({f["license"]})' for f in result['files']]
    lines = [f'{k}: ' + ', '.join(f'{c} {n}' for c, n in cats.items()) for k, cats in result.get('catalog', {}).items()]
    lines += [f'{a["id"]}  [{a["kind"]}] {a.get("title", "")}' + (f'  {a["duration"]}s' if a.get('duration') else '')
              + f'  tags: {", ".join(a.get("tags", []))}' for a in result['assets']]
    lines.append(f'{len(result["assets"])} of {result["total"]} matches')
    return lines
