"""The edit decision list (edit.json): parsed into a frame-exact timeline. Contract: SPEC.md §4."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .ff import HEX_COLOR, RnError, probe, source_matrix

# ffmpeg xfade transitions (7.x); 'fade' and 'dissolve' cover most edits.
XFADE = {
    'fade', 'fadeblack', 'fadewhite', 'fadegrays', 'dissolve', 'pixelize', 'distance', 'radial', 'circleopen', 'circleclose',
    'circlecrop', 'rectcrop', 'wipeleft', 'wiperight', 'wipeup', 'wipedown', 'wipetl', 'wipetr', 'wipebl', 'wipebr',
    'slideleft', 'slideright', 'slideup', 'slidedown', 'smoothleft', 'smoothright', 'smoothup', 'smoothdown',
    'horzopen', 'horzclose', 'vertopen', 'vertclose', 'diagtl', 'diagtr', 'diagbl', 'diagbr', 'hlslice', 'hrslice',
    'vuslice', 'vdslice', 'hblur', 'squeezeh', 'squeezev', 'zoomin', 'coverleft', 'coverright', 'coverup', 'coverdown',
    'revealleft', 'revealright', 'revealup', 'revealdown',
}
FITS = {'cover', 'contain', 'stretch'}
EASES = {'linear', 'in', 'out', 'inout', 'in3', 'out3', 'inout3', 'hold'}
KEYS_MAIN = {'scale', 'x', 'y', 'rotate', 'speed', 'gain_db'}
KEYS_OVERLAY = {'scale', 'x', 'y', 'rotate', 'opacity', 'speed'}
KEYS_AUDIO = {'gain_db'}
# ffmpeg blend modes, computed in RGB; "add" is accepted for "addition".
BLENDS = {'normal', 'addition', 'multiply', 'screen', 'overlay', 'darken', 'lighten', 'difference', 'exclusion', 'softlight',
          'hardlight', 'colordodge', 'colorburn', 'linearlight', 'vividlight', 'pinlight', 'average', 'negation', 'phoenix'}
_GRAPH_BREAKERS = re.compile(r'[;\[\]]')
# The same curves as assemble's ffmpeg expressions (assemble._EASE), for what rn computes itself (speed ramps).
EASE_FN = {
    'linear': lambda u: u, 'hold': lambda u: 0.0, 'in': lambda u: u * u, 'out': lambda u: 1 - (1 - u) ** 2,
    'in3': lambda u: u ** 3, 'out3': lambda u: 1 - (1 - u) ** 3,
    'inout': lambda u: 2 * u * u if u < 0.5 else 1 - 2 * (1 - u) ** 2,
    'inout3': lambda u: 4 * u ** 3 if u < 0.5 else 1 - 4 * (1 - u) ** 3,
}
SPEED_STEPS = 8  # integration sub-steps per output frame
_SEQ = re.compile(r'%0?(\d*)d')
ROLES = {'music', 'voice', 'sfx'}
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def curve_value(points: list[tuple[float, float, str]], t: float) -> float:
    """Keyframes at time t: held before the first and after the last, eased into each key."""
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0, _), (t1, v1, ease) in zip(points, points[1:]):
        if t < t1:
            return v0 + (v1 - v0) * EASE_FN[ease]((t - t0) / (t1 - t0))
    return points[-1][1]


def source_offsets(points: list[tuple[float, float, str]], frames: int, fps: Fraction) -> list[float]:
    """Seconds of source consumed before each output frame n = 0..frames of a speed curve (output time on its x axis)."""
    dt = 1 / (float(fps) * SPEED_STEPS)
    offsets, tau, t = [0.0], 0.0, 0.0
    for _ in range(frames):
        for _ in range(SPEED_STEPS):
            tau += curve_value(points, t + dt / 2) * dt
            t += dt
        offsets.append(tau)
    return offsets


def frames_for_span(points: list[tuple[float, float, str]], span: float, fps: Fraction) -> int:
    """Output frames a speed curve needs to consume `span` seconds of source (the nearest whole frame)."""
    dt = 1 / (float(fps) * SPEED_STEPS)
    previous = tau = t = 0.0
    frames = 0
    while tau < span and frames < 1_000_000:
        previous = tau
        for _ in range(SPEED_STEPS):
            tau += curve_value(points, t + dt / 2) * dt
            t += dt
        frames += 1
    return frames - 1 if span - previous < tau - span else frames


@dataclass
class Clip:
    where: str
    kind: str  # video | sequence | image | color | edit (a nested EDL, rendered before the slots)
    source: Path | None
    color: str | None
    start_s: float  # position inside the source, seconds from its first frame
    speed: float
    frames: int  # frames on the output timeline
    fit: str
    audio: bool
    gain_db: float
    has_audio: bool = False
    src_fps: Fraction | None = None
    matrix: str = 'bt709'  # YUV matrix to decode the source with (ff.source_matrix)
    decoder: str | None = None  # forced decoder, e.g. libvpx-vp9 to keep WebM alpha
    short_by: int = 0  # timeline frames the source cannot cover (filled by holding its last frame)
    transition: str | None = None
    transition_frames: int = 0
    timeline_start: int = 0
    # overlays only
    x: int = 0
    y: int = 0
    w: int | None = None
    h: int | None = None
    opacity: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    # keyframed transform, {prop: [(t, value, ease)]}, t in seconds from the clip's first frame
    keys: dict | None = None
    seq_start: int = 0  # sequences: first file number
    src_size: tuple[int, int] | None = None  # sequences and images: pixel size
    id: str | None = None
    meta: object = None  # kept as given, never interpreted
    vf: str | None = None  # raw ffmpeg video filters, applied in the clip's own chain
    af: str | None = None  # raw ffmpeg audio filters (main-track clips' own sound)
    blend: str | None = None  # overlays: blend mode against everything below
    mask: 'Clip | None' = None  # overlays: a full-frame matte timed on the timeline
    mask_mode: str = 'luma'  # luma | alpha
    mask_invert: bool = False


@dataclass
class AudioItem:
    where: str
    source: Path
    role: str
    at: float
    start_s: float
    duration: float | None
    gain_db: float
    fade_in: float
    fade_out: float
    id: str | None = None
    meta: object = None
    af: str | None = None
    keys: dict | None = None  # gain_db over time, t from the item's first sample on the timeline


@dataclass
class Edit:
    path: Path
    width: int
    height: int
    fps: Fraction
    background: str
    crf: int
    preset: str
    base: list[Clip]
    overlays: list[Clip]
    audio: list[AudioItem]
    subtitles: Path | None
    subtitle_style: str | None
    lufs: float | None
    true_peak: float
    duck_db: float
    total_frames: int = 0
    cut_fade: float = 0.0  # seconds of fade on each side of a hard cut in the main track's own sound
    accept: list[dict] = field(default_factory=list)  # qa warnings judged intended: {"check", "at"?, "note"?}
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return float(self.total_frames / self.fps)

    def summary(self) -> dict:
        return {
            'output': {'width': self.width, 'height': self.height, 'fps': str(self.fps), 'frames': self.total_frames,
                       'duration': round(self.duration, 4)},
            'base': [{'where': c.where, 'id': c.id, 'start_frame': c.timeline_start, 'frames': c.frames,
                      'transition_frames': c.transition_frames} for c in self.base],
            'overlays': [{'where': c.where, 'id': c.id, 'start_frame': c.timeline_start, 'frames': c.frames} for c in self.overlays],
            'audio': [{'where': a.where, 'id': a.id, 'role': a.role, 'at': a.at} for a in self.audio],
            'warnings': self.warnings,
        }


def _num(value, where: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RnError(f'{where}: expected a number of seconds, got {json.dumps(value)}')
    if positive and value <= 0:
        raise RnError(f'{where}: must be > 0')
    if minimum is not None and value < minimum:
        raise RnError(f'{where}: must be >= {minimum}')
    return float(value)


def _position(value, where: str, source_fps: Fraction | None, source: Path | None = None, *, end: bool = False) -> float:
    """Seconds, or {"frame": n} / {"time": s} / {"shot": n, "offset": s} inside a source. A shot is one found by
    `rn cuts` with its defaults; as "in" it means the shot's first frame, as "out" its end."""
    if isinstance(value, dict):
        if 'shot' in value:
            if source is None or source_fps is None:
                raise RnError(f'{where}: shot positions need a video source')
            shot = value['shot']
            if not isinstance(shot, int) or shot < 0:
                raise RnError(f'{where}.shot: expected a non-negative integer (shot numbers from rn cuts)')
            from .cuts import shot_span
            start, stop = shot_span(source, shot)
            return max(0.0, (stop if end else start) + _num(value.get('offset', 0), f'{where}.offset'))
        if 'frame' in value:
            if source_fps is None:
                raise RnError(f'{where}: frame positions need a video source')
            frame = value['frame']
            if not isinstance(frame, int) or frame < 0:
                raise RnError(f'{where}.frame: expected a non-negative integer')
            return float(frame / source_fps)
        if 'time' in value:
            return _num(value['time'], f'{where}.time', minimum=0)
        raise RnError(f'{where}: expected seconds, {{"frame": n}} or {{"time": s}}')
    return _num(value, where, minimum=0)


def _frames(seconds: float, fps: Fraction) -> int:
    return max(0, round(seconds * fps))


def _resolve(edit_dir: Path, value, where: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RnError(f'{where}: expected a file path')
    path = (edit_dir / value).expanduser()
    if not path.exists():
        raise RnError(f'{where}: file not found: {path}')
    return path


@dataclass
class Timeline:
    """What output-timeline anchors resolve against: the laid-out main track, named markers and listen results."""
    fps: Fraction
    edit_dir: Path
    base: list[Clip]
    markers: dict[str, float]
    by_id: dict[str, Clip] = field(default_factory=dict)  # main-track clips and the overlays parsed so far
    ends: dict[str, Clip] = field(default_factory=dict)  # a "ranges" clip's id -> its last piece (for clip_end)
    _listen: dict[str, dict] = field(default_factory=dict)

    def listen(self, value, where: str) -> dict:
        path = _resolve(self.edit_dir, value, where)
        if str(path) not in self._listen:
            try:
                self._listen[str(path)] = json.loads(path.read_text())
            except json.JSONDecodeError as error:
                raise RnError(f'{where}: {path.name} is not JSON ({error}); write it with rn listen --json') from None
        return self._listen[str(path)]


def _anchor(value, where: str, timeline: Timeline) -> float:
    """Seconds on the output timeline: a number, or an anchor plus "offset" seconds:
    {"clip": i} / {"clip_end": i} (main-track clip i as laid out, transitions included; or a clip id, main track or an
    earlier overlay), {"marker": "name"},
    {"frame": n}, {"beat": n, "listen": "music.json"}, {"word": n, "listen": "vo.json", "edge": "start"|"end"}.
    Beat and word times are relative to the analysed file: add "offset" when that audio is placed later."""
    if not isinstance(value, dict):
        return _num(value, where, minimum=0)
    offset = _num(value.get('offset', 0), f'{where}.offset')
    kinds = [k for k in ('clip', 'clip_end', 'marker', 'frame', 'time', 'beat', 'word') if k in value]
    if len(kinds) != 1:
        raise RnError(f'{where}: one of clip, clip_end, marker, frame, beat, word (plus optional offset)')
    kind = kinds[0]
    index = value[kind]
    if kind in ('clip', 'clip_end'):
        if isinstance(index, str):
            clip = timeline.by_id.get(index)
            if clip is None:
                raise RnError(f'{where}.{kind}: no clip with id "{index}" on the main track or in an earlier overlay')
        elif not isinstance(index, int) or not 0 <= index < len(timeline.base):
            raise RnError(f'{where}.{kind}: main-track clip index 0..{len(timeline.base) - 1}, or a clip id')
        else:
            clip = timeline.base[index]
        if kind == 'clip_end' and isinstance(index, str):
            clip = timeline.ends.get(index, clip)
        frame = clip.timeline_start + (clip.frames if kind == 'clip_end' else 0)
        seconds = float(frame / timeline.fps)
    elif kind == 'marker':
        if index not in timeline.markers:
            raise RnError(f'{where}.marker: "{index}" is not in the top-level "markers" ({sorted(timeline.markers) or "none defined"})')
        seconds = timeline.markers[index]
    elif kind == 'frame':
        if not isinstance(index, int) or index < 0:
            raise RnError(f'{where}.frame: a non-negative integer')
        seconds = float(index / timeline.fps)
    elif kind == 'time':
        seconds = _num(index, f'{where}.time', minimum=0)
    else:
        data = timeline.listen(value.get('listen'), f'{where}.listen')
        items = (data.get('beats') or {}).get('times') if kind == 'beat' else data.get('words')
        if not items:
            raise RnError(f'{where}: {value.get("listen")} has no {kind}s (run rn listen{" --transcribe" if kind == "word" else ""} --json)')
        if not isinstance(index, int) or not 0 <= index < len(items):
            raise RnError(f'{where}.{kind}: index 0..{len(items) - 1}')
        if kind == 'beat':
            seconds = float(items[index])
        else:
            edge = value.get('edge', 'start')
            if edge not in ('start', 'end'):
                raise RnError(f'{where}.edge: "start" or "end"')
            seconds = float(items[index][edge])
    return max(0.0, seconds + offset)


def _keys(raw, where: str, allowed: set[str]) -> dict | None:
    """{"scale": [[t, v], [t, v, "out"], ...], ...}: t seconds from the clip's first frame, values held before the
    first and after the last key; the ease names the curve into that key (linear, in, out, inout, in3, out3, inout3, hold)."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise RnError(f'{where}: {{"scale": [[0, 1], [0.5, 1.1, "out"]], ...}}')
    keys = {}
    for prop, points in raw.items():
        if prop not in allowed:
            raise RnError(f'{where}.{prop}: keyable here: {sorted(allowed)}')
        if not isinstance(points, list) or not points:
            raise RnError(f'{where}.{prop}: a list of [t, value] or [t, value, ease]')
        parsed, last = [], -1.0
        for k, point in enumerate(points):
            if not isinstance(point, list) or len(point) not in (2, 3):
                raise RnError(f'{where}.{prop}[{k}]: [t, value] or [t, value, ease]')
            t = _num(point[0], f'{where}.{prop}[{k}] time', minimum=0)
            if t <= last:
                raise RnError(f'{where}.{prop}[{k}]: times must increase')
            value = _num(point[1], f'{where}.{prop}[{k}] value')
            ease = point[2] if len(point) == 3 else 'linear'
            if ease not in EASES:
                raise RnError(f'{where}.{prop}[{k}]: ease one of {sorted(EASES)}')
            if prop == 'scale' and value <= 0:
                raise RnError(f'{where}.scale: must be > 0')
            if prop == 'opacity' and not 0 <= value <= 1:
                raise RnError(f'{where}.opacity: between 0 and 1')
            if prop == 'speed' and not 0.1 <= value <= 8:
                raise RnError(f'{where}.speed: between 0.1 and 8')
            if prop == 'gain_db' and not -60 <= value <= 24:
                raise RnError(f'{where}.gain_db: between -60 and 24 dB')
            parsed.append((t, value, ease))
            last = t
        keys[prop] = parsed
    return keys


def _sequence(edit_dir: Path, pattern, where: str) -> tuple[Path, int, int, tuple[int, int]]:
    """A numbered image sequence (printf pattern like "title/%05d.png") -> (pattern path, first number, count, size)."""
    if not isinstance(pattern, str) or not _SEQ.search(pattern):
        raise RnError(f'{where}: a numbered pattern like "title/%05d.png"')
    path = (edit_dir / pattern).expanduser()
    width = _SEQ.search(path.name)
    if not width:
        raise RnError(f'{where}: the frame number goes in the file name, e.g. "title/%05d.png"')
    digits = width.group(1)
    head, tail = path.name[:width.start()], path.name[width.end():]
    number = re.compile(re.escape(head) + (r'(\d{%s})' % int(digits) if digits else r'(\d+)') + re.escape(tail) + '$')
    numbers = sorted(int(m.group(1)) for f in (path.parent.iterdir() if path.parent.is_dir() else []) if (m := number.match(f.name)))
    if not numbers:
        raise RnError(f'{where}: no files match {path}')
    start = numbers[0]
    count = next((k for k, n in enumerate(numbers) if n != start + k), len(numbers))
    if count < len(numbers):
        raise RnError(f'{where}: numbering has a gap after {head}{start + count - 1}{tail}; a sequence must be contiguous')
    from PIL import Image
    first = path.parent / f'{head}{start:0{int(digits) if digits else 1}d}{tail}'
    with Image.open(first) as image:
        size = image.size
    return path, start, count, size


def cache_dir(edit_dir: Path) -> Path:
    """Renders rn can reuse: slots, nested edits, placeholder cards. RN_CACHE overrides <edit dir>/.rn-cache."""
    return Path(os.environ.get('RN_CACHE') or edit_dir / '.rn-cache')


def _filters(raw: dict, key: str, where: str) -> str | None:
    """A raw ffmpeg filter chain ("eq=saturation=1.2,unsharp"): any filter, but one linear chain, so it cannot reach
    into the rest of the graph."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or _GRAPH_BREAKERS.search(value):
        raise RnError(f'{where}.{key}: one ffmpeg filter chain like "eq=saturation=1.2,unsharp" (no ; [ ])')
    return value


def _ident(raw: dict, where: str) -> tuple[str | None, object]:
    ident = raw.get('id')
    if ident is not None and (not isinstance(ident, str) or not ID.match(ident)):
        raise RnError(f'{where}.id: letters, digits, "_", "-" and "." (starting with a letter or digit)')
    return ident, raw.get('meta')


_SANS_CJK = ('Noto Sans CJK', 'NotoSansCJK', 'PingFang', 'Source Han Sans', 'SourceHanSans', 'Hiragino Sans', 'Heiti', 'WenQuanYi')


def _font(size: int):
    """A face that covers Chinese when fontconfig lists one (placeholder text is often a prompt), else Pillow's default.
    fc-match ':lang=zh' is not enough: it falls back to any font; fc-list only lists fonts that cover the language."""
    from PIL import ImageFont
    if shutil.which('fc-list'):
        files = subprocess.run(['fc-list', ':lang=zh-cn', '--format', '%{file}\n'], capture_output=True, text=True).stdout.split('\n')
        files = [f for f in files if Path(f).suffix.lower() in ('.ttf', '.otf', '.ttc')]
        for path in sorted(files, key=lambda f: not any(name in f for name in _SANS_CJK)):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    """Greedy by measured width; breaks at spaces when a line has one, anywhere otherwise (CJK has no spaces)."""
    lines, line = [], ''
    for char in text:
        if draw.textlength(line + char, font=font) <= width:
            line += char
            continue
        cut = line.rfind(' ')
        if cut > 0 and not char.isspace():
            lines.append(line[:cut])
            line = line[cut + 1:] + char
        else:
            lines.append(line)
            line = char.lstrip()
    return lines + ([line] if line else [])


def _card(edit_dir: Path, size: tuple[int, int], lines: list[str]) -> Path:
    """A placeholder still: dark slate, the clip's name, its meta label or prompt, and what is missing."""
    from PIL import Image, ImageDraw
    digest = hashlib.sha1(json.dumps([size, lines], ensure_ascii=False).encode()).hexdigest()[:16]
    path = cache_dir(edit_dir) / 'placeholders' / f'{digest}.png'
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = size
    image = Image.new('RGB', size, (38, 40, 46))
    draw = ImageDraw.Draw(image)
    unit = max(14, min(width, height) // 18)
    big, small = _font(unit * 2), _font(unit)
    y = height // 6
    draw.text((width // 12, y), lines[0], fill=(255, 214, 90), font=big)
    y += unit * 3
    for line in lines[1:]:
        for wrapped in _wrap(draw, line, small, width * 5 // 6)[:8]:
            draw.text((width // 12, y), wrapped, fill=(230, 230, 230), font=small)
            y += int(unit * 1.4)
        y += unit // 2
    image.save(path)
    return path


def _placeholder(raw: dict, where: str, edit_dir: Path, size: tuple[int, int], warnings: list[str]) -> dict | None:
    """With placeholders on, a clip whose source is missing becomes a card for the same time span."""
    key = next(k for k in ('src', 'frames', 'image', 'color') if k in raw)
    value = raw[key]
    if key == 'color' or not isinstance(value, str):
        return None
    path = (edit_dir / value).expanduser()
    if (path.parent.is_dir() and any(path.parent.iterdir())) if key == 'frames' else path.exists():
        return None
    if 'duration' in raw:
        seconds = raw['duration']
    elif isinstance(raw.get('out'), (int, float)) and isinstance(raw.get('in', 0), (int, float)):
        seconds = (raw['out'] - raw.get('in', 0)) / raw.get('speed', 1)
    else:
        raise RnError(f'{where}: {value} is missing; a placeholder needs "duration" or numeric "in"/"out"')
    meta = raw.get('meta') if isinstance(raw.get('meta'), dict) else {}
    label = meta.get('label') or meta.get('prompt') or ''
    card = _card(edit_dir, size, [raw.get('id') or Path(value).name, *([str(label)] if label else []), f'missing: {value}'])
    warnings.append(f'{where}: {value} is missing; placeholder card for {seconds}s')
    keep = {k: v for k, v in raw.items() if k not in ('src', 'frames', 'image', 'fps', 'in', 'out', 'speed', 'audio', 'gain_db', 'duration', 'until')}
    return {**keep, 'image': str(card), 'duration': seconds, 'fit': raw.get('fit', 'contain')}


def _clip(raw: dict, where: str, edit_dir: Path, fps: Fraction, *, overlay: bool, warnings: list[str],
          timeline: Timeline | None = None, size: tuple[int, int] = (1920, 1080), placeholders: bool = False,
          stack: tuple[Path, ...] = ()) -> Clip:
    if not isinstance(raw, dict):
        raise RnError(f'{where}: expected an object')
    kinds = [k for k in ('src', 'frames', 'image', 'color') if k in raw]
    if len(kinds) != 1:
        raise RnError(f'{where}: give exactly one of "src" (video or a nested edit .json), "frames" (image sequence), "image" or "color"')
    if placeholders:
        raw = _placeholder(raw, where, edit_dir, size, warnings) or raw
    at_s = 0.0
    if overlay:
        assert timeline is not None
        at_s = _anchor(raw.get('at', 0), f'{where}.at', timeline)
        if 'until' in raw:
            if 'duration' in raw or 'out' in raw:
                raise RnError(f'{where}: give "until" or "duration"/"out", not both')
            until = _anchor(raw['until'], f'{where}.until', timeline)
            if until <= at_s:
                raise RnError(f'{where}.until: {until:.3f}s is not after "at" ({at_s:.3f}s)')
            raw = {**raw, 'duration': until - at_s}
    elif 'until' in raw:
        raise RnError(f'{where}.until: main-track clips play back to back; "until" is for overlays')
    fit = raw.get('fit', 'cover')
    if fit not in FITS:
        raise RnError(f'{where}.fit: one of {sorted(FITS)}')
    speed = _num(raw.get('speed', 1.0), f'{where}.speed', positive=True)
    if not 0.1 <= speed <= 8:
        raise RnError(f'{where}.speed: keep between 0.1 and 8')
    gain = _num(raw.get('gain_db', 0.0), f'{where}.gain_db')
    clip = Clip(where=where, kind='video', source=None, color=None, start_s=0.0, speed=speed, frames=0, fit=fit,
                audio=bool(raw.get('audio', not overlay)), gain_db=gain)
    clip.id, clip.meta = _ident(raw, where)
    clip.vf, clip.af = _filters(raw, 'vf', where), _filters(raw, 'af', where)
    if overlay and clip.af:
        raise RnError(f'{where}.af: overlays are silent; filter their sound in "audio"')
    clip.keys = _keys(raw.get('keys'), f'{where}.keys', KEYS_OVERLAY if overlay else KEYS_MAIN)
    ramp = (clip.keys or {}).get('speed')
    if ramp and 'speed' in raw:
        raise RnError(f'{where}: give "speed" or keys.speed, not both')
    if ramp and not ('src' in raw or 'frames' in raw):
        raise RnError(f'{where}.keys.speed: only video, sequences and nested edits play at a speed')
    if 'pivot' in raw and 'until' in raw:
        raise RnError(f'{where}: "pivot" places the clip by a point inside it; it cannot also end "until" an anchor')

    if 'src' in raw or 'frames' in raw:
        if 'src' in raw and str(raw['src']).lower().endswith('.json'):
            # A nested edit: laid out and checked now, rendered (and cached) by assemble before the slots.
            clip.source = _resolve(edit_dir, raw['src'], f'{where}.src').resolve()
            if clip.source in stack:
                raise RnError(f'{where}.src: {clip.source.name} includes itself (through {" -> ".join(p.name for p in stack)})')
            sub = load(clip.source, placeholders=placeholders, _stack=stack)
            clip.kind = 'edit'
            src_fps = sub.fps
            clip.src_size = (sub.width, sub.height)
            src_len = sub.duration
            clip.has_audio = bool(sub.audio or any(c.audio and c.has_audio for c in sub.base))
        elif 'src' in raw:
            clip.source = _resolve(edit_dir, raw['src'], f'{where}.src')
            info = probe(clip.source)
            video = info['video']
            if not video or video['still']:
                raise RnError(f'{where}.src: {clip.source.name} has no video; use "image" for stills, "frames" for a numbered sequence')
            src_fps = Fraction(video['fps'])
            clip.matrix = source_matrix(video)
            clip.decoder = video.get('alpha_decoder')
            clip.src_size = (video['width'], video['height'])
            src_len = float(video['frames'] / src_fps) if video['frames'] else (video['duration'] or info['duration'] or 0)
            clip.has_audio = info['audio'] is not None
        else:
            clip.kind = 'sequence'
            clip.source, clip.seq_start, count, clip.src_size = _sequence(edit_dir, raw['frames'], f'{where}.frames')
            try:
                src_fps = Fraction(str(raw.get('fps', fps)))
            except (ValueError, ZeroDivisionError):
                raise RnError(f'{where}.fps: a number or "30000/1001"') from None
            if src_fps <= 0:
                raise RnError(f'{where}.fps: must be > 0')
            src_len = float(count / src_fps)
            clip.audio = False
            if 'audio' in raw or 'gain_db' in raw:
                raise RnError(f'{where}: image sequences are silent; add their sound to "audio"')
        clip.src_fps = src_fps
        clip.start_s = _position(raw.get('in', 0), f'{where}.in', src_fps, clip.source if clip.kind == 'video' else None)
        if clip.start_s >= src_len:
            raise RnError(f'{where}.in: {clip.start_s:.3f}s is past the end of {clip.source.name} ({src_len:.3f}s)')
        if 'duration' in raw and 'out' in raw:
            raise RnError(f'{where}: give "out" or "duration", not both')
        if 'duration' in raw:
            clip.frames = _frames(_num(raw['duration'], f'{where}.duration', positive=True), fps)
        else:
            end = _position(raw['out'], f'{where}.out', src_fps, clip.source if clip.kind == 'video' else None, end=True) if 'out' in raw else src_len
            if end <= clip.start_s:
                raise RnError(f'{where}.out: must be after "in"')
            span = min(end, src_len) - clip.start_s
            clip.frames = frames_for_span(ramp, span, fps) if ramp else _frames(span / speed, fps)
            if end > src_len + 0.5 / float(src_fps):
                warnings.append(f'{where}: out {end:.3f}s is past the end of {clip.source.name} ({src_len:.3f}s); clamped')
        if ramp:
            available = src_len - clip.start_s
            covered = sum(1 for tau in source_offsets(ramp, clip.frames, fps)[:-1] if tau < available)
            if clip.audio and clip.has_audio:
                warnings.append(f'{where}: a speed ramp drops the clip\'s own sound; place audio in "audio"')
            clip.audio = False
        else:
            covered = _frames((src_len - clip.start_s) / speed, fps)
        if covered < clip.frames:
            clip.short_by = clip.frames - covered
            warnings.append(f'{where}: {clip.source.name} covers {covered} of {clip.frames} frames; holding its last frame')
    else:
        if 'image' in raw:
            clip.kind = 'image'
            clip.source = _resolve(edit_dir, raw['image'], f'{where}.image')
            image = probe(clip.source)['video']
            if not image:
                raise RnError(f'{where}.image: {clip.source.name} is not an image')
            clip.matrix = source_matrix(image)
            clip.src_size = (image['width'], image['height'])
        else:
            clip.kind = 'color'
            clip.color = raw['color']
            if not isinstance(clip.color, str) or not (HEX_COLOR.match(clip.color) or clip.color.isalpha()):
                raise RnError(f'{where}.color: expected "#rrggbb" or a color name')
        clip.audio = False
        clip.frames = _frames(_num(raw.get('duration'), f'{where}.duration', positive=True), fps)
    if clip.frames < 1:
        raise RnError(f'{where}: shorter than one frame at {fps} fps')

    if 'pivot' in raw:
        # The pivot (source seconds; for stills and colours, seconds into the clip) lands on "at".
        if not overlay:
            raise RnError(f'{where}.pivot: main-track clips play back to back; pivot positions overlays and audio')
        if ramp:
            raise RnError(f'{where}.pivot: not with a speed ramp')
        inside = _num(raw['pivot'], f'{where}.pivot', minimum=0) - (clip.start_s if clip.kind in ('video', 'sequence', 'edit') else 0)
        if inside < 0:
            raise RnError(f'{where}.pivot: before the clip\'s "in"')
        at_s -= inside / speed
        if at_s < 0:  # the part before the timeline start is dropped
            dropped = _frames(-at_s, fps)
            if dropped >= clip.frames:
                raise RnError(f'{where}.pivot: puts the whole clip before 0 s')
            clip.start_s += dropped / float(fps) * speed if clip.kind in ('video', 'sequence', 'edit') else 0
            clip.frames -= dropped
            at_s = 0.0
    if overlay:
        clip.timeline_start = _frames(at_s, fps)
        clip.x = int(_num(raw.get('x', 0), f'{where}.x'))
        clip.y = int(_num(raw.get('y', 0), f'{where}.y'))
        clip.w = int(_num(raw['w'], f'{where}.w', positive=True)) if 'w' in raw else None
        clip.h = int(_num(raw['h'], f'{where}.h', positive=True)) if 'h' in raw else None
        clip.opacity = _num(raw.get('opacity', 1.0), f'{where}.opacity', minimum=0)
        if clip.opacity > 1:
            raise RnError(f'{where}.opacity: between 0 and 1')
        clip.fade_in = _num(raw.get('fade_in', 0), f'{where}.fade_in', minimum=0)
        clip.fade_out = _num(raw.get('fade_out', 0), f'{where}.fade_out', minimum=0)
        if 'transition' in raw:
            raise RnError(f'{where}.transition: transitions are for the first (main) track; use fade_in / fade_out on overlays')
        if 'audio' in raw or 'gain_db' in raw:
            raise RnError(f'{where}: overlay clips are silent; add their sound to "audio" with "at" (role "sfx")')
        blend = raw.get('blend')
        if blend is not None:
            blend = {'add': 'addition'}.get(blend, blend)
            if blend not in BLENDS:
                raise RnError(f'{where}.blend: one of {sorted(BLENDS | {"add"})}')
            clip.blend = blend
        if 'mask' in raw:
            clip.mask, clip.mask_mode, clip.mask_invert = _mask(raw['mask'], where, clip, edit_dir, fps, warnings, timeline, size)
    else:
        for key in ('at', 'x', 'y', 'w', 'h', 'opacity', 'fade_in', 'fade_out', 'blend', 'mask'):
            if key in raw:
                raise RnError(f'{where}.{key}: main-track clips play back to back; put positioned clips on another track')
        transition = raw.get('transition')
        if transition is not None:
            if not isinstance(transition, dict) or transition.get('type') not in XFADE:
                raise RnError(f'{where}.transition: {{"type": one of xfade names (fade, dissolve, wipeleft, slideup, ...), "duration": s}}')
            clip.transition = transition['type']
            clip.transition_frames = _frames(_num(transition.get('duration', 0.5), f'{where}.transition.duration', positive=True), fps)
    if clip.keys and 'gain_db' in clip.keys and not (clip.audio and clip.has_audio):
        raise RnError(f'{where}.keys.gain_db: the clip plays no sound of its own')
    if clip.keys and overlay and ('scale' in clip.keys or 'rotate' in clip.keys) and clip.kind == 'color' and not (clip.w and clip.h):
        raise RnError(f'{where}.keys: give a colour overlay "w" and "h" to scale or rotate it')
    unknown = set(raw) - {'src', 'frames', 'fps', 'image', 'color', 'in', 'out', 'duration', 'until', 'speed', 'fit', 'audio', 'gain_db',
                          'at', 'x', 'y', 'w', 'h', 'opacity', 'fade_in', 'fade_out', 'transition', 'keys', 'note', 'id', 'meta',
                          'pivot', 'vf', 'af', 'blend', 'mask'}
    if unknown:
        raise RnError(f'{where}: unknown field(s) {sorted(unknown)}')
    return clip


def _mask(raw, where: str, layer: Clip, edit_dir: Path, fps: Fraction, warnings: list[str], timeline: Timeline,
          size: tuple[int, int]) -> tuple[Clip, str, bool]:
    """A matte over the whole frame (stretched to it) that multiplies an overlay's alpha: "luma" white = keep, or the
    matte's own "alpha". Its time defaults to the overlay's; outside its span the overlay is not masked."""
    where = f'{where}.mask'
    if not isinstance(raw, dict):
        raise RnError(f'{where}: {{"frames": "matte/%05d.png", "mode": "luma"|"alpha", "invert": false}} (any clip source)')
    mode, invert = raw.get('mode', 'luma'), raw.get('invert', False)
    if mode not in ('luma', 'alpha') or not isinstance(invert, bool):
        raise RnError(f'{where}: "mode" luma or alpha, "invert" true or false')
    placed = {'x', 'y', 'w', 'h', 'blend', 'mask', 'opacity'} & set(raw)
    if placed:
        raise RnError(f'{where}: a mask covers the whole frame; {sorted(placed)} do not apply')
    matte = {k: v for k, v in raw.items() if k not in ('mode', 'invert')}
    matte.setdefault('at', float(layer.timeline_start / fps))
    if not {'duration', 'out', 'until'} & set(matte):
        matte['duration'] = float(layer.frames / fps)
    matte.setdefault('fit', 'stretch')
    clip = _clip(matte, where, edit_dir, fps, overlay=True, warnings=warnings, timeline=timeline, size=size)
    clip.w, clip.h = size
    return clip, mode, invert


def _audio(raw: dict, where: str, edit_dir: Path, timeline: Timeline) -> AudioItem:
    if not isinstance(raw, dict):
        raise RnError(f'{where}: expected an object')
    source = _resolve(edit_dir, raw.get('src'), f'{where}.src')
    info = probe(source)
    if not info['audio']:
        raise RnError(f'{where}.src: {source.name} has no audio stream')
    role = raw.get('role', 'sfx')
    if role not in ROLES:
        raise RnError(f'{where}.role: one of {sorted(ROLES)}')
    start = _num(raw.get('in', 0), f'{where}.in', minimum=0)
    duration = None
    if 'duration' in raw and 'out' in raw:
        raise RnError(f'{where}: give "out" or "duration", not both')
    if 'duration' in raw:
        duration = _num(raw['duration'], f'{where}.duration', positive=True)
    elif 'out' in raw:
        duration = _num(raw['out'], f'{where}.out', positive=True) - start
        if duration <= 0:
            raise RnError(f'{where}.out: must be after "in"')
    unknown = set(raw) - {'src', 'role', 'at', 'in', 'out', 'duration', 'gain_db', 'fade_in', 'fade_out', 'note', 'id', 'meta', 'pivot',
                          'af', 'keys'}
    if unknown:
        raise RnError(f'{where}: unknown field(s) {sorted(unknown)}')
    ident, meta = _ident(raw, where)
    at = _anchor(raw.get('at', 0), f'{where}.at', timeline)
    if 'pivot' in raw:
        # The pivot (seconds in the source: a hit, a downbeat, the drop) lands on "at"; what falls before 0 s is cut.
        inside = _num(raw['pivot'], f'{where}.pivot', minimum=0) - start
        if inside < 0:
            raise RnError(f'{where}.pivot: before "in"')
        at -= inside
        if at < 0:
            start -= at
            if duration is not None:
                duration += at
                if duration <= 0:
                    raise RnError(f'{where}.pivot: puts the whole item before 0 s')
            at = 0.0
    return AudioItem(where=where, source=source, role=role, at=at, start_s=start,
                     duration=duration, gain_db=_num(raw.get('gain_db', 0), f'{where}.gain_db'),
                     fade_in=_num(raw.get('fade_in', 0), f'{where}.fade_in', minimum=0),
                     fade_out=_num(raw.get('fade_out', 0), f'{where}.fade_out', minimum=0), id=ident, meta=meta,
                     af=_filters(raw, 'af', where), keys=_keys(raw.get('keys'), f'{where}.keys', KEYS_AUDIO))


def _expand_ranges(clips: list) -> tuple[list[tuple[str, dict]], dict[str, tuple[int, int]]]:
    """A main-track clip with "ranges": [[in, out], ...] plays those spans of one source back to back: one clip per span.
    Its id names the first span (later ones get <id>.1, <id>.2...); {"clip_end": id} is the end of the last span.
    Index anchors count the spans."""
    pieces, groups = [], {}
    for i, raw in enumerate(clips):
        where = f'tracks[0].clips[{i}]'
        if not (isinstance(raw, dict) and 'ranges' in raw):
            pieces.append((where, raw))
            continue
        spans = raw['ranges']
        if not isinstance(spans, list) or not spans or not all(isinstance(x, list) and len(x) == 2 for x in spans):
            raise RnError(f'{where}.ranges: [[in, out], ...] with at least one span')
        clash = {'in', 'out', 'duration', 'keys'} & set(raw)
        if clash:
            raise RnError(f'{where}: "ranges" replaces {sorted(clash)}')
        first = len(pieces)
        for k, (start, end) in enumerate(spans):
            piece = {key: value for key, value in raw.items() if key not in ('ranges', 'transition', 'id')}
            piece.update({'in': start, 'out': end})
            if k == 0 and 'transition' in raw:
                piece['transition'] = raw['transition']
            if raw.get('id'):
                piece['id'] = raw['id'] if k == 0 else f'{raw["id"]}.{k}'
            pieces.append((f'{where}.ranges[{k}]', piece))
        if raw.get('id'):
            groups[raw['id']] = (first, len(pieces) - 1)
    return pieces, groups


def load(path: str | Path, *, placeholders: bool = False, _stack: tuple[Path, ...] = ()) -> Edit:
    """Parses and lays out an EDL. placeholders: a missing source becomes a card for its time span instead of an error."""
    path = Path(path).resolve()
    stack = (*_stack, path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise RnError(f'no such file: {path}') from None
    except json.JSONDecodeError as error:
        raise RnError(f'{path}: invalid JSON: {error}') from None
    edit_dir = path.parent
    output = raw.get('output') or {}
    width = int(_num(output.get('width'), 'output.width', positive=True))
    height = int(_num(output.get('height'), 'output.height', positive=True))
    if width % 2 or height % 2:
        raise RnError('output.width / output.height: must be even for yuv420p')
    try:
        fps = Fraction(str(output.get('fps', 30)))
    except (ValueError, ZeroDivisionError):
        raise RnError('output.fps: a number or "30000/1001"') from None
    if not 1 <= fps <= 120:
        raise RnError('output.fps: between 1 and 120')
    background = output.get('background', '#000000')
    if not isinstance(background, str) or not (HEX_COLOR.match(background) or background.isalpha()):
        raise RnError('output.background: "#rrggbb" or a color name')

    tracks = raw.get('tracks')
    if not isinstance(tracks, list) or not tracks or not isinstance(tracks[0], dict) or not tracks[0].get('clips'):
        raise RnError('tracks: need at least one track whose "clips" list is the main track')
    warnings: list[str] = []
    context = {'size': (width, height), 'placeholders': placeholders, 'stack': stack}
    pieces, groups = _expand_ranges(tracks[0]['clips'])
    base = [_clip(c, where, edit_dir, fps, overlay=False, warnings=warnings, **context) for where, c in pieces]
    if base[0].transition:
        raise RnError('tracks[0].clips[0].transition: the first clip has nothing to transition from')
    position = 0
    for i, clip in enumerate(base):
        if clip.transition_frames:
            limit = min(base[i - 1].frames, clip.frames) - 1
            if clip.transition_frames > limit:
                raise RnError(f'{clip.where}.transition.duration: {clip.transition_frames} frames is longer than the clips it joins allow ({limit})')
            if clip.transition_frames + base[i - 1].transition_frames > base[i - 1].frames:
                raise RnError(f'{clip.where}.transition.duration: overlaps the transition into the previous clip ({base[i - 1].where}); '
                              'shorten one of them or lengthen that clip')
            position -= clip.transition_frames
        clip.timeline_start = position
        position += clip.frames
    total = position
    markers_raw = raw.get('markers') or {}
    if not isinstance(markers_raw, dict):
        raise RnError('markers: {"name": seconds, ...} on the output timeline')
    markers = {}
    for name, value in markers_raw.items():
        markers[name] = _position(value, f'markers.{name}', fps)
    timeline = Timeline(fps=fps, edit_dir=edit_dir, base=base, markers=markers)
    seen: set[str] = set()

    def register(item, clip: Clip | None = None) -> None:
        if item.id is None:
            return
        if item.id in seen:
            raise RnError(f'{item.where}.id: "{item.id}" is used twice; ids must be unique in the edit')
        seen.add(item.id)
        if clip is not None:
            timeline.by_id[item.id] = clip

    for clip in base:
        register(clip, clip)
    for ident, (first, last) in groups.items():
        timeline.ends[ident] = base[last]
    overlays = []
    for t, track in enumerate(tracks[1:], start=1):
        if not isinstance(track, dict) or not isinstance(track.get('clips'), list):
            raise RnError(f'tracks[{t}]: expected {{"clips": [...]}}')
        for i, c in enumerate(track['clips']):
            clip = _clip(c, f'tracks[{t}].clips[{i}]', edit_dir, fps, overlay=True, warnings=warnings, timeline=timeline, **context)
            if clip.timeline_start >= total:
                raise RnError(f'{clip.where}.at: starts after the main track ends ({float(total / fps):.3f}s)')
            if clip.timeline_start + clip.frames > total:
                warnings.append(f'{clip.where}: runs past the end of the main track; cut at {float(total / fps):.3f}s')
                clip.frames = total - clip.timeline_start
            overlays.append(clip)
            register(clip, clip)
        unknown = set(track) - {'clips', 'id', 'meta', 'note'}
        if unknown:
            raise RnError(f'tracks[{t}]: unknown field(s) {sorted(unknown)}')
    audio = [_audio(a, f'audio[{i}]', edit_dir, timeline) for i, a in enumerate(raw.get('audio') or [])]
    for item in audio:
        register(item)

    subtitles = style = None
    if raw.get('subtitles'):
        sub = raw['subtitles']
        if not isinstance(sub, dict):
            raise RnError('subtitles: {"file": "captions.ass", "force_style": "..."}')
        subtitles = _resolve(edit_dir, sub.get('file'), 'subtitles.file')
        if subtitles.suffix.lower() not in ('.ass', '.ssa', '.srt'):
            raise RnError('subtitles.file: .ass or .srt')
        style = sub.get('force_style')
        if style is not None and (not isinstance(style, str) or "'" in style or '\\' in style):
            raise RnError('subtitles.force_style: ASS overrides like "Fontname=Noto Sans CJK SC,Fontsize=48" (no quotes or backslashes)')
    mix = raw.get('mix') or {}
    lufs = mix.get('lufs', -14.0)
    if lufs is not None:
        lufs = _num(lufs, 'mix.lufs')
        if not -40 <= lufs <= -5:
            raise RnError('mix.lufs: between -40 and -5 (or null to keep levels as mixed)')
    true_peak = _num(mix.get('true_peak', -1.0), 'mix.true_peak')
    if not -9 <= true_peak <= 0:
        raise RnError('mix.true_peak: between -9 and 0 dBTP')
    duck = _num(mix.get('duck_db', 0), 'mix.duck_db', minimum=0)
    cut_fade = _num(mix.get('cut_fade', 0), 'mix.cut_fade', minimum=0)
    if cut_fade > 0.25:
        raise RnError('mix.cut_fade: up to 0.25 s (it removes clicks at hard cuts; longer fades are audio items)')
    crf = int(_num(output.get('crf', 18), 'output.crf', minimum=0))
    preset = output.get('preset', 'medium')
    accept = raw.get('accept') or []
    if not isinstance(accept, list) or not all(isinstance(a, dict) and isinstance(a.get('check'), str) for a in accept):
        raise RnError('accept: [{"check": "dead_air", "at": 1.5, "note": "the pause before the drop"}, ...] ("at" optional)')
    for a in accept:
        if 'at' in a:
            _num(a['at'], 'accept[].at', minimum=0)
    unknown = set(raw) - {'output', 'tracks', 'audio', 'subtitles', 'mix', 'markers', 'note', 'version', 'accept'}
    if unknown:
        raise RnError(f'unknown top-level field(s) {sorted(unknown)}')
    return Edit(path=path, width=width, height=height, fps=fps, background=background, crf=crf, preset=preset, base=base,
                overlays=overlays, audio=audio, subtitles=subtitles, subtitle_style=style, lufs=lufs, true_peak=true_peak,
                duck_db=duck, total_frames=total, warnings=warnings, cut_fade=cut_fade, accept=accept)


def seconds(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def ceil_frames(seconds_value: float, fps: Fraction) -> int:
    return math.ceil(seconds_value * fps - 1e-9)
