"""rn doctor: what this machine can do with rn, offline, in about a second. Each check is ok, warn (there, but limited)
or missing, with the rn commands that depend on it and a one-line fix. The core sets the exit status: Python with rn's
required modules, and ffmpeg and ffprobe with the H.264 / AAC encoders every render uses. Missing extras, models, OCR or
fonts only narrow what works. Nothing is downloaded or opened: Python modules are found, not imported (importing librosa
alone takes seconds), and the asset manifest URL is reported, not fetched.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .ff import LONG_FILTER

PYTHON_MIN = (3, 11)  # requires-python in pyproject.toml
FFMPEG_MIN = 7  # README / SPEC §6; older builds lack "-/filter_complex FILE", which long filter graphs go through
# Package data and who reads it (pyproject.toml [tool.setuptools.package-data]).
PACKAGE_FILES = {'edit.schema.json': ['rn schema'], 'ref_guide.md': ['rn ref'], 'fx/fx.js': ['rn capture', 'rn fx']}
ENCODERS = ('libx264', 'aac')  # rn assemble slots and output, rn finish, rn compare --video
EVERY = 'every rn command except doctor and schema'

# Filters rn's own graphs use (an EDL's raw "vf" / "af" may add others), by what needs them: from the -vf, -af and
# -filter_complex strings in rn/. A test checks each name still appears in rn's sources.
FILTER_USES = (
    ('rn assemble', 'scale crop pad setpts trim fps setsar format setparams tpad settb color overlay concat asetpts atrim '
                    'aresample aformat volume apad amix anullsrc alimiter ebur128'),
    ('rn assemble (slow motion)', 'framerate'),
    ('rn assemble (keys scale, rotate, left, top)', 'perspective'),
    ('rn assemble (overlay opacity and fades)', 'colorchannelmixer fade geq'),
    ('rn assemble (transitions)', 'xfade acrossfade'),
    ('rn assemble (mask, mix-blend-mode)', 'split alphaextract alphamerge negate blend'),
    ('rn assemble (subtitles)', 'subtitles'),
    ('rn assemble (speed with sound)', 'atempo'),
    ('rn assemble (audio fades and "at")', 'afade adelay'),
    ('rn assemble (mix.duck_db)', 'asplit sidechaincompress'),
    ('rn finish', 'scale setsar format setparams atrim asetpts adelay aresample apad volume alimiter ebur128'),
    ('rn qa', 'blackdetect freezedetect cropdetect aresample silencedetect asetnsamples astats ametadata ebur128'),
    ('rn sheet', 'select crop scale'),
    ('rn frame', 'select'),
    ('rn text', 'select'),
    ('rn cuts', 'select metadata scale'),
    ('rn shots', 'scale select'),
    ('rn ref', 'select metadata scale ebur128'),
    ('rn compare', 'select scale'),
    ('rn compare --cuts', 'metadata'),
    ('rn compare --video', 'setsar hstack'),
    ('rn signal', 'signalstats metadata'),
    ('rn listen', 'ebur128'),
)

OPENCV = ('opencv-python-headless', 'opencv-python', 'opencv-contrib-python-headless', 'opencv-contrib-python')
# (check name, modules it needs, distributions to read a version from, extra, what imports it). From the import sites in
# rn/; the extras are pyproject.toml's. An extra of None is a required dependency: part of the core.
MODULES = (
    ('numpy', ('numpy',), ('numpy',), None, ['rn shots', 'rn signal', 'rn align', 'rn listen', 'rn.fx']),
    ('pillow', ('PIL',), ('pillow',), None, ['rn sheet', 'rn compare', 'rn listen', 'rn matte', 'rn assemble (images, cards)']),
    ('opencv', ('cv2',), OPENCV, 'faces', ['rn faces', 'rn shots', 'rn compare --shots']),
    ('librosa', ('librosa',), ('librosa',), 'listen', ['rn listen', 'rn ref (a video with sound)']),
    ('matplotlib', ('matplotlib',), ('matplotlib',), 'listen', ['rn listen', 'rn ref (a video with sound)', 'rn signal -o']),
    ('faster-whisper', ('faster_whisper',), ('faster-whisper',), 'transcribe', ['rn listen --transcribe', 'rn ref --transcribe']),
    ('playwright', ('playwright',), ('playwright',), 'capture', ['rn capture']),
    ('pysubs2', ('pysubs2',), ('pysubs2',), 'captions', ['rn captions', 'rn export --srt (from .ass)']),
    ('fonttools', ('fontTools',), ('fonttools',), 'fonts', ['rn fonts --licenses', 'rn fonts without fc-list']),
    ('rembg', ('rembg', 'onnxruntime'), ('rembg',), 'matte', ['rn matte']),
    ('opentimelineio', ('opentimelineio',), ('opentimelineio',), 'export', ['rn export']),
    # OTIO adapters are plugins found through their distributions' entry points, not imported by name.
    ('otio-fcpx-xml-adapter', (), ('otio-fcpx-xml-adapter',), 'export', ['rn export -o .fcpxml']),
    ('otio-fcp-adapter', (), ('otio-fcp-adapter',), 'export', ['rn export -o .xml']),
)
WHISPER_MODEL = ('small', 'Systran/faster-whisper-small')  # rn listen --model default, and its repo as faster-whisper names it
MATTE_MODEL = 'isnet-general-use'  # rn matte --model default


def _by_os(mac, linux, windows):
    return mac if sys.platform == 'darwin' else windows if sys.platform == 'win32' else linux


def _install_ffmpeg() -> str:
    return _by_os('brew install ffmpeg-full, then export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"',
                  'apt install ffmpeg fontconfig (Debian trixie has 7.1)', 'winget install Gyan.FFmpeg, then open a new terminal')


def _check(name: str, group: str, status: str, detail: str, commands: list[str], fix: str, *, core: bool = False,
           version: str | None = None, path: str | None = None) -> dict:
    """Every check has these keys: `commands` are the rn commands that depend on it (for a warn, the ones it limits);
    `fix` is None when the check is ok."""
    return {'name': name, 'group': group, 'core': core, 'status': status, 'version': version, 'path': path, 'detail': detail,
            'commands': commands, 'fix': None if status == 'ok' else fix}


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def _failure(result: subprocess.CompletedProcess | None) -> str | None:
    """Why a tool did not run, or None when it did."""
    if result is None:
        return 'did not start'
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        return lines[-1] if lines else f'exit {result.returncode}'
    return None


def _names(listing: str) -> set[str]:
    """Second column of `ffmpeg -filters` / `-encoders`."""
    return {parts[1] for parts in (line.split() for line in listing.splitlines()) if len(parts) > 2}


def _found(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(names) -> str | None:
    for name in names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _python_checks() -> list[dict]:
    version = '.'.join(str(v) for v in sys.version_info[:3])
    fine = sys.version_info[:2] >= PYTHON_MIN
    checks = [_check('python', 'python', 'ok' if fine else 'missing',
                     f'rn needs {PYTHON_MIN[0]}.{PYTHON_MIN[1]}+', ['every rn command'],
                     'uv tool install --python 3.12 "renoise-kit[all]"', core=True, version=version, path=sys.executable)]
    package = Path(__file__).parent
    absent = [name for name in PACKAGE_FILES if not (package / name).is_file()]
    checks.append(_check('rn', 'python', 'missing' if absent else 'ok',
                         f'package files missing: {", ".join(absent)}' if absent else f'package files: {", ".join(PACKAGE_FILES)}',
                         [command for name in absent or PACKAGE_FILES for command in PACKAGE_FILES[name]],
                         'uv tool install --reinstall "renoise-kit[all]"', version=__version__, path=str(package)))
    for name, modules, dists, extra, commands in MODULES:
        absent = [m for m in modules if not _found(m)]
        version = _dist_version(dists)
        present = not absent if modules else version is not None
        detail = (f'extra "{extra}"' if extra else 'required') if present else \
            'not installed' + (f' (module {", ".join(absent)})' if absent else '')
        fix = ('uv tool install --reinstall "renoise-kit[all]"' if extra is None else
               f'extra "{extra}": uv tool install "renoise-kit[all]"')
        checks.append(_check(name, 'module', 'ok' if present else 'missing', detail, commands, fix, core=extra is None,
                             version=version if present else None))
    return checks


def _ffmpeg_checks(paths: dict, out: dict) -> list[dict]:
    checks = []
    working = {}
    for tool in ('ffmpeg', 'ffprobe'):
        path, result = paths[tool], out.get(tool)
        failed = 'not found on PATH' if not path else _failure(result)
        if failed:
            checks.append(_check(tool, 'ffmpeg', 'missing', failed, [EVERY], _install_ffmpeg(), core=True, path=path))
            continue
        working[tool] = result
        banner = result.stderr if tool == 'ffmpeg' else result.stdout  # -filters prints the banner to stderr
        match = re.search(rf'{tool} version (\S+)', banner)
        version = match.group(1) if match else None
        major = re.match(r'n?(\d+)\.', version or '')  # a git build ("N-118000-g...") has no number: take it as current
        old = tool == 'ffmpeg' and major is not None and int(major.group(1)) < FFMPEG_MIN
        checks.append(_check(tool, 'ffmpeg', 'warn' if old else 'ok',
                             f'rn expects {FFMPEG_MIN}+: filter graphs over {LONG_FILTER} characters (many overlays or keys) fail'
                             if old else '', ['rn assemble'] if old else [EVERY],
                             f'ffmpeg {FFMPEG_MIN}+: {_install_ffmpeg()}', core=True, version=version, path=path))
    renders = ['rn assemble', 'rn finish', 'rn compare --video']
    if 'ffmpeg' not in working:
        checks.append(_check('encoders', 'ffmpeg', 'missing', 'not checked: no working ffmpeg', renders, _install_ffmpeg(), core=True))
        have = None
    else:
        encoders = _names(out['encoders'].stdout) if out.get('encoders') and not _failure(out['encoders']) else set()
        absent = [e for e in ENCODERS if e not in encoders]
        checks.append(_check('encoders', 'ffmpeg', 'missing' if absent else 'ok',
                             f'missing {", ".join(absent)}' if absent else ', '.join(ENCODERS), renders,
                             f'an ffmpeg build with libx264 (GPL) and aac: {_install_ffmpeg()}', core=True))
        have = _names(working['ffmpeg'].stdout)
    uses: dict[str, list[str]] = {}
    for command, names in FILTER_USES:
        for name in names.split():
            uses.setdefault(name, []).append(command)
    for name, commands in uses.items():
        libass = name == 'subtitles'
        status = 'ok' if have is not None and name in have else 'missing'
        detail = ('not checked: no working ffmpeg' if have is None else
                  ('in this ffmpeg' if status == 'ok' else 'not in this ffmpeg build') + (' (libass)' if libass else ''))
        fix = f'an ffmpeg built with libass: {_install_ffmpeg()}' if libass else f'a full ffmpeg {FFMPEG_MIN}+ build: {_install_ffmpeg()}'
        checks.append(_check(f'filter {name}', 'filter', status, detail, commands, fix))
    return checks


def _playwright_check() -> dict:
    """Playwright's own Chromium for rn capture (headless: the headless shell where this playwright has one)."""
    commands = ['rn capture']
    spec = importlib.util.find_spec('playwright') if _found('playwright') else None
    if not spec or not spec.origin:
        return _check('chromium', 'browser', 'missing', 'playwright is not installed', commands,
                      'extra "capture": uv tool install "renoise-kit[all]", then playwright install chromium')
    package = Path(spec.origin).parent / 'driver' / 'package'
    setting = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if setting == '0':
        root = package / '.local-browsers'
    elif setting:
        root = Path(setting)
    else:
        root = _by_os(Path.home() / 'Library' / 'Caches', Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache'),
                      Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')) / 'ms-playwright'
    try:
        browsers = {b['name']: b['revision'] for b in json.loads((package / 'browsers.json').read_text(encoding='utf-8'))['browsers']}
        name = next(n for n in ('chromium-headless-shell', 'chromium') if n in browsers)
        wanted = [f'{name.replace("-", "_")}-{browsers[name]}']
    except (OSError, ValueError, KeyError, StopIteration):  # an unfamiliar layout: any Chromium will do
        wanted = sorted(p.name for p in root.glob('chromium*')) if root.is_dir() else []
    present = next((root / w for w in wanted if (root / w).is_dir()), None)
    return _check('chromium', 'browser', 'ok' if present else 'missing',
                  'playwright install chromium done' if present else f'no {wanted[0] if wanted else "Chromium"} in {root}',
                  commands, 'playwright install chromium', path=str(present) if present else None)


def _tesseract_check(path: str | None, out: dict) -> dict:
    commands = ['rn text']
    fix = _by_os('brew install tesseract (ffmpeg-full brings it)', 'apt install tesseract-ocr tesseract-ocr-chi-sim',
                 'winget install UB-Mannheim.TesseractOCR, then add C:\\Program Files\\Tesseract-OCR to PATH')
    failed = 'not found on PATH' if not path else _failure(out.get('tesseract'))
    if failed:
        return _check('tesseract', 'system', 'missing', failed, commands, fix, path=path)
    words = (out['tesseract'].stdout + out['tesseract'].stderr).split()
    listing = out['tesseract_langs'].stdout.splitlines() if out.get('tesseract_langs') else []
    languages = [line.strip() for line in listing if line.strip() and 'List of' not in line and line.strip() != 'osd']
    return _check('tesseract', 'system', 'ok' if languages else 'missing', f'languages: {" ".join(languages) or "none"}',
                  commands, fix, version=words[1] if len(words) > 1 else None, path=path)


def _font_checks(path: str | None, out: dict) -> list[dict]:
    from .captions import FONTS, WINDOWS_FONTS
    families: set[str] = set()
    chinese = 0
    listing = out.get('fonts')
    if path and listing is not None and not _failure(listing):
        for line in listing.stdout.splitlines():
            names, _, langs = line.partition('\t')
            families |= {name.strip() for name in names.split(',') if name.strip()}
            chinese += 'zh-cn' in langs.split('|')
    fc_fix = _by_os('brew install fontconfig', 'apt install fontconfig',
                    'optional on Windows: rn fonts reads the font folders with fontTools (extra "fonts")')
    checks = [_check('fc-list', 'fonts', 'ok' if path else 'missing', '' if path else 'not found on PATH',
                     ['rn fonts (else fontTools reads the font files)', 'rn assemble --placeholders (a Chinese font for cards)',
                      'rn doctor (caption fonts)'], fc_fix, path=path)]
    wanted = sorted(set((WINDOWS_FONTS if sys.platform == 'win32' else FONTS).values()))
    commands = ['rn captions', 'rn assemble (burned-in subtitles: libass falls back silently)']
    if not path:
        checks.append(_check('caption fonts', 'fonts', 'warn', f'not checked without fc-list: {", ".join(wanted)}', commands, fc_fix))
        return checks
    absent = [family for family in wanted if family not in families]
    font_fix = _by_os('install Noto Sans and Noto Sans CJK (fonts.google.com/noto) into ~/Library/Fonts',
                      'apt install fonts-noto-core fonts-noto-cjk', f'install {", ".join(absent)}')
    checks.append(_check('caption fonts', 'fonts', 'missing' if absent else 'ok',
                         f'not installed: {", ".join(absent)}; {chinese} installed fonts cover Chinese' if absent else ', '.join(wanted),
                         commands, font_fix))
    return checks


def _model_checks() -> list[dict]:
    from .faces import DEFAULT_MODEL
    checks = []
    setting = os.environ.get('RN_YUNET_MODEL')
    cached = Path.home() / '.cache' / 'rn' / Path(DEFAULT_MODEL).name
    face = ['rn faces', 'rn shots --faces']
    if setting:
        found = Path(setting).is_file()
        checks.append(_check('yunet model', 'model', 'ok' if found else 'missing', 'RN_YUNET_MODEL' + ('' if found else ': no such file'),
                             face, 'point RN_YUNET_MODEL at the model, or unset it', path=setting))
    else:
        found = next((str(p) for p in (Path(DEFAULT_MODEL), cached) if p.is_file()), None)
        checks.append(_check('yunet model', 'model', 'ok' if found else 'missing',
                             ('baked into the sandbox' if found == DEFAULT_MODEL else 'fetched before') if found else
                             f'neither {DEFAULT_MODEL} nor {cached}; fetched on first use (needs the network)', face,
                             'run rn faces once with network access, or set RN_YUNET_MODEL', path=found))
    # Where huggingface_hub keeps downloads, which is where faster-whisper looks for a model given by name.
    hub = os.environ.get('HF_HUB_CACHE') or os.environ.get('HUGGINGFACE_HUB_CACHE')
    hf_home = os.environ.get('HF_HOME') or Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'huggingface'
    hub = Path(hub) if hub else Path(hf_home) / 'hub'
    snapshot = next((p.parent for p in (hub / f'models--{WHISPER_MODEL[1].replace("/", "--")}').glob('snapshots/*/model.bin')), None)
    checks.append(_check('whisper model', 'model', 'ok' if snapshot else 'missing',
                         WHISPER_MODEL[0] if snapshot else f'{WHISPER_MODEL[0]} not in {hub}; downloaded on first use (needs the network)',
                         [f'rn listen --transcribe (default --model {WHISPER_MODEL[0]})', 'rn ref --transcribe'],
                         'run rn listen --transcribe once with network access', path=str(snapshot) if snapshot else None))
    u2net = Path(os.environ.get('U2NET_HOME') or Path(os.environ.get('XDG_DATA_HOME') or Path.home()) / '.u2net')
    onnx = u2net / f'{MATTE_MODEL}.onnx'
    checks.append(_check('matte model', 'model', 'ok' if onnx.is_file() else 'missing',
                         MATTE_MODEL if onnx.is_file() else f'{MATTE_MODEL} not in {u2net}; downloaded on first use (needs the network)',
                         [f'rn matte (default --model {MATTE_MODEL})'], 'run rn matte once with network access',
                         path=str(onnx) if onnx.is_file() else None))
    return checks


def _settings() -> dict:
    from .assets import _cache, _url
    return {'assets_manifest': _url(), 'assets_manifest_from': 'RN_ASSETS_URL' if os.environ.get('RN_ASSETS_URL') else 'default',
            'assets_cache': str(_cache()), 'render_cache': os.environ.get('RN_CACHE') or '.rn-cache next to each EDL'}


def doctor_command() -> dict:
    started = time.monotonic()
    paths = {tool: shutil.which(tool) for tool in ('ffmpeg', 'ffprobe', 'tesseract', 'fc-list')}
    calls = {}
    if paths['ffmpeg']:
        calls['ffmpeg'] = [paths['ffmpeg'], '-filters']  # the banner (version) goes to stderr, the filters to stdout
        calls['encoders'] = [paths['ffmpeg'], '-hide_banner', '-encoders']
    if paths['ffprobe']:
        calls['ffprobe'] = [paths['ffprobe'], '-version']
    if paths['tesseract']:
        calls['tesseract'] = [paths['tesseract'], '--version']
        calls['tesseract_langs'] = [paths['tesseract'], '--list-langs']
    if paths['fc-list']:
        calls['fonts'] = [paths['fc-list'], '--format', '%{family}\t%{lang}\n']
    with ThreadPoolExecutor(max_workers=max(1, len(calls))) as pool:
        futures = {key: pool.submit(_run, args) for key, args in calls.items()}
        python = _python_checks()  # while the tools run
        out = {key: future.result() for key, future in futures.items()}
    checks = [*python[:2], *_ffmpeg_checks(paths, out), *python[2:], _playwright_check(), _tesseract_check(paths['tesseract'], out),
              *_font_checks(paths['fc-list'], out), *_model_checks()]
    return {'ok': all(c['status'] != 'missing' for c in checks if c['core']), 'rn': __version__,
            'python': '.'.join(str(v) for v in sys.version_info[:3]), 'platform': sys.platform, 'checks': checks,
            'missing': [c['name'] for c in checks if c['status'] == 'missing'],
            'warnings': [c['name'] for c in checks if c['status'] == 'warn'], 'settings': _settings(),
            'seconds': round(time.monotonic() - started, 2)}


def _lines(check: dict) -> list[str]:
    version, path = check['version'], check['path']
    facts = f'{version} ({path})' if version and path else version or path
    head = f'{check["status"]:8} {check["name"]}: ' + ', '.join(part for part in (facts, check['detail']) if part)
    if check['status'] == 'ok':
        return [head]
    return [head, f'{"":9}affects {", ".join(check["commands"])} | fix: {check["fix"]}']


def summary_lines(result: dict) -> list[str]:
    lines: list[str] = []
    filters = [c for c in result['checks'] if c['group'] == 'filter']
    for check in result['checks']:
        if check['group'] != 'filter':
            lines.extend(_lines(check))
        elif check is filters[0]:  # one line for all filters unless some are missing
            bad = [c for c in filters if c['status'] != 'ok']
            if not bad:
                lines.append(f'ok       ffmpeg filters: all {len(filters)} that rn uses, subtitles (libass) included')
            elif len(bad) == len(filters) and bad[0]['detail'].startswith('not checked'):
                lines.append(f'missing  ffmpeg filters: {bad[0]["detail"]}')
            else:
                for c in bad:
                    lines.extend(_lines(c))
    settings = result['settings']
    lines.append(f'assets   manifest {settings["assets_manifest"]} ({settings["assets_manifest_from"]}, not fetched; '
                 f'RN_ASSETS_URL changes it), cache {settings["assets_cache"]}')
    warnings = len(result['warnings'])
    lines.append(f'doctor: {"core ok" if result["ok"] else "core broken (exit 1)"}; {len(result["missing"])} missing, '
                 f'{warnings} warning{"" if warnings == 1 else "s"}  ({result["seconds"]}s)')
    return lines
