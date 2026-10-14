"""The edit decision list (edit.json): parsed into a frame-exact timeline. Contract: spec/mcp/03-media-kit.md §4."""
from __future__ import annotations

import json
import math
import re
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
KEYS_MAIN = {'scale', 'x', 'y', 'rotate'}
KEYS_OVERLAY = {'scale', 'x', 'y', 'rotate', 'opacity'}
_SEQ = re.compile(r'%0?(\d*)d')
ROLES = {'music', 'voice', 'sfx'}


@dataclass
class Clip:
    where: str
    kind: str  # video | sequence | image | color
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
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return float(self.total_frames / self.fps)

    def summary(self) -> dict:
        return {
            'output': {'width': self.width, 'height': self.height, 'fps': str(self.fps), 'frames': self.total_frames,
                       'duration': round(self.duration, 4)},
            'base': [{'where': c.where, 'start_frame': c.timeline_start, 'frames': c.frames, 'transition_frames': c.transition_frames}
                     for c in self.base],
            'overlays': [{'where': c.where, 'start_frame': c.timeline_start, 'frames': c.frames} for c in self.overlays],
            'audio': [{'where': a.where, 'role': a.role, 'at': a.at} for a in self.audio],
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
    {"clip": i} / {"clip_end": i} (main-track clip i as laid out, transitions included), {"marker": "name"},
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
        if not isinstance(index, int) or not 0 <= index < len(timeline.base):
            raise RnError(f'{where}.{kind}: main-track clip index 0..{len(timeline.base) - 1}')
        clip = timeline.base[index]
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


def _clip(raw: dict, where: str, edit_dir: Path, fps: Fraction, *, overlay: bool, warnings: list[str],
          timeline: Timeline | None = None) -> Clip:
    if not isinstance(raw, dict):
        raise RnError(f'{where}: expected an object')
    kinds = [k for k in ('src', 'frames', 'image', 'color') if k in raw]
    if len(kinds) != 1:
        raise RnError(f'{where}: give exactly one of "src" (video), "frames" (image sequence), "image" or "color"')
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

    if 'src' in raw or 'frames' in raw:
        if 'src' in raw:
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
            clip.frames = _frames((min(end, src_len) - clip.start_s) / speed, fps)
            if end > src_len + 0.5 / float(src_fps):
                warnings.append(f'{where}: out {end:.3f}s is past the end of {clip.source.name} ({src_len:.3f}s); clamped')
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
    else:
        for key in ('at', 'x', 'y', 'w', 'h', 'opacity', 'fade_in', 'fade_out'):
            if key in raw:
                raise RnError(f'{where}.{key}: main-track clips play back to back; put positioned clips on another track')
        transition = raw.get('transition')
        if transition is not None:
            if not isinstance(transition, dict) or transition.get('type') not in XFADE:
                raise RnError(f'{where}.transition: {{"type": one of xfade names (fade, dissolve, wipeleft, slideup, ...), "duration": s}}')
            clip.transition = transition['type']
            clip.transition_frames = _frames(_num(transition.get('duration', 0.5), f'{where}.transition.duration', positive=True), fps)
    clip.keys = _keys(raw.get('keys'), f'{where}.keys', KEYS_OVERLAY if overlay else KEYS_MAIN)
    if clip.keys and overlay and ('scale' in clip.keys or 'rotate' in clip.keys) and clip.kind == 'color' and not (clip.w and clip.h):
        raise RnError(f'{where}.keys: give a colour overlay "w" and "h" to scale or rotate it')
    unknown = set(raw) - {'src', 'frames', 'fps', 'image', 'color', 'in', 'out', 'duration', 'until', 'speed', 'fit', 'audio', 'gain_db',
                          'at', 'x', 'y', 'w', 'h', 'opacity', 'fade_in', 'fade_out', 'transition', 'keys', 'note'}
    if unknown:
        raise RnError(f'{where}: unknown field(s) {sorted(unknown)}')
    return clip


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
    unknown = set(raw) - {'src', 'role', 'at', 'in', 'out', 'duration', 'gain_db', 'fade_in', 'fade_out', 'note'}
    if unknown:
        raise RnError(f'{where}: unknown field(s) {sorted(unknown)}')
    return AudioItem(where=where, source=source, role=role, at=_anchor(raw.get('at', 0), f'{where}.at', timeline), start_s=start,
                     duration=duration, gain_db=_num(raw.get('gain_db', 0), f'{where}.gain_db'),
                     fade_in=_num(raw.get('fade_in', 0), f'{where}.fade_in', minimum=0),
                     fade_out=_num(raw.get('fade_out', 0), f'{where}.fade_out', minimum=0))


def load(path: str | Path) -> Edit:
    path = Path(path).resolve()
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
    base = [_clip(c, f'tracks[0].clips[{i}]', edit_dir, fps, overlay=False, warnings=warnings) for i, c in enumerate(tracks[0]['clips'])]
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
    overlays = []
    for t, track in enumerate(tracks[1:], start=1):
        if not isinstance(track, dict) or not isinstance(track.get('clips'), list):
            raise RnError(f'tracks[{t}]: expected {{"clips": [...]}}')
        for i, c in enumerate(track['clips']):
            clip = _clip(c, f'tracks[{t}].clips[{i}]', edit_dir, fps, overlay=True, warnings=warnings, timeline=timeline)
            if clip.timeline_start >= total:
                raise RnError(f'{clip.where}.at: starts after the main track ends ({float(total / fps):.3f}s)')
            if clip.timeline_start + clip.frames > total:
                warnings.append(f'{clip.where}: runs past the end of the main track; cut at {float(total / fps):.3f}s')
                clip.frames = total - clip.timeline_start
            overlays.append(clip)
    audio = [_audio(a, f'audio[{i}]', edit_dir, timeline) for i, a in enumerate(raw.get('audio') or [])]

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
    crf = int(_num(output.get('crf', 18), 'output.crf', minimum=0))
    preset = output.get('preset', 'medium')
    unknown = set(raw) - {'output', 'tracks', 'audio', 'subtitles', 'mix', 'markers', 'note', 'version'}
    if unknown:
        raise RnError(f'unknown top-level field(s) {sorted(unknown)}')
    return Edit(path=path, width=width, height=height, fps=fps, background=background, crf=crf, preset=preset, base=base,
                overlays=overlays, audio=audio, subtitles=subtitles, subtitle_style=style, lufs=lufs, true_peak=true_peak,
                duck_db=duck, total_frames=total, warnings=warnings)


def seconds(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def ceil_frames(seconds_value: float, fps: Fraction) -> int:
    return math.ceil(seconds_value * fps - 1e-9)
