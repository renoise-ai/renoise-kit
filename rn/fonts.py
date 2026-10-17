"""rn fonts: what is actually installed, from fontconfig, or where there is none (Windows) from the system font folders.
A missing weight makes browsers and libass fall back silently (Katana's sandbox only had some families in ExtraBold), so
check the weight and script here before using a face."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .ff import RnError

# fontconfig weight -> CSS weight (fontconfig.h FC_WEIGHT_*).
_CSS = [(0, 100), (40, 200), (50, 300), (80, 400), (100, 500), (180, 600), (200, 700), (205, 800), (210, 900)]
_SCRIPTS = {'latin': ('en',), 'zh-hans': ('zh-cn', 'zh-sg'), 'zh-hant': ('zh-tw', 'zh-hk'), 'ja': ('ja',), 'ko': ('ko',),
            'th': ('th',), 'ar': ('ar',), 'he': ('he',), 'hi': ('hi',), 'ru': ('ru',)}


FONT_SUFFIXES = ('.ttf', '.otf', '.ttc', '.otc')
# Without fontconfig a font covers a script when its cmap has all of these characters.
_SAMPLES = {'latin': 'AZaz', 'zh-hans': '这们说', 'zh-hant': '這們說', 'ja': 'あアの', 'ko': '한국어', 'th': 'กขค', 'ar': 'بتث',
            'he': 'אבג', 'hi': 'कखग', 'ru': 'ЖЯЫ', 'emoji': '\U0001F600\U0001F389\U0001F44D'}


def font_dirs() -> list[Path]:
    """Where the OS keeps installed fonts, system-wide and per user."""
    home = Path.home()
    if sys.platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
        dirs = [Path(os.environ.get('WINDIR') or 'C:/Windows') / 'Fonts', local / 'Microsoft' / 'Windows' / 'Fonts']
    elif sys.platform == 'darwin':
        dirs = [Path('/System/Library/Fonts'), Path('/Library/Fonts'), home / 'Library' / 'Fonts']
    else:
        data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local' / 'share')
        dirs = [Path('/usr/share/fonts'), Path('/usr/local/share/fonts'), data / 'fonts', home / '.fonts']
    return [d for d in dirs if d.is_dir()]


def font_files() -> list[Path]:
    return sorted({f for d in font_dirs() for f in d.rglob('*') if f.suffix.lower() in FONT_SUFFIXES and f.is_file()})


def css_weight(fc_weight: float) -> int:
    return min(_CSS, key=lambda pair: abs(pair[0] - fc_weight))[1]


def _license(path: str) -> str | None:
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        font = TTFont(path, fontNumber=0, lazy=True)
        names = font['name']
        text = names.getDebugName(13) or names.getDebugName(14)
    except Exception:  # unreadable or exotic font: no license info rather than a failure
        return None
    return ' '.join(text.split())[:120] if text else None


def _entry(families: dict, family: str) -> dict:
    return families.setdefault(family, {'family': family, 'weights': set(), 'styles': set(), 'variable': False,
                                        'scripts': set(), 'files': set()})


def installed() -> dict[str, dict]:
    if not shutil.which('fc-list'):
        return _scanned()
    output = subprocess.run(['fc-list', '--format', '%{family[0]}\t%{style[0]}\t%{weight}\t%{variable}\t%{file}\t%{lang}\n'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True).stdout
    families: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) != 6 or not parts[0] or parts[0].startswith('.'):
            continue
        family, style, weight, variable, path, langs = parts
        entry = _entry(families, family)
        try:
            entry['weights'].add(css_weight(float(weight)))
        except ValueError:
            pass
        entry['styles'].add(style)
        entry['variable'] |= variable == 'True'
        entry['files'].add(path)
        codes = set(langs.split('|'))
        entry['scripts'] |= {name for name, keys in _SCRIPTS.items() if codes & set(keys)}
        if 'emoji' in family.lower():
            entry['scripts'].add('emoji')
    return families


def _scanned() -> dict[str, dict]:
    """The same facts read from the font files themselves: name table, OS/2 weight, fvar axes, cmap coverage."""
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        raise RnError('fc-list not found: install fontconfig, or fontTools (extra "fonts") to read the system font '
                      'folders directly: uv tool install "renoise-kit[all]"') from None
    families: dict[str, dict] = {}
    for path in font_files():
        try:
            faces = TTCollection(str(path), lazy=True).fonts if path.suffix.lower() in ('.ttc', '.otc') else [TTFont(str(path), lazy=True)]
            for font in faces:
                names = font['name']
                family = names.getDebugName(16) or names.getDebugName(1)
                if not family or family.startswith('.'):
                    continue
                style = names.getDebugName(17) or names.getDebugName(2) or 'Regular'
                weight = font['OS/2'].usWeightClass if 'OS/2' in font else 400
                weight = weight * 100 if weight < 10 else weight  # a few old fonts store 1-9
                weights = {min(900, max(100, round(weight / 100) * 100))}
                axes = {axis.axisTag: axis for axis in font['fvar'].axes} if 'fvar' in font else {}
                if 'wght' in axes:
                    weights |= {w for w in range(100, 1000, 100) if axes['wght'].minValue <= w <= axes['wght'].maxValue}
                cmap = font.getBestCmap() or {}
                entry = _entry(families, family)
                entry['styles'].add(style)
                entry['weights'] |= weights
                entry['variable'] |= bool(axes)
                entry['scripts'] |= {name for name, chars in _SAMPLES.items() if all(ord(c) in cmap for c in chars)}
                entry['files'].add(str(path))
        except Exception:  # unreadable or exotic font file: skip it rather than fail the listing
            continue
    return families


def fonts_command(*, family: str | None, script: str | None, licenses: bool) -> dict:
    if script and script not in (*_SCRIPTS, 'emoji'):
        raise RnError(f'--script: one of {", ".join([*_SCRIPTS, "emoji"])}')
    rows = []
    for entry in sorted(installed().values(), key=lambda e: e['family'].lower()):
        if family and family.lower() not in entry['family'].lower():
            continue
        if script and script not in entry['scripts']:
            continue
        files = sorted(entry['files'])
        row = {'family': entry['family'], 'weights': sorted(entry['weights']), 'styles': sorted(entry['styles']),
               'variable': entry['variable'], 'scripts': sorted(entry['scripts']), 'files': files}
        if licenses:
            row['license'] = _license(files[0])
        rows.append(row)
    if family and not rows:
        raise RnError(f'no installed family matches "{family}"; run rn fonts to list them, or download one into your project')
    return {'count': len(rows), 'fonts': rows}


def summary_lines(result: dict) -> list[str]:
    lines = []
    for row in result['fonts']:
        weights = ','.join(str(w) for w in row['weights']) + (' (variable)' if row['variable'] else '')
        lines.append(f'{row["family"]}: {weights}  [{" ".join(row["scripts"]) or "-"}]' + (f'  {row["license"]}' if row.get('license') else ''))
    lines.append(f'{result["count"]} families')
    return lines
