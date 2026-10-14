"""rn fonts: what is actually installed, from fontconfig. A missing weight makes browsers and libass fall back
silently (Katana's sandbox only had some families in ExtraBold), so check the weight and script here before using a face."""
from __future__ import annotations

import shutil
import subprocess

from .ff import RnError

# fontconfig weight -> CSS weight (fontconfig.h FC_WEIGHT_*).
_CSS = [(0, 100), (40, 200), (50, 300), (80, 400), (100, 500), (180, 600), (200, 700), (205, 800), (210, 900)]
_SCRIPTS = {'latin': ('en',), 'zh-hans': ('zh-cn', 'zh-sg'), 'zh-hant': ('zh-tw', 'zh-hk'), 'ja': ('ja',), 'ko': ('ko',),
            'th': ('th',), 'ar': ('ar',), 'he': ('he',), 'hi': ('hi',), 'ru': ('ru',)}


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


def installed() -> dict[str, dict]:
    if not shutil.which('fc-list'):
        raise RnError('fc-list not found (fontconfig); the media sandbox has it')
    output = subprocess.run(['fc-list', '--format', '%{family[0]}\t%{style[0]}\t%{weight}\t%{variable}\t%{file}\t%{lang}\n'],
                            capture_output=True, text=True, check=True).stdout
    families: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) != 6 or not parts[0] or parts[0].startswith('.'):
            continue
        family, style, weight, variable, path, langs = parts
        entry = families.setdefault(family, {'family': family, 'weights': set(), 'styles': set(), 'variable': False,
                                             'scripts': set(), 'files': set()})
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
