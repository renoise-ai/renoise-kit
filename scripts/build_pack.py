"""Rebuild rn/packs (the offline asset pack) from downloaded sources. Not shipped in the wheel.

    uv run python scripts/build_pack.py SOURCES_DIR

SOURCES_DIR holds what scripts/pack_sources.json names:
  sfx/<opengameart slug>/...   the CC0 packs as downloaded (zips are unpacked here) + meta_<slug>.json
                               ({url, title, licenses, author}) written by the downloader
  fonts/<dir>/<file>.ttf       from github.com/google/fonts (ofl/ or apache/), with OFL.txt / LICENSE.txt
  lucide/package/              the lucide-static npm package (icon-nodes.json, tags.json, LICENSE)

Sound effects are trimmed of leading/trailing silence (-60 dBFS), loudness-capped, encoded as Ogg Opus 96 kb/s 48 kHz and
measured from the encoded file: onset (first sample within 30 dB of the peak), peak (loudest 10 ms window), peak_db,
rms_db, duration. Only CC0 sources are accepted.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'rn' / 'packs'
SOURCES = json.loads((Path(__file__).with_name('pack_sources.json')).read_text(encoding='utf-8'))


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def measure(path: Path) -> dict:
    x = decode(path)
    win = 480  # 10 ms
    n = len(x) // win
    rms = np.sqrt(np.mean(x[:n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    peak_win = int(np.argmax(rms))
    peak_db = 20 * np.log10(float(np.max(np.abs(x))) + 1e-9)
    env = 20 * np.log10(np.abs(x) + 1e-9)
    onset = int(np.argmax(env > peak_db - 30))
    return {'duration': round(len(x) / 48000, 3), 'onset': round(onset / 48000, 3), 'peak': round((peak_win + 0.5) * win / 48000, 3),
            'peak_db': round(peak_db, 1), 'rms_db': round(20 * np.log10(float(np.sqrt(np.mean(x ** 2))) + 1e-9), 1)}


def encode(src: Path, dest: Path, max_seconds: float) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    trim = ('silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.005,'
            'areverse,silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.02,areverse')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-af', f'{trim},atrim=0:{max_seconds},afade=t=out:st={max_seconds - 0.05}:d=0.05,'
                    'alimiter=limit=0.89:level=disabled', '-ar', '48000', '-c:a', 'libopus', '-b:a', '96k', str(dest)], check=True)


def unpack(sources: Path) -> None:
    for archive in sources.glob('sfx/*/*.zip'):
        target = archive.with_suffix('')
        if not target.exists():
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)


def build_sfx(sources: Path) -> list[dict]:
    unpack(sources)
    shutil.rmtree(PACK / 'sfx', ignore_errors=True)
    items, credits = [], {}
    for category, entries in SOURCES['sfx'].items():
        for entry in entries:
            slug, pattern, name = entry['from'], entry['file'], entry['name']
            meta = json.loads((sources / 'sfx' / f'meta_{slug}.json').read_text())
            if 'CC0' not in meta['licenses']:
                raise SystemExit(f'{slug}: not CC0 ({meta["licenses"]})')
            matches = sorted((sources / 'sfx' / slug).rglob(pattern))
            if not matches:
                raise SystemExit(f'{slug}/{pattern}: no file')
            dest = PACK / 'sfx' / category / f'{name}.ogg'
            encode(matches[0], dest, entry.get('max', 4.0))
            data = dest.read_bytes()
            items.append({'id': f'{category}/{name}', 'kind': 'sfx', 'category': category, 'tags': entry.get('tags', []) + [category],
                          'file': dest.relative_to(PACK).as_posix(), **measure(dest), 'license': 'CC0-1.0',
                          'source': meta['url'], 'author': meta['author'], 'original': matches[0].name,
                          'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            credits[slug] = meta
    return items, credits


def build_fonts(sources: Path) -> list[dict]:
    from fontTools.ttLib import TTFont
    shutil.rmtree(PACK / 'fonts', ignore_errors=True)
    (PACK / 'fonts' / 'licenses').mkdir(parents=True)
    items = []
    for entry in SOURCES['fonts']:
        src = sources / 'fonts' / entry['file']
        dest = PACK / 'fonts' / src.name.replace('[', '-').replace(']', '').replace(',', '-')
        shutil.copyfile(src, dest)
        licence = next(p for p in (src.parent / 'OFL.txt', src.parent / 'LICENSE.txt') if p.exists())
        shutil.copyfile(licence, PACK / 'fonts' / 'licenses' / f'{dest.stem}.txt')
        font = TTFont(dest)
        family = font['name'].getBestFamilyName()
        axes = {a.axisTag: [int(a.minValue), int(a.maxValue)] for a in font['fvar'].axes} if 'fvar' in font else {}
        weights = list(range(axes['wght'][0], axes['wght'][1] + 1, 100)) if 'wght' in axes else [font['OS/2'].usWeightClass]
        cmap = font.getBestCmap()
        scripts = [s for s, chars in (('latin', 'AZaz'), ('latin-ext', 'ŁřŐ'), ('cyrillic', 'ЖЯ'), ('greek', 'ΩΣ'), ('vietnamese', 'ỹỗ'))
                   if all(ord(c) in cmap for c in chars)]
        data = dest.read_bytes()
        items.append({'id': f'font/{entry["name"]}', 'kind': 'font', 'category': entry['category'], 'tags': entry['tags'],
                      'family': family, 'weights': weights, 'variable': 'wght' in axes, 'axes': axes, 'scripts': scripts,
                      'description': entry['what'], 'file': dest.relative_to(PACK).as_posix(),
                      'license': 'Apache-2.0' if 'apache' in licence.name.lower() or licence.name == 'LICENSE.txt' else 'OFL-1.1',
                      'license_file': f'fonts/licenses/{dest.stem}.txt', 'source': f'https://github.com/google/fonts/tree/main/{entry["upstream"]}',
                      'author': font['name'].getDebugName(0) or family, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return items


def build_icons(sources: Path) -> dict:
    pkg = sources / 'lucide' / 'package'
    nodes = json.loads((pkg / 'icon-nodes.json').read_text())
    tags = json.loads((pkg / 'tags.json').read_text())
    version = json.loads((pkg / 'package.json').read_text())['version']
    (PACK / 'icons').mkdir(parents=True, exist_ok=True)
    (PACK / 'icons' / 'lucide.json').write_text(json.dumps({'version': version, 'icons': nodes, 'tags': tags},
                                                            separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    shutil.copyfile(pkg / 'LICENSE', PACK / 'icons' / 'LICENSE-lucide.txt')
    return {'version': version, 'count': len(nodes)}


def write_credits(credits: dict, fonts: list[dict], icons: dict) -> None:
    lines = ['# rn pack: sources and licences', '',
             'Every file in rn/packs is free to use in commercial work. Attribution is not required for CC0 and is given here',
             'anyway; the font licences (OFL-1.1, Apache-2.0) and the icon licence (ISC) travel with the files.', '',
             '## Sound effects (CC0-1.0)', '', '| pack | author | source |', '|---|---|---|']
    lines += [f'| {m["title"]} | {m["author"]} | {m["url"]} |' for m in sorted(credits.values(), key=lambda m: m['title'])]
    lines += ['', 'Trimmed of silence and re-encoded (Ogg Opus 96 kb/s); otherwise unchanged.', '',
              '## Fonts', '', '| family | licence | source |', '|---|---|---|']
    lines += [f'| {f["family"]} | {f["license"]} (fonts/licenses/{Path(f["file"]).stem}.txt) | {f["source"]} |' for f in fonts]
    lines += ['', f'## Icons', '', f'Lucide {icons["version"]} ({icons["count"]} icons), ISC licence (icons/LICENSE-lucide.txt), '
              'https://lucide.dev', '', '## Looks', '', 'Generated by rn\'s own code (rn/pack.py LOOKS), MIT like rn.', '']
    (PACK / 'LICENSES.md').write_text('\n'.join(lines), encoding='utf-8')


def main() -> None:
    sources = Path(sys.argv[1])
    old = json.loads((PACK / 'catalog.json').read_text()) if (PACK / 'catalog.json').exists() else {'items': [], 'sfx_sources': {}}
    if SOURCES['sfx'] and (sources / 'sfx').is_dir():
        sfx, credits = build_sfx(sources)
    else:  # keep the sounds already built
        sfx, credits = [i for i in old['items'] if i['kind'] == 'sfx'], old.get('sfx_sources', {})
    fonts = build_fonts(sources)
    icons = build_icons(sources)
    write_credits(credits, fonts, icons)
    (PACK / 'catalog.json').write_text(json.dumps({'version': 1, 'sfx_sources': credits, 'items': sfx + fonts}, indent=1,
                                                  ensure_ascii=False) + '\n', encoding='utf-8')
    total = sum(p.stat().st_size for p in PACK.rglob('*') if p.is_file())
    print(f'{len(sfx)} sounds, {len(fonts)} fonts, {icons["count"]} icons; {total / 1e6:.1f} MB in rn/packs')


if __name__ == '__main__':
    main()
