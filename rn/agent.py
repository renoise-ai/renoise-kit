"""What every command adds for an agent that cannot watch or listen: a proof image to look at (one labelled JPEG under
125 KB, sized for sandbox_exec image_paths), `next:` lines with the exact next command, and a run-time estimate before
long work (foreground sandbox commands die after about a minute)."""
from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

PROOF_KB = 125
TILE_W = 256


# ---------------------------------------------------------------- proof images

def _checker(size: tuple[int, int], cell: int = 12):
    from PIL import Image
    w, h = size
    board = Image.new('RGB', (w, h), (200, 200, 200))
    dark = Image.new('RGB', (cell, cell), (150, 150, 150))
    for y in range(0, h, cell):
        for x in range((y // cell) % 2 * cell, w, cell * 2):
            board.paste(dark, (x, y))
    return board


def _flat(image):
    """RGB for a tile: transparency shown over a checkerboard."""
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        image = image.convert('RGBA')
        board = _checker(image.size)
        board.paste(image, mask=image.getchannel('A'))
        return board
    return image.convert('RGB')


def _fit(images, width: int = TILE_W, max_aspect: float = 2.2):
    """Same-size tiles: scaled to width, very tall pictures (a full web page) cut to their top, padded to one height."""
    from PIL import Image, ImageOps
    tiles = []
    for image in images:
        image = _flat(image)
        if image.height > image.width * max_aspect:
            image = image.crop((0, 0, image.width, int(image.width * max_aspect)))
        tiles.append(image.resize((width, max(1, round(image.height * width / image.width))), Image.LANCZOS))
    height = max(t.height for t in tiles)
    return [ImageOps.pad(t, (width, height), color=(24, 24, 24)) for t in tiles]


def write_proof(images, labels: list[str], title: str, path: Path, columns: int | None = None, width: int = TILE_W) -> str:
    from .frames import _compose, _save_under
    tiles = _fit(images, width)
    columns = columns or min(len(tiles), 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_under(_compose([], Fraction(1), columns, title, tiles, labels), path, PROOF_KB)
    return str(path)


def proof_path(out: str | Path) -> Path:
    """Next to the output: <file stem>.proof.jpg, or <folder>.proof.jpg beside a folder (never inside a frame sequence)."""
    out = Path(out)
    return out.with_name(f'{out.stem if out.suffix else out.name}.proof.jpg')


def _frames(video: str | Path, indices: list[int], width: int = TILE_W * 2):
    from PIL import Image
    from .frames import extract
    with tempfile.TemporaryDirectory() as tmp:
        return [(i, Image.open(p).convert('RGB')) for i, p in extract(video, indices, Path(tmp), width=width)]


def _spread(count: int, n: int) -> list[int]:
    return sorted({round(k * (count - 1) / max(1, n - 1)) for k in range(n)}) if count > 0 else []


def _is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff')


def _seq(folder: str | Path) -> list[Path]:
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() == '.png')


def _matte_proof(source: str, folder: str, cutout: bool, path: Path, title: str, source_index=lambda i: i) -> str:
    """source | matte | cut-out over a checkerboard, for three frames (or the one still)."""
    from PIL import Image
    files = _seq(folder)
    picks = _spread(len(files), 3)
    if _is_video(source):
        sources = dict(_frames(source, [source_index(i) for i in picks]))
        src_of = lambda i: sources.get(source_index(i))  # noqa: E731
    else:
        still = Image.open(source).convert('RGB')
        src_of = lambda i: still  # noqa: E731
    images, labels = [], []
    for i in picks:
        made = Image.open(files[i])
        src = src_of(i)
        if src is None:
            continue
        if cutout:
            matte, cut = made.convert('RGBA').getchannel('A'), made.convert('RGBA')
        else:
            matte = made.convert('L')
            cut = src.convert('RGBA')
            cut.putalpha(matte.resize(src.size))
        images += [src, matte.convert('RGB'), cut]
        labels += [f'#{source_index(i)} source', 'matte (white = kept)', 'cut-out']
    return write_proof(images, labels, title, path, columns=3)


def _grid_proof(result: dict, path: Path) -> str:
    from PIL import Image
    tiles = result['tiles']
    return write_proof([Image.open(t['path']) for t in tiles], [f'r{t["row"]}c{t["col"]}' for t in tiles],
                       f'{Path(result["path"]).name}: {len(tiles)} tiles', path, columns=max(1, min(result.get('cols') or 4, 6)))


def _sequence_proof(result: dict, path: Path, title: str) -> str:
    """Several frames of a rendered PNG sequence (rn template render, rn capture), transparency over a checkerboard."""
    from PIL import Image
    files = _seq(result['dir'])
    picks = _spread(len(files), 6)
    fps = float(result.get('fps') or 30)
    return write_proof([Image.open(files[i]) for i in picks], [f'#{i}  {i / fps:.2f}s' for i in picks], title, path, columns=3)


def _reframe_proof(source: str, result: dict, path: Path) -> str:
    """The source with the crop (green) and the tracked face (yellow), next to the reframed output at the same frames."""
    from PIL import ImageDraw
    plan, track = result['reframe'], result['track']
    out = plan['render']['path']
    count = plan['render']['frames']
    picks = _spread(count, 4)
    src = dict(_frames(source, picks, width=None))
    made = dict(_frames(out, picks, width=None))
    cw, ch = plan['crop']
    keys = plan['keys']
    by_frame = {t['f']: t for t in track}
    images, labels = [], []
    for f in picks:
        if f not in src or f not in made:
            continue
        x, y = next(((kx, ky) for kf, kx, ky in reversed(keys) if kf <= f), (keys[0][1], keys[0][2]))
        image = src[f].copy()
        draw = ImageDraw.Draw(image)
        draw.rectangle([x, y, x + cw, y + ch], outline=(64, 255, 96), width=max(2, image.width // 200))
        near = by_frame.get(f) or min(track, key=lambda t: abs(t['f'] - f))
        if near.get('box'):
            bx, by, bw, bh = near['box']
            draw.rectangle([bx, by, bx + bw, by + bh], outline=(255, 220, 64), width=max(2, image.width // 250))
        images += [image, made[f]]
        labels += [f'#{f} source: crop, face', f'#{f} reframed']
    return write_proof(images, labels, f'reframe {plan["size"][0]}x{plan["size"][1]}: {Path(out).name}', path, columns=4)


def _waveform_proof(result: dict, path: Path) -> str:
    """The music bed as a waveform with its start, loop points and fade marked in bed seconds."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    rate = 4000
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', result['path'], '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'],
                         capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    w, h, top = 1000, 220, 26
    image = Image.new('RGB', (w, h + top + 18), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=13)
    duration = max(len(samples) / rate, 1e-6)
    columns = np.array_split(samples, w) if len(samples) >= w else [samples] * w
    mid = top + h // 2
    for x, col in enumerate(columns):
        if len(col):
            draw.line([(x, mid - col.max() * h / 2), (x, mid - col.min() * h / 2)], fill=(120, 170, 255))

    def mark(t: float, colour, text: str, row: int) -> None:
        x = int(min(w - 1, max(0, t / duration * w)))
        draw.line([(x, top), (x, top + h)], fill=colour, width=2)
        draw.text((min(x + 3, w - 150), top + 2 + row * 14), text, fill=colour, font=font)
    mark(0, (64, 255, 96), f'start = music {result["start"]}s', 0)
    for k, loop in enumerate(result.get('loops') or []):
        at = loop.get('at', loop.get('time', 0)) if isinstance(loop, dict) else float(loop)
        mark(float(at), (255, 160, 64), f'loop {at}s', 1 + k % 3)
    fade = result.get('fade_out') or {}
    if fade:
        mark(fade['at'], (255, 220, 64), f'fade {fade["at"]}s ({fade["seconds"]}s)', 4)
    draw.text((4, 4), f'{Path(result["path"]).name}  {result["duration"]}s  {result.get("bpm") or "?"} bpm  '
                      f'{result.get("verdict", "")}'[:150], fill=(255, 255, 255), font=font)
    draw.text((4, top + h + 2), '0s', fill=(200, 200, 200), font=font)
    draw.text((w - 60, top + h + 2), f'{duration:.2f}s', fill=(200, 200, 200), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .frames import _save_under
    _save_under(image, path, PROOF_KB)
    return str(path)


def _web_proof(result: dict, path: Path) -> str:
    from PIL import Image
    images, labels = [], []
    if result.get('full'):
        images.append(Image.open(result['full']))
        labels.append('full page (top)')
    for s in result.get('sections', []):
        images.append(Image.open(s['path']))
        labels.append(f'section "{s["label"]}"'[:32])
    if result.get('scroll_video'):
        video = result['scroll_video']
        picks = _spread(video['frames'], 4)
        for i, image in _frames(video['path'], picks):
            images.append(image)
            labels.append(f'scroll #{i}  {i / video["fps"]:.1f}s')
    if not images:
        return ''
    return write_proof(images, labels, f'{result["url"]}'[:90], path, columns=min(4, len(images)))


def _q(value) -> str:
    return shlex.quote(str(value))


def proof(args, result: dict) -> str | None:
    """Write the command's proof image when its output is a picture or sound to check; None when there is nothing to show
    or it already wrote a sheet (then that sheet is the proof)."""
    command = args.command
    if command == 'sheet':
        return result['pages'][0]['path'] if result.get('pages') else None
    if command in ('cuts', 'shots') and result.get('sheet'):
        return result['sheet'][0]['path'] if isinstance(result['sheet'], list) else result['sheet'].get('path')
    if getattr(args, 'no_proof', False):
        return None
    if command == 'key':
        return _matte_proof(args.file, result['dir'], result['cutout'], proof_path(result['dir']),
                            f'rn key {Path(args.file).name}: backdrop {result["backdrop"]["color"]}')
    if command == 'matte':
        return _matte_proof(args.file, result['dir'], result['cutout'], proof_path(result['dir']), f'rn matte {Path(args.file).name}')
    if command == 'grid':
        return _grid_proof(result, proof_path(result['dir']))
    if command == 'template' and result.get('dir'):
        return _sequence_proof(result, proof_path(result['dir']), f'rn template {result.get("template", "")}')
    if command == 'capture':
        if result.get('url'):
            return _web_proof(result, proof_path(result['dir'] if result.get('dir') else args.out)) or None
        return _sequence_proof(result, proof_path(result['dir']), f'rn capture {Path(args.page).name}')
    if command == 'faces' and (result.get('reframe') or {}).get('render'):
        return _reframe_proof(args.file, result, proof_path(result['reframe']['render']['path']))
    if command == 'faces' and result.get('sheet'):
        return result['sheet']['path']
    if command == 'music' and result.get('path') and Path(result['path']).exists() and result['path'] != getattr(args, 'file', None):
        return _waveform_proof(result, proof_path(result['path']))
    if command == 'grade':
        return result.get('proof')
    if command == 'assets' and result.get('previews'):
        from PIL import Image

        from .assets import fetch_preview
        previews = result['previews']
        return write_proof([Image.open(fetch_preview(p['url'])) for p in previews], [p['id'] for p in previews],
                           f'rn assets search {" ".join(args.terms)}'.strip(), proof_path(args.out), columns=4)
    return None


def next_steps(args, result: dict) -> list[str]:
    """At most two exact next commands (or EDL snippets) from this run's own paths and ids."""
    command, out = args.command, []
    if command == 'look' and args.ref:
        for item in result['inputs']:
            vs = item.get('vs_ref') or {}
            if vs.get('match') in ('far', 'noticeable'):
                out.append(f'rn grade {_q(item["path"])} --ref {" ".join(_q(r) for r in args.ref)} -o {_q(Path(item["path"]).stem + ".cube")}')
    elif command == 'grade':
        out.append(f'EDL clip: "vf": "lut3d=file={Path(result["path"]).name}" (path relative to the EDL), '
                   f'then rn look <the render> --ref {" ".join(_q(r) for r in result["refs"])}')
    elif command == 'shots':
        if args.json:
            out.append(f'rn edit edit.json init --from-shots {_q(args.json)}')
        soft = [s['start'] for s in result.get('shots', []) if s.get('transition_in') in ('dissolve', 'fade', 'wipe', 'flash')]
        if soft:
            out.append(f'rn sheet {_q(args.file)} --around {",".join(f"{t:g}" for t in soft[:6])} --span 4   # check the soft boundaries')
    elif command == 'edit' and result.get('op') == 'init':
        first = result['clips'][0] if result['clips'] else 's01'
        if not str(result.get('source', '')).startswith(('gen', '<')) and '<id>' not in str(result.get('source', '')):
            out.append(f'rn assemble {_q(result["path"])} -o rebuild.mp4 --preview')
        else:
            out.append(f'rn edit {_q(result["path"])} set {first} src={result["source"].replace("<id>", first)}')
            out.append(f'rn assemble {_q(result["path"])} -o draft.mp4 --placeholders --preview')
    elif command == 'assemble' and result.get('path'):
        out.append(f'rn qa {_q(result["path"])} --edl {_q(args.edl)}' + (' --placeholders' if args.placeholders else ''))
        out.append(f'rn sheet {_q(result["path"])} --count 12')
    elif command == 'assemble':
        out.append(f'rn assemble {_q(args.edl)} -o {_q(args.out)} --preview')
    elif command == 'qa':
        out += _qa_fixes(args, result)
    elif command == 'listen' and getattr(args, 'speech', False):
        for take in result.get('takes', [result]):
            if (take.get('expect') or {}).get('match') == 'mismatch':
                out.append(f'regenerate {take["path"]}: it does not say the expected line (heard: "{take["expect"]["heard"][:60]}")')
    elif command == 'clean':
        if result.get('video_copied'):
            out.append(f'rn qa {_q(result["path"])} --lufs {result["loudness"]["target"]:g}')
        clarity = result.get('clarity') or {}
        if clarity.get('status') == 'lost':
            out.append(f'rn listen {_q(result["path"])} --speech --transcribe   # words were lost even at light: read what is left')
        elif clarity.get('status') == 'skipped' and not result.get('sample'):
            out.append(f'rn clean {_q(args.file)} -o {_q(args.out)} --strength {result["strength"]["chosen"] or args.strength} --sample'
                       '   # words unchecked: a 10 s before/after pair at the same loudness')
    elif command in ('key', 'matte', 'template') and result.get('edl'):
        what = 'overlay "mask"' if command != 'template' else 'overlay clip'
        out.append(f'EDL {what}: {json.dumps(result["edl"], ensure_ascii=False)}'
                   + ('  ("invert": true keeps graphics off the subject)' if command != 'template' and not result.get('cutout') else ''))
    elif command == 'capture' and result.get('edl'):
        out.append(f'EDL overlay clip: {json.dumps(result["edl"], ensure_ascii=False)}')
    elif command == 'faces' and result.get('reframe'):
        render = result['reframe'].get('render')
        out.append(f'EDL main-track clip: {{"src": "{render["path"]}"}}' if render else
                   f'rn faces {_q(args.file)} --reframe {result["reframe"]["size"][0]}x{result["reframe"]["size"][1]} -o reframed.mp4')
    elif command == 'music' and result.get('edl'):
        out.append(f'EDL audio item: {json.dumps(result["edl"], ensure_ascii=False)}')
    elif command == 'assets' and result.get('files'):
        for f in result['files']:
            note = '  (path relative to the EDL)' if 'vf' in f['edl'] else ''
            out.append(f'EDL {f["place"]}: {json.dumps(f["edl"], ensure_ascii=False)}{note}')
    elif command == 'assets' and result.get('assets'):
        out.append(f'rn assets get {" ".join(_q(a["id"]) for a in result["assets"][:3])} -o {_q(args.out)}')
    elif command == 'pack' and result.get('written'):
        sfx = [w['id'] for w in result['written'] if w['id'].split('/')[0] not in ('font', 'look', 'icon')]
        if sfx:
            out.append(f'rn edit edit.json hits {sfx[0]} --at cuts')
    return out[:2]


def _qa_fixes(args, result: dict) -> list[str]:
    bad = {c['check'] for c in result.get('checks', []) if c['status'] != 'PASS'}
    stem = Path(args.file).stem
    out = []
    if bad & {'loudness', 'true_peak', 'faststart', 'codec', 'pix_fmt', 'sar', 'color', 'fixed_frame_rate'}:
        out.append(f'rn finish {_q(args.file)} -o {_q(stem + "-fixed.mp4")}   # loudness, peak, codec, faststart, frame-rate flag')
    if 'black_bars' in bad:
        out.append('EDL: "style": {"object-fit": "cover"} on the letterboxed clip (or crop it with "vf")')
    if bad & {'black_frames', 'frozen_frames', 'dead_air', 'level_jumps', 'clicks'}:
        times = [c['detail'] for c in result['checks'] if c['check'] in ('black_frames', 'frozen_frames') and c['status'] != 'PASS']
        out.append(f'rn sheet {_q(args.file)} --fps 2   # look at what qa flagged' + (f' ({times[0][:60]})' if times else '')
                   + '; if intended: rn qa ... --accept CHECK@T')
    return out


# ---------------------------------------------------------------- run-time estimate

# Seconds of work per second of 1080x1920 video, for a Renoise sandbox. Measured once on an Apple M-series laptop (12
# cores) on a 10 s 1080x1920 30 fps clip: shots 1.0 s wall / 3 s CPU, key 10 s, finish 2 s, assemble (2 clips + a
# transition) 3.4 s, compare --shots 2.2 s; matte (rembg) and listen --transcribe (Whisper small) from their usual CPU
# rates. Then scaled up about 3x for fewer, slower sandbox cores. Pixels scale linearly (not below a quarter of 1080p).
# ponytail: a rough guide for when to background a command, not a progress bar; recalibrate if sandboxes change.
PER_SECOND = {'shots': 0.6, 'assemble': 2.0, 'faces': 0.4, 'reframe': 1.5, 'key': 3.0, 'matte': 15.0, 'text': 1.0,
              'transcribe': 1.2, 'compare_shots': 1.2, 'compare_video': 2.0, 'ref': 1.8}
SCROLL_FRAME_S = 0.25  # rn capture URL --scroll-video: one browser screenshot per frame
WARN_S = 30


def estimate_seconds(kind: str, duration: float, width: int = 1080, height: int = 1920, every: int = 1) -> float:
    """Rough sandbox seconds for `kind` over `duration` seconds of width x height video (audio-only kinds ignore size)."""
    pixels = 1.0 if kind == 'transcribe' else max(0.25, width * height / (1080 * 1920))
    return PER_SECOND[kind] * duration * pixels / max(1, every)


def _media(path: str) -> tuple[float, int, int]:
    from .ff import probe
    info = probe(path)
    video = info.get('video') or {}
    duration = float(video.get('duration') or info.get('duration') or (info.get('audio') or {}).get('duration') or 0)
    return duration, int(video.get('width') or 0), int(video.get('height') or 0)


def estimate(args) -> tuple[float, str] | None:
    """(seconds, what) for the commands whose time grows with the media, or None."""
    command = args.command
    if command == 'capture' and getattr(args, 'scroll_video', None):
        return round(SCROLL_FRAME_S * (args.duration or 0) * (args.fps or 30) + 5), f'{args.duration} s scroll recording'
    if command == 'clean':
        import importlib.util

        from .clean import CHECK_SECONDS, STRENGTHS
        if importlib.util.find_spec('faster_whisper') is None:
            return None
        heard = min(_media(args.file)[0], CHECK_SECONDS)
        tries = 2 + STRENGTHS.index(args.strength)  # the original, then each strength from the requested one down
        return estimate_seconds('transcribe', heard * tries), f'clarity check: up to {tries} transcriptions of {heard:.0f} s'
    kind = {'shots': 'shots', 'key': 'key', 'matte': 'matte', 'text': 'text', 'ref': 'ref'}.get(command)
    if command == 'faces':
        kind = 'reframe' if getattr(args, 'reframe', None) else 'faces'
    elif command == 'listen' and getattr(args, 'transcribe', False):
        kind = 'transcribe'
    elif command == 'compare' and (args.shots or args.video):
        kind = 'compare_shots' if args.shots else 'compare_video'
    if command == 'assemble' and not (args.preview or args.range or args.dry_run):
        from .assemble import assemble_command
        planned = assemble_command(args.edl, args.out, dry_run=True, keep=False, placeholders=args.placeholders)['output']
        seconds = estimate_seconds('assemble', planned['duration'], planned['width'], planned['height'])
        return seconds, f'{planned["duration"]} s of {planned["width"]}x{planned["height"]} video'
    if not kind:
        return None
    path = args.a if command == 'compare' else args.files[0] if command == 'sheet' else args.file
    if command in ('key', 'matte') and not _is_video(path):
        return None
    duration, width, height = _media(path)
    seconds = estimate_seconds(kind, duration, width or 1080, height or 1920, every=getattr(args, 'every', None) or 1)
    return seconds, f'{duration:.1f} s of {width}x{height} video' if width else f'{duration:.1f} s of audio'


def warn_if_long(args) -> float | None:
    import sys
    try:
        found = estimate(args)
    except Exception:  # an estimate never stops the command; the command reports its own errors
        return None
    if not found:
        return None
    seconds, what = found
    if seconds > WARN_S:
        print(f'rn: about {round(seconds)} s for this ({what}); in a sandbox run it with background: true', file=sys.stderr)
    return round(seconds, 1)


def after(args, result: dict, estimate_s: float | None) -> list[str]:
    """Adds proof / next / estimate_s to the result and returns the summary's closing lines."""
    lines = []
    if estimate_s is not None:
        result['estimate_s'] = estimate_s
    if not isinstance(result, dict):
        return lines
    try:
        path = proof(args, result)
    except Exception as error:  # a proof that cannot be drawn never fails the command
        path, lines = None, [f'warning: no proof image ({type(error).__name__}: {error})']
    if path:
        result['proof'] = path
        lines.append(f'proof: {path}')
    steps = next_steps(args, result)
    if steps:
        result['next'] = steps
        lines += [f'next: {s}' for s in steps]
    return lines
