"""The edit decision list (edit.json): parsed into a frame-exact timeline. Contract: spec/mcp/03-media-kit.md §4."""
from __future__ import annotations

import json
import math
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
ROLES = {'music', 'voice', 'sfx'}


@dataclass
class Clip:
    where: str
    kind: str  # video | image | color
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


def _position(value, where: str, source_fps: Fraction | None) -> float:
    """Seconds, or {"frame": n} / {"time": s} inside a source."""
    if isinstance(value, dict):
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


def _clip(raw: dict, where: str, edit_dir: Path, fps: Fraction, *, overlay: bool, warnings: list[str]) -> Clip:
    if not isinstance(raw, dict):
        raise RnError(f'{where}: expected an object')
    kinds = [k for k in ('src', 'image', 'color') if k in raw]
    if len(kinds) != 1:
        raise RnError(f'{where}: give exactly one of "src" (video), "image" or "color"')
    fit = raw.get('fit', 'cover')
    if fit not in FITS:
        raise RnError(f'{where}.fit: one of {sorted(FITS)}')
    speed = _num(raw.get('speed', 1.0), f'{where}.speed', positive=True)
    if not 0.1 <= speed <= 8:
        raise RnError(f'{where}.speed: keep between 0.1 and 8')
    gain = _num(raw.get('gain_db', 0.0), f'{where}.gain_db')
    clip = Clip(where=where, kind='video', source=None, color=None, start_s=0.0, speed=speed, frames=0, fit=fit,
                audio=bool(raw.get('audio', not overlay)), gain_db=gain)

    if 'src' in raw:
        clip.source = _resolve(edit_dir, raw['src'], f'{where}.src')
        info = probe(clip.source)
        video = info['video']
        if not video or video['still']:
            raise RnError(f'{where}.src: {clip.source.name} has no video; use "image" for stills')
        src_fps = Fraction(video['fps'])
        clip.src_fps = src_fps
        clip.matrix = source_matrix(video)
        clip.decoder = video.get('alpha_decoder')
        src_len = float(video['frames'] / src_fps) if video['frames'] else (video['duration'] or info['duration'] or 0)
        clip.has_audio = info['audio'] is not None
        clip.start_s = _position(raw.get('in', 0), f'{where}.in', src_fps)
        if clip.start_s >= src_len:
            raise RnError(f'{where}.in: {clip.start_s:.3f}s is past the end of {clip.source.name} ({src_len:.3f}s)')
        if 'duration' in raw and 'out' in raw:
            raise RnError(f'{where}: give "out" or "duration", not both')
        if 'duration' in raw:
            clip.frames = _frames(_num(raw['duration'], f'{where}.duration', positive=True), fps)
        else:
            end = _position(raw['out'], f'{where}.out', src_fps) if 'out' in raw else src_len
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
        clip.timeline_start = _frames(_num(raw.get('at', 0), f'{where}.at', minimum=0), fps)
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
    unknown = set(raw) - {'src', 'image', 'color', 'in', 'out', 'duration', 'speed', 'fit', 'audio', 'gain_db', 'at', 'x', 'y',
                          'w', 'h', 'opacity', 'fade_in', 'fade_out', 'transition', 'note'}
    if unknown:
        raise RnError(f'{where}: unknown field(s) {sorted(unknown)}')
    return clip


def _audio(raw: dict, where: str, edit_dir: Path) -> AudioItem:
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
    return AudioItem(where=where, source=source, role=role, at=_num(raw.get('at', 0), f'{where}.at', minimum=0), start_s=start,
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
            position -= clip.transition_frames
        clip.timeline_start = position
        position += clip.frames
    total = position
    overlays = []
    for t, track in enumerate(tracks[1:], start=1):
        if not isinstance(track, dict) or not isinstance(track.get('clips'), list):
            raise RnError(f'tracks[{t}]: expected {{"clips": [...]}}')
        for i, c in enumerate(track['clips']):
            clip = _clip(c, f'tracks[{t}].clips[{i}]', edit_dir, fps, overlay=True, warnings=warnings)
            if clip.timeline_start >= total:
                raise RnError(f'{clip.where}.at: starts after the main track ends ({float(total / fps):.3f}s)')
            if clip.timeline_start + clip.frames > total:
                warnings.append(f'{clip.where}: runs past the end of the main track; cut at {float(total / fps):.3f}s')
                clip.frames = total - clip.timeline_start
            overlays.append(clip)
    audio = [_audio(a, f'audio[{i}]', edit_dir) for i, a in enumerate(raw.get('audio') or [])]

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
    unknown = set(raw) - {'output', 'tracks', 'audio', 'subtitles', 'mix', 'note', 'version'}
    if unknown:
        raise RnError(f'unknown top-level field(s) {sorted(unknown)}')
    return Edit(path=path, width=width, height=height, fps=fps, background=background, crf=crf, preset=preset, base=base,
                overlays=overlays, audio=audio, subtitles=subtitles, subtitle_style=style, lufs=lufs, true_peak=true_peak,
                duck_db=duck, total_frames=total, warnings=warnings)


def seconds(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def ceil_frames(seconds_value: float, fps: Fraction) -> int:
    return math.ceil(seconds_value * fps - 1e-9)
