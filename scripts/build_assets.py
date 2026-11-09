"""Build the online asset library that `rn assets` reads (rn/assets.py) from local source folders. Not shipped in the wheel.

    uv run python scripts/build_assets.py SELECTION.json SOURCES_DIR OUT_DIR [--jobs N] [--lut-sample IMG] [--upload]

SELECTION.json lists the sets to publish, each with its licence recorded:
  {"sets": [{"dir": "sfx/kenney-interface/Audio", "glob": "*.ogg", "kind": "sfx", "category": "ui",
             "license": "CC0-1.0", "source": "https://kenney.nl/assets/interface-sounds", "author": "Kenney",
             "tags": ["ui", "click"], "description": "{title}: a short interface sound for taps and toggles",
             "strip": "regex removed from the file name before it becomes the title", "max": 10,
             "use": {"src": "{src}", "style": {"mix-blend-mode": "screen"}},
             "files": {"<file name>": {"title", "description", "tags", "skip": true}}}]}
A set's description is a template for every file in it: name no examples there ("spark, star, smoke"), or a search
for one of them matches the whole set; say what a single file is in `files`. `dir` is relative to SOURCES_DIR; zips in it are unpacked next to themselves first. Only licences that allow
redistribution are accepted (LICENSES below): the library is a public download.

OUT_DIR gets manifest.json, LICENSES.md, <kind>/<category>/<name>.<ext> and previews/<kind>/<category>/<name>.jpg, laid
out to be synced as is under the manifest's URL (static/rn-assets/ on the CDN). Files already built are kept, so a
re-run only encodes what is new; delete OUT_DIR/<kind> to rebuild a kind.

--upload then puts the library on the CDN: R2 bucket renoise-assets under static/rn-assets/, served at
https://assets.renoise.ai/static/rn-assets/manifest.json (rn's default RN_ASSETS_URL), through the Cloudflare REST API with
CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (R2 write) from the environment. Only files changed since the last upload
go (OUT_DIR/.uploaded.json), and the manifest goes last, so it never names a file that is not there yet; an interrupted
upload resumes on the next run. Assets dropped from the selection leave the manifest but stay in the bucket.

Encoding: sfx as build_pack (silence trimmed, peak <= -1 dBFS, Ogg Opus 96 kb/s, onset / peak measured); music Ogg Opus
160 kb/s; overlays H.264 CRF 20, at most 1920 wide, no sound, faststart; textures JPEG (PNG when they have alpha), at
most 2048 px; LUTs (.cube) copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from build_pack import encode as encode_sfx, measure as measure_sfx  # noqa: E402

KINDS = ('sfx', 'overlay', 'texture', 'music', 'lut')
EXT = {'sfx': '.ogg', 'music': '.ogg', 'overlay': '.mp4', 'lut': '.cube'}
LICENSES = {'CC0-1.0', 'CC-BY-4.0', 'CC-BY-3.0', 'MIT', 'ISC', 'Apache-2.0', 'BSD-3-Clause', 'OFL-1.1', 'Unlicense'}
PREVIEW_H = 160
R2_OBJECTS = 'https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/renoise-assets/objects/static/rn-assets/'


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-y', *args], check=True, cwd=cwd)


def probe(path: Path) -> dict:
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,width,height',
                          '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    info = {}
    if (d := data.get('format', {}).get('duration')) not in (None, 'N/A'):
        info['duration'] = round(float(d), 3)
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if video:
        info.update(width=video['width'], height=video['height'])
    return info


def slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'asset'


def check_set(s: dict, n: int) -> None:
    where = f'sets[{n}] ({s.get("dir")})'
    for key in ('dir', 'glob', 'kind', 'category', 'license', 'source'):
        if not s.get(key):
            raise SystemExit(f'{where}: "{key}" is required')
    if s['kind'] not in KINDS:
        raise SystemExit(f'{where}: kind must be one of {", ".join(KINDS)}')
    if s['license'] not in LICENSES:
        raise SystemExit(f'{where}: licence {s["license"]!r} is not one that allows redistribution ({", ".join(sorted(LICENSES))})')


def collect(selection: dict, sources: Path) -> list[dict]:
    """Every file to publish, with its id and destination decided; ids are unique across the library."""
    jobs, seen = [], set()
    for n, s in enumerate(selection['sets']):
        check_set(s, n)
        folder = sources / s['dir']
        for archive in folder.glob('*.zip'):
            if not archive.with_suffix('').exists():
                with zipfile.ZipFile(archive) as z:
                    z.extractall(archive.with_suffix(''))
        files = sorted(p for p in folder.glob(s['glob']) if p.is_file())
        if not files:
            raise SystemExit(f'sets[{n}]: no file matches {s["dir"]}/{s["glob"]}')
        for src in files:
            extra = s.get('files', {}).get(src.name, {})
            if extra.get('skip'):
                continue
            stem = re.sub(s['strip'], '', src.stem) if s.get('strip') else src.stem
            stem = re.sub(r'(?<=[a-z])(?=[A-Z0-9])', ' ', stem)  # beltHandle1 -> belt Handle 1
            title = extra.get('title') or re.sub(r'[_\-.]+', ' ', stem).strip()
            name, k = slug(stem), 2
            while f'{s["category"]}/{name}' in seen:
                name, k = f'{slug(stem)}-{k}', k + 1
            asset_id = f'{s["category"]}/{name}'
            seen.add(asset_id)
            ext = EXT.get(s['kind']) or ('.png' if has_alpha(src) else '.jpg')
            rel = f'{s["kind"]}/{asset_id}{ext}'
            jobs.append({'set': s, 'src': src, 'id': asset_id, 'title': title, 'rel': rel,
                         'preview': f'previews/{s["kind"]}/{asset_id}.jpg',
                         'description': (extra.get('description') or s.get('description') or '{title}: {category} {kind}').format(
                             title=title, category=s['category'], kind=s['kind']),
                         'tags': list(dict.fromkeys([*s.get('tags', []), *extra.get('tags', []), s['category']]))})
    return jobs


def has_alpha(path: Path) -> bool:
    with Image.open(path) as im:
        return im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info)


def build_file(job: dict, out: Path) -> None:
    s, src, dest = job['set'], job['src'], out / job['rel']
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f'.{dest.name}')  # half-written files never look built
    kind = s['kind']
    if kind == 'sfx':
        tmp = tmp.with_suffix('.tmp.ogg')
        encode_sfx(src, tmp, float(s.get('max', 10)))
    elif kind == 'music':
        run('-i', str(src), '-vn', '-ar', '48000', '-c:a', 'libopus', '-b:a', '160k', '-f', 'ogg', str(tmp))
    elif kind == 'overlay':
        run('-i', str(src), '-an', '-vf', "scale='min(1920,iw)':-2", '-c:v', 'libx264', '-crf', '20', '-preset', 'slow',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-f', 'mp4', str(tmp))
    elif kind == 'texture':
        with Image.open(src) as im:
            im.thumbnail((2048, 2048))
            if dest.suffix == '.png':
                im.convert('RGBA').save(tmp, 'PNG', optimize=True)
            else:
                im.convert('RGB').save(tmp, 'JPEG', quality=90)
    else:
        shutil.copyfile(src, tmp)
    tmp.replace(dest)


def build_preview(job: dict, out: Path, lut_sample: Path | None) -> None:
    dest = out / job['preview']
    if dest.exists() and dest.stat().st_mtime >= (out / job['rel']).stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, kind = out / job['rel'], job['set']['kind']
    if kind == 'overlay':  # the brightest 4 of 12 frames, in order: most overlays are dark between their moments
        info = probe(src)
        w = round(info['width'] * PREVIEW_H / info['height'] / 2) * 2
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(src), '-vf', f'fps={12 / max(info.get("duration", 1), 0.1):.4f},'
                              f'scale={w}:{PREVIEW_H}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
        frames = np.frombuffer(raw, np.uint8).reshape(-1, PREVIEW_H, w, 3)
        keep = sorted(np.argsort(frames.reshape(len(frames), -1).mean(axis=1))[-4:])
        Image.fromarray(np.hstack(frames[keep])).save(dest, 'JPEG', quality=80)
    elif kind in ('sfx', 'music'):
        run('-i', str(src), '-filter_complex', f'apad=whole_len=640,showwavespic=s=640x{PREVIEW_H // 2}:colors=white,format=yuvj420p', '-frames:v', '1', '-q:v', '4', str(dest))
    elif kind == 'texture':
        with Image.open(src) as im:
            im.thumbnail((PREVIEW_H * 3, PREVIEW_H))
            bg = Image.new('RGB', im.size, (128, 128, 128))  # alpha shown over grey
            bg.paste(im, mask=im.convert('RGBA').getchannel('A'))
            bg.save(dest, 'JPEG', quality=80)
    else:  # the sample, then the sample through the LUT; run from the LUT's folder so the filter sees a bare name
        sample = ['-i', str(lut_sample.resolve())] if lut_sample else ['-f', 'lavfi', '-i', f'smptehdbars=s={PREVIEW_H * 16 // 9}x{PREVIEW_H}']
        run(*sample, '-filter_complex', f'[0]scale=-2:{PREVIEW_H},format=rgb24,split[a][b];[b]lut3d=file={src.name}[c];[a][c]hstack,format=yuvj420p',
            '-frames:v', '1', '-q:v', '4', str(dest.resolve()), cwd=src.parent)


def entry(job: dict, out: Path) -> dict:
    s, dest = job['set'], out / job['rel']
    data = dest.read_bytes()
    item = {'id': job['id'], 'kind': s['kind'], 'category': s['category'], 'title': job['title'],
            'description': job['description'], 'tags': job['tags']}
    if s['kind'] == 'sfx':
        item.update(measure_sfx(dest))  # duration, onset, peak (the second to put on a cut), peak_db, rms_db
    elif s['kind'] == 'texture':
        with Image.open(dest) as im:
            item.update(width=im.width, height=im.height)
    elif s['kind'] in ('music', 'overlay'):
        item.update(probe(dest))
    item.update({'license': s['license'], 'source': s['source'], 'author': s.get('author'), 'url': job['rel'],
                 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'preview': job['preview']})
    if s.get('use'):
        item['use'] = s['use']
    return {k: v for k, v in item.items() if v is not None}


def write_credits(selection: dict, out: Path) -> None:
    lines = ['# Renoise asset library: sources and licences', '', '| set | kind | author | licence | source |', '|---|---|---|---|---|']
    lines += [f'| {s["dir"]} | {s["kind"]} | {s.get("author", "")} | {s["license"]} | {s["source"]} |' for s in selection['sets']]
    lines += ['', 'Re-encoded (see scripts/build_assets.py in renoise-kit); otherwise unchanged.', '']
    text, path = '\n'.join(lines), out / 'LICENSES.md'
    if not path.exists() or path.read_text(encoding='utf-8') != text:  # unchanged keeps its mtime, so --upload skips it
        path.write_text(text, encoding='utf-8')


def upload(out: Path, manifest: dict) -> int:
    account, token = os.environ.get('CLOUDFLARE_ACCOUNT_ID'), os.environ.get('CLOUDFLARE_API_TOKEN')
    if not (account and token):
        raise SystemExit('--upload: set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (a token with R2 write)')
    base = R2_OBJECTS.format(account=account)
    record_path = out / '.uploaded.json'
    record = json.loads(record_path.read_text()) if record_path.exists() else {}

    def stamp(rel: str) -> str:
        st = (out / rel).stat()
        return f'{st.st_size}:{st.st_mtime_ns}'

    def put(rel: str) -> str:
        # ponytail: one PUT per file, fine for files up to tens of MB; large 4K footage would want R2's S3 multipart API
        req = urllib.request.Request(base + urllib.parse.quote(rel), data=(out / rel).read_bytes(), method='PUT', headers={
            'Authorization': f'Bearer {token}', 'Content-Type': mimetypes.guess_type(rel)[0] or 'application/octet-stream'})
        for _ in range(3):  # a request now and then stalls on the API: give it up after 60 s and send it again
            try:
                urllib.request.urlopen(req, timeout=60).close()
                return rel
            except urllib.error.HTTPError as error:
                if error.code < 500 and error.code != 429:
                    raise SystemExit(f'upload {rel}: HTTP {error.code} {error.read()[:300]!r}') from None
                problem = f'HTTP {error.code}'
            except (urllib.error.URLError, OSError) as error:
                problem = getattr(error, 'reason', error)
        raise SystemExit(f'upload {rel}: {problem} (3 tries; run again to resume)')

    files = [*(a['url'] for a in manifest['assets']), *(a['preview'] for a in manifest['assets']), 'LICENSES.md']
    todo = [rel for rel in files if record.get(rel) != stamp(rel)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for n, rel in enumerate(pool.map(put, todo), 1):
                record[rel] = stamp(rel)
                if n % 50 == 0:
                    print(f'uploaded {n} of {len(todo)}', flush=True)
        put('manifest.json')
    finally:
        record_path.write_text(json.dumps(record, indent=0, sort_keys=True) + '\n')
    return len(todo)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('selection', type=Path)
    ap.add_argument('sources', type=Path)
    ap.add_argument('out', type=Path)
    ap.add_argument('--jobs', type=int, default=4, help='files encoded at once (ffmpeg uses several threads each)')
    ap.add_argument('--upload', action='store_true', help='then put the library on the CDN (see above)')
    ap.add_argument('--lut-sample', type=Path, help='image each LUT preview shows before / after (default: colour bars)')
    args = ap.parse_args(argv)
    selection = json.loads(args.selection.read_text(encoding='utf-8'))
    jobs = collect(selection, args.sources)
    args.out.mkdir(parents=True, exist_ok=True)

    def one(job: dict) -> dict:
        try:
            build_file(job, args.out)
            build_preview(job, args.out, args.lut_sample)
            return entry(job, args.out)
        except (subprocess.CalledProcessError, OSError) as error:
            raise SystemExit(f'{job["src"]}: {error}') from None

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        assets = list(pool.map(one, jobs))
    manifest = {'version': 1, 'assets': sorted(assets, key=lambda a: (a['kind'], a['id']))}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    write_credits(selection, args.out)
    counts = {k: sum(a['kind'] == k for a in assets) for k in KINDS}
    total = sum(a['bytes'] for a in assets)
    print(', '.join(f'{n} {k}' for k, n in counts.items() if n) + f'; {total / 1e6:.1f} MB in {args.out}')
    if args.upload:
        sent = upload(args.out, manifest)
        print(f'uploaded {sent} changed file(s) and the manifest: https://assets.renoise.ai/static/rn-assets/manifest.json')


if __name__ == '__main__':
    main()
