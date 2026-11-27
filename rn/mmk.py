"""rn export EDL -o x.mmkproj: the edit as a MultiMediaKit project, the format the Renoise app previews and `mmk export`
renders. Built through MMK's own edit transactions (`mmk edit`), so MMK writes and checks every file. edit.json stays the
source: the project is written fresh each time. Needs mmk 0.2.0 or later.

Converts what MMK can express today: cuts, trims, constant speed and speed ramps (pieces of constant speed, RAMP_STEP
frames each, using the source as rn assemble does), cover / contain / stretch, cross-dissolve and dips to black or white,
video, still and image-sequence clips and overlays placed and sized, opacity, fades, keys (scale, x, y, rotate, opacity,
gain_db), the screen and plus-lighter blends, masks (the matte as a clip on its own track, covering the whole overlay),
the sound of clips and audio items with gain and fades, mix.duck_db (`mmk duck`'s levels added to each music item's own)
and subtitles (`mmk subtitles`: the cues in MMK's own style). Sound rn does not play (an overlay's, "audio": false) is
muted. Anything else (other transitions, solids other than black, nested edits, other blends, vf / af, cut fades, ASS
styles) is left out and listed under "dropped"; rn assemble still renders the full edit. Loudness is an export setting:
mmk export --lufs. --strict refuses an export that would leave anything out. A nested edit is refused: render it to a
clip first.

Beside the project it writes rn/: the edit itself, every file it reads hard-linked under rn/files/ (one already in rn/
stays where it is) and its paths rewritten relative to rn/, so the folder alone (copied, published, pulled into another
sandbox) still holds the edit it was made from. Change rn/edit.json and export it again.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .edl import _FILTER_FILE, _SEQ, AudioItem, Clip, Edit, curve_value, load, source_offsets
from .ff import RnError, probe

TICKS = 120000  # MMK's timebase: ticks per second
SILENT_DB = -80.0  # where a fade starts or ends
RAMP_STEP = 2  # output frames per constant-speed piece of a speed ramp (mmk ramp's default)
TRANSITIONS = {'fade': ('mmk.transition.cross_dissolve', {}), 'dissolve': ('mmk.transition.cross_dissolve', {}),
               'fadeblack': ('mmk.transition.dip_to_color', {'color': [0.0, 0.0, 0.0, 1.0]}),
               'fadewhite': ('mmk.transition.dip_to_color', {'color': [1.0, 1.0, 1.0, 1.0]})}
BLENDS = {'screen': 'screen', 'addition': 'add'}  # rn's (ffmpeg's) blend -> MMK's /blend_mode
PICTURES = ('video', 'image', 'sequence')
# rn's eases as cubic Béziers (easings.net); linear and hold are MMK's own
BEZIER = {'in': [0.55, 0.085, 0.68, 0.53], 'out': [0.25, 0.46, 0.45, 0.94], 'inout': [0.455, 0.03, 0.515, 0.955],
          'in3': [0.55, 0.055, 0.675, 0.19], 'out3': [0.215, 0.61, 0.355, 1.0], 'inout3': [0.645, 0.045, 0.355, 1.0]}
BLACK = {'black', '#000000', '#000'}


def mmk_binary() -> str:
    # The Renoise sandbox installs it into ~/.local/bin, which a non-login shell may not have on PATH
    local = Path.home() / '.local' / 'bin' / 'mmk'
    found = os.environ.get('RN_MMK') or shutil.which('mmk') or (str(local) if local.exists() else None)
    if not found:
        raise RnError('mmk is not installed (set RN_MMK to its path)')
    return found


def _mmk(*args: str, request: dict | None = None) -> dict:
    run = subprocess.run([mmk_binary(), *args], input=None if request is None else json.dumps(request),
                         capture_output=True, text=True)
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError:
        result = None
    if run.returncode != 0 or result is None:
        detail = (run.stdout.strip() or run.stderr.strip())[-2000:]
        raise RnError(f'mmk {args[0]} failed: {detail}')
    return result


def _matte_problem(clip: Clip) -> str | None:
    """Why an overlay's mask cannot be an MMK matte, or None. Outside its matte MMK hides the layer where rn leaves it
    unmasked, so the matte must cover the whole overlay."""
    mask = clip.mask
    if mask.kind not in PICTURES:
        return f'mask ({mask.kind})'
    if mask.keys:
        return 'mask with keys'
    if mask.timeline_start > clip.timeline_start or mask.timeline_start + mask.frames < clip.timeline_start + clip.frames:
        return 'mask shorter than the overlay'
    return None


class _Converter:
    def __init__(self, edit: Edit, package: Path, matte_tracks: dict[int, str]):
        self.edit, self.package = edit, package
        self.rate = edit.fps
        frame_ticks = Fraction(TICKS) / edit.fps
        if frame_ticks.denominator != 1:
            raise RnError(f'output.fps {edit.fps}: MMK needs a whole number of 1/{TICKS} s per frame')
        self.frame_ticks = int(frame_ticks)
        self.matte_tracks = matte_tracks  # id(mask clip) -> its track
        self.ops: list[dict] = []
        self.dropped: list[dict] = []
        self.assets: dict[tuple, dict] = {}  # (source, sequence) -> {id, uri, probed}
        self.sounds: set[str] = set()  # main-track clips' sound, for the transitions between them
        self.roles: dict[str, list[str]] = {'voice': [], 'music': []}  # audio items, for mix.duck_db
        self.held: dict[str, list] = {}  # music items to duck: their level in dB [(t, dB, ease)], written after mmk duck

    # -- helpers --
    def overlay_track(self, lane: int) -> str:
        return f'track_V{lane + 2}'

    def audio_track(self, lane: int) -> str:
        return f'track_A{lane + 2}'

    def ticks(self, frames: int) -> int:
        return frames * self.frame_ticks

    def drop(self, where: str, *what: str) -> None:
        if not what:
            return
        entry = next((d for d in self.dropped if d['item'] == where), None)
        if entry is None:
            self.dropped.append(entry := {'item': where, 'dropped': []})
        entry['dropped'] += what

    def asset(self, source: Path, kind: str, sequence: tuple[int, Fraction] | None = None) -> dict:
        """A file (or a numbered image sequence: its first number and fps) linked into assets/, imported and probed."""
        source = source.resolve()
        if (source, sequence) not in self.assets:
            stem = re.sub(r'[^A-Za-z0-9_.-]', '_', source.parent.name if sequence else source.stem) or 'media'
            suffix = '' if sequence else source.suffix.lower()
            name, n = f'{stem}{suffix}', 1
            while (self.package / 'assets' / name).exists():
                n += 1
                name = f'{stem}-{n}{suffix}'
            target = self.package / 'assets' / name
            target.parent.mkdir(exist_ok=True)
            if sequence:
                # A folder of the numbered files: MMK reads from `start` up to the first one missing
                start, fps = sequence
                width = _SEQ.search(source.name)
                digits = int(width.group(1)) if width.group(1) else 1
                target.mkdir()
                number = start
                while (frame := source.parent / f'{source.name[:width.start()]}{number:0{digits}d}{source.name[width.end():]}').exists():
                    _link(frame, target / frame.name)
                    number += 1
                uri, path = f'assets/{name}/{source.name}#fps={fps}&start={start}', f'{target / source.name}#fps={fps}&start={start}'
            else:
                _link(source, target)
                uri, path = f'assets/{name}', str(target)
            probed = _mmk('probe', path)
            probed.pop('frame_count', None)
            # By what the file holds, not by its first use: one file can be an audio item and a clip
            if kind != 'image':
                kind = 'video' if (probed.get('streams') or {}).get('video') else 'audio'
            asset_id = f'asset_{len(self.assets) + 1}'
            self.assets[(source, sequence)] = {'id': asset_id, 'uri': uri, 'probed': probed}
            self.ops.append({'op': 'import_asset', 'asset': {'id': asset_id, 'kind': kind, 'uri': uri}})
            self.ops.append({'op': 'update_asset_probe', 'asset': asset_id, 'probed': probed})
        return self.assets[(source, sequence)]

    def picture(self, clip: Clip) -> dict:
        if clip.kind == 'sequence':
            return self.asset(clip.source, 'video', (clip.seq_start, clip.src_fps))
        return self.asset(clip.source, clip.kind)

    @staticmethod
    def pixels(asset: dict) -> tuple[float, float]:
        video = (asset['probed'].get('streams') or {}).get('video') or {}
        width, height = video.get('width'), video.get('height')
        if not width or not height:
            raise RnError(f'{asset["uri"]}: mmk probe found no picture size')
        if video.get('rotation') in (90, 270, -90):
            width, height = height, width
        return float(width), float(height)

    def fit(self, fit: str, asset: dict) -> tuple[float, float]:
        """The scale on top of MMK's own fit (contain, centred) that gives rn's cover / contain / stretch of the frame."""
        sw, sh = self.pixels(asset)
        W, H = self.edit.width, self.edit.height
        contain = min(W / sw, H / sh)
        if fit == 'cover':
            return (max(W / sw, H / sh) / contain,) * 2
        if fit == 'stretch':
            return (W / (sw * contain), H / (sh * contain))
        return (1.0, 1.0)

    def pieces(self, clip: Clip, clip_id: str, first: int, frames: int, tail: int = 0) -> list[tuple[str, int, int, int, float]]:
        """The clip's frames first..first+frames as [(id, first frame, frames, source start in ticks, speed)]: one piece, or
        a speed ramp as pieces of constant speed, each starting where rn's own integration of the curve has the source.
        The last piece is at least `tail` frames: MMK wants a transition no longer than the clip before it."""
        if clip.kind == 'image':
            return [(clip_id, first, frames, 0, 1)]
        ramp = (clip.keys or {}).get('speed')
        if not ramp:
            return [(clip_id, first, frames, round((clip.start_s + first / self.rate * clip.speed) * TICKS), clip.speed)]
        used = source_offsets(ramp, clip.frames, self.rate)  # source seconds before each output frame
        end = first + frames
        cuts = [a for a in range(first, end, RAMP_STEP) if a == first or a <= end - tail]
        out = []
        for a, b in zip(cuts, [*cuts[1:], end]):
            out.append((clip_id if a == first else f'{clip_id}_ramp_{len(out)}', a, b - a, round((clip.start_s + used[a]) * TICKS),
                        (used[b] - used[a]) * float(self.rate) / (b - a)))
        return out

    def keyframes(self, clip_id: str, path: str, points, value, offset_s: float = 0.0) -> None:
        """rn keys [(t, v, ease)] (ease on the point it eases into) as MMK keyframes (ease on the point it leaves)."""
        frames = []
        for i, (t, v, _) in enumerate(points):
            key = {'time': round((t - offset_s) * TICKS), 'value': value(v)}
            if i + 1 < len(points):
                ease = points[i + 1][2]
                key['interpolation_to_next'] = ease if ease in ('linear', 'hold') else 'cubic_bezier'
                if ease in BEZIER:
                    key['easing'] = BEZIER[ease]
            frames.append(key)
        self.ops.append({'op': 'set_keyframes', 'target': {'object': clip_id, 'path': path}, 'time_space': 'clip_local',
                         'keyframes': frames})

    def position(self, clip_id: str, keys: dict, rest: tuple[float, float], to_xy, frames: int, offset_s: float = 0.0) -> bool:
        """keys x / y as one vec2 track (MMK keys a clip's position whole). False when neither is keyed."""
        xs, ys = keys.get('x'), keys.get('y')
        if not xs and not ys:
            return False
        if not xs or not ys or [(t, e) for t, _, e in xs] == [(t, e) for t, _, e in ys]:
            points = [(t, (v if xs else rest[0], ys[i][1] if ys else rest[1]) if xs else (rest[0], v), e)
                      for i, (t, v, e) in enumerate(xs or ys)]
        else:  # keyed at different times: one key per frame, exact on every frame
            points = [(k / self.rate + offset_s, (curve_value(xs, k / self.rate + offset_s), curve_value(ys, k / self.rate + offset_s)), 'linear')
                      for k in range(frames + 1)]
            points = [(float(t), v, e) for t, v, e in points]
        self.keyframes(clip_id, '/transform/position', points, lambda v: list(to_xy(*v)), offset_s)
        return True

    def prop(self, clip_id: str, path: str, value) -> None:
        self.ops.append({'op': 'set_property', 'target': {'object': clip_id, 'path': path}, 'value': value})

    def mute(self, clip_id: str) -> None:
        # A video clip plays its own sound unless it is linked to a clip on an audio track
        self.ops.append({'op': 'set_clip_audio_speed', 'clip_id': clip_id, 'policy': 'mute'})

    def insert(self, track: str, clip_id: str, asset: dict, at: int, start: int, length: int, speed: float) -> None:
        self.ops.append({'op': 'insert_clip', 'track': track, 'id': clip_id, 'at': at, 'asset_ref': asset['id'],
                         'source_range': {'start': start, 'end': start + length}, 'ripple': 'none'})
        if speed != 1:
            scalar = Fraction(speed).limit_denominator(1000)
            self.ops.append({'op': 'set_linear_time_warp', 'clip_id': clip_id,
                             'scalar': {'num': scalar.numerator, 'den': scalar.denominator}})

    def volume(self, clip_id: str, where: str, gain_db: float, keys: dict | None, fade_in: float, fade_out: float,
               seconds: float, offset_s: float = 0.0, hold: bool = False) -> None:
        """The sound's level in dB over time; `hold` keeps it back for mix.duck_db (ducked)."""
        if keys and 'gain_db' in keys:
            points = [(t, gain_db + v, e) for t, v, e in keys['gain_db']]  # keys add to gain_db, as in rn assemble
            self.drop(where, *(['fade_in', 'fade_out'] if fade_in or fade_out else []))
        elif fade_in or fade_out:
            # ponytail: a fade is a straight line in dB between two keys; more keys if it ever needs to sound linear in amplitude
            points = [(0.0, SILENT_DB if fade_in else gain_db, 'linear')]
            if fade_in:
                points.append((fade_in, gain_db, 'linear'))
            if fade_out:
                points += [(max(fade_in, seconds - fade_out), gain_db, 'linear'), (seconds, SILENT_DB, 'linear')]
        else:
            points = [(0.0, gain_db, 'linear')]
        if hold:
            self.held[clip_id] = points
        elif len(points) > 1:
            self.keyframes(clip_id, '/volume', points, float, offset_s)
        elif gain_db:
            self.prop(clip_id, '/volume', gain_db)

    def ducked(self, duck: dict) -> list[dict]:
        """The held music levels plus mmk duck's (dB offsets from 0: it ran before they were set), as volume keys."""
        lowered = {op['target']['object']: [(k['time'] / TICKS, k['value'], 'linear') for k in op['keyframes']]
                   for op in duck['operations'] if op.get('op') == 'set_keyframes' and op['target']['path'] == '/volume'}
        start = len(self.ops)
        for item_id, points in self.held.items():
            duck_points = lowered.get(item_id, [(0.0, 0.0, 'linear')])
            times = {t for t, _, _ in points} | {t for t, _, _ in duck_points}
            if any(e != 'linear' for _, _, e in points[1:]):  # eased keys: sample every frame, then straight lines
                times |= {k / float(self.rate) for k in range(math.ceil(max(times) * self.rate) + 1)}
            curve = [(t, curve_value(points, t) + curve_value(duck_points, t), 'linear') for t in sorted(times)]
            if len(curve) > 1:
                self.keyframes(item_id, '/volume', curve, float)
            elif curve[0][1]:
                self.prop(item_id, '/volume', curve[0][1])
        ops = self.ops[start:]
        del self.ops[start:]
        return ops

    # -- tracks --
    def main(self) -> None:
        edit, W, H = self.edit, self.edit.width, self.edit.height
        previous: str | None = None
        for i, clip in enumerate(edit.base):
            clip_id = clip.id or f'c{i + 1}'
            transition = TRANSITIONS.get(clip.transition) if clip.transition_frames and previous else None
            if clip.transition_frames and not transition:
                self.drop(clip_id, f'transition {clip.transition} (a hard cut)')
            # The overlap comes from this clip's head: it starts where the previous one ends, its first frames are the
            # transition's handle.
            head = clip.transition_frames
            at = self.ticks(clip.timeline_start + head)
            frames = clip.frames - head - clip.short_by
            dropped = [name for name in ('vf', 'af', 'blend') if getattr(clip, name)]
            if clip.mask is not None:
                dropped.append('mask')
            if clip.short_by:
                dropped.append(f'hold of the last frame ({clip.short_by} frames, left empty)')
            if clip.kind not in PICTURES:
                if clip.kind != 'color' or clip.color.lower() not in BLACK | {edit.background.lower()}:
                    dropped.insert(0, f'{clip.kind} clip (left empty)')
                self.drop(clip_id, *dropped)
                previous = None
                continue
            asset = self.picture(clip)
            base = self.fit(clip.fit, asset)
            keys = clip.keys or {}
            following = edit.base[i + 1] if i + 1 < len(edit.base) else None
            tail = following.transition_frames if following and following.transition in TRANSITIONS else 0
            pieces = self.pieces(clip, clip_id, head, frames, tail)
            for piece_id, first, count, start, speed in pieces:
                self.insert('track_V1', piece_id, asset, self.ticks(clip.timeline_start + first), start, self.ticks(count), speed)
                offset = first / self.rate
                if 'scale' in keys:
                    self.keyframes(piece_id, '/transform/scale', keys['scale'], lambda v: [base[0] * v, base[1] * v], offset)
                elif base != (1.0, 1.0):
                    self.prop(piece_id, '/transform/scale', list(base))
                self.position(piece_id, keys, (0.0, 0.0), lambda x, y: (W / 2 + x * W, H / 2 + y * H), count, offset)
                if 'rotate' in keys:
                    self.keyframes(piece_id, '/transform/rotation', keys['rotate'], math.radians, offset)
                if clip.kind == 'video' and clip.has_audio and not clip.audio:
                    self.mute(piece_id)
            if transition:
                definition, parameters = transition
                self.ops.append({'op': 'add_transition', 'id': f't_{clip_id}', 'left': previous, 'right': clip_id,
                                 'definition': definition, 'parameters': parameters,
                                 'in_offset': self.ticks(head), 'out_offset': 0})
            if clip.kind == 'video' and clip.audio and clip.has_audio:  # never a ramp: rn drops a ramp's sound
                sound = f'{clip_id}_audio'
                self.insert('track_A1', sound, asset, at, pieces[0][3], self.ticks(frames), clip.speed)
                if clip.speed != 1:
                    self.ops.append({'op': 'set_clip_audio_speed', 'clip_id': sound, 'policy': 'preserve_pitch'})
                self.volume(sound, clip_id, clip.gain_db, clip.keys, 0, 0, frames / self.rate, head / self.rate)
                self.ops.append({'op': 'link_clips', 'clips': [clip_id, sound]})
                if transition and f'{previous}_audio' in self.sounds:
                    self.ops.append({'op': 'add_transition', 'id': f't_{sound}', 'left': f'{previous}_audio', 'right': sound,
                                     'definition': 'mmk.transition.cross_dissolve', 'in_offset': self.ticks(head),
                                     'out_offset': 0})
                self.sounds.add(sound)
            self.drop(clip_id, *dropped)
            previous = pieces[-1][0]
        if edit.cut_fade:
            self.drop('mix', 'cut_fade')

    def matte(self, clip: Clip, clip_id: str) -> str | None:
        """The overlay's mask as a clip on its own track (MMK draws a matte clip only into the layer it masks)."""
        problem = _matte_problem(clip)
        if problem:
            self.drop(clip_id, problem)
            return None
        mask, matte_id = clip.mask, f'{clip_id}_matte'
        asset = self.picture(mask)
        ((_, _, count, start, speed),) = self.pieces(mask, matte_id, 0, mask.frames)
        self.insert(self.matte_tracks[id(mask)], matte_id, asset, self.ticks(mask.timeline_start), start, self.ticks(count), speed)
        base = self.fit(mask.fit, asset)  # a mask covers the whole frame, fitted like a main-track clip
        if base != (1.0, 1.0):
            self.prop(matte_id, '/transform/scale', list(base))
        if mask.kind == 'video' and mask.has_audio:
            self.mute(matte_id)
        return matte_id

    def overlays(self, lanes: list[list[tuple[int, Clip]]]) -> None:
        for lane_no, lane in enumerate(lanes):
            track = self.overlay_track(lane_no)
            for index, clip in lane:
                clip_id = clip.id or f'o{index + 1}'
                dropped = [name for name in ('vf', 'af') if getattr(clip, name)]
                blend = BLENDS.get(clip.blend) if clip.blend and clip.blend != 'normal' else None
                if clip.blend and clip.blend != 'normal' and not blend:
                    dropped.append(f'blend {clip.blend}')
                if clip.kind not in PICTURES:
                    self.drop(clip_id, f'{clip.kind} overlay', *dropped, *(['mask'] if clip.mask is not None else []))
                    continue
                asset = self.picture(clip)
                sw, sh = self.pixels(asset)
                if clip.w and clip.h:
                    ow, oh = clip.w, clip.h
                elif clip.w:
                    ow, oh = clip.w, round(sh * clip.w / sw / 2) * 2
                elif clip.h:
                    ow, oh = round(sw * clip.h / sh / 2) * 2, clip.h
                else:
                    ow, oh = sw, sh
                contain = min(self.edit.width / sw, self.edit.height / sh)
                base = (ow / (sw * contain), oh / (sh * contain))
                keys = clip.keys or {}
                seconds = float(clip.frames / self.rate)
                if 'opacity' in keys:
                    opacity = (keys['opacity'], lambda v: v * clip.opacity)
                    dropped += ['fade_in', 'fade_out'] if clip.fade_in or clip.fade_out else []
                elif clip.fade_in or clip.fade_out:
                    points = [(0.0, 0.0 if clip.fade_in else clip.opacity, 'linear')]
                    if clip.fade_in:
                        points.append((clip.fade_in, clip.opacity, 'linear'))
                    if clip.fade_out:
                        points += [(max(clip.fade_in, seconds - clip.fade_out), clip.opacity, 'linear'), (seconds, 0.0, 'linear')]
                    opacity = (points, float)
                else:
                    opacity = None
                matte = self.matte(clip, clip_id) if clip.mask is not None else None
                for piece_id, first, count, start, speed in self.pieces(clip, clip_id, 0, clip.frames):
                    self.insert(track, piece_id, asset, self.ticks(clip.timeline_start + first), start, self.ticks(count), speed)
                    offset = first / self.rate
                    if 'scale' in keys:
                        self.keyframes(piece_id, '/transform/scale', keys['scale'], lambda v: [base[0] * v, base[1] * v], offset)
                    else:
                        self.prop(piece_id, '/transform/scale', list(base))
                    if not self.position(piece_id, keys, (clip.x, clip.y), lambda x, y: (x + ow / 2, y + oh / 2), count, offset):
                        self.prop(piece_id, '/transform/position', [clip.x + ow / 2, clip.y + oh / 2])
                    if 'rotate' in keys:
                        self.keyframes(piece_id, '/transform/rotation', keys['rotate'], math.radians, offset)
                    if opacity:
                        self.keyframes(piece_id, '/opacity', *opacity, offset)
                    elif clip.opacity != 1:
                        self.prop(piece_id, '/opacity', clip.opacity)
                    if blend:
                        self.prop(piece_id, '/blend_mode', blend)
                    if matte:
                        self.prop(piece_id, '/matte/clip', matte)
                        self.prop(piece_id, '/matte/mode', clip.mask_mode)
                        if clip.mask_invert:
                            self.prop(piece_id, '/matte/invert', True)
                    if clip.kind == 'video' and clip.has_audio:
                        self.mute(piece_id)
                if clip.kind == 'video' and clip.audio and clip.has_audio:
                    dropped.append('sound of an overlay')
                self.drop(clip_id, *dropped)

    def audio(self, lanes: list[list[tuple[int, AudioItem, float]]]) -> None:
        placed = [(item.id or f'a{index + 1}', item) for lane in lanes for index, item, _ in lane]
        for item_id, item in placed:
            if item.role in self.roles:
                self.roles[item.role].append(item_id)
        ducking = bool(self.edit.duck_db and self.roles['voice'] and self.roles['music'])
        for lane_no, lane in enumerate(lanes):
            track = self.audio_track(lane_no)
            for index, item, seconds in lane:
                item_id = item.id or f'a{index + 1}'
                asset = self.asset(item.source, 'audio')
                self.insert(track, item_id, asset, round(item.at * TICKS), round(item.start_s * TICKS), round(seconds * TICKS), 1)
                self.volume(item_id, item_id, item.gain_db, item.keys, item.fade_in, item.fade_out, seconds,
                            hold=ducking and item.role == 'music')
                self.drop(item_id, *(['af'] if item.af else []))


def _link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _overlay_lanes(overlays: list[Clip]) -> list[list[tuple[int, Clip]]]:
    """Later overlays draw above earlier ones: each goes one lane above every earlier overlay it overlaps."""
    lanes: list[list[tuple[int, Clip]]] = []
    placed: list[tuple[int, Clip]] = []  # (lane, clip)
    for index, clip in enumerate(overlays):
        end = clip.timeline_start + clip.frames
        below = [lane for lane, other in placed if other.timeline_start < end and clip.timeline_start < other.timeline_start + other.frames]
        lane = max(below) + 1 if below else 0
        while len(lanes) <= lane:
            lanes.append([])
        lanes[lane].append((index, clip))
        placed.append((lane, clip))
    return lanes


def _audio_lanes(edit: Edit) -> list[list[tuple[int, AudioItem, float]]]:
    lanes: list[list[tuple[int, AudioItem, float]]] = []
    for index, item in sorted(enumerate(edit.audio), key=lambda pair: pair[1].at):
        available = (probe(item.source)['audio']['duration'] or 0) - item.start_s
        seconds = min(item.duration or available, available, edit.duration - item.at)
        if seconds <= 0:
            continue
        lane = next((l for l in lanes if l[-1][1].at + l[-1][2] <= item.at), None)
        if lane is None:
            lanes.append(lane := [])
        lane.append((index, item, seconds))
    return lanes


def _skeleton(edit: Edit, package: Path, video_tracks: int, audio_tracks: int) -> None:
    def track(name: str, kind: str) -> dict:
        return {'OTIO_SCHEMA': 'Track.1', 'name': name, 'kind': kind, 'enabled': True, 'children': [],
                'metadata': {'mmk': {'id': f'track_{name}'}}}

    timeline = {'OTIO_SCHEMA': 'Timeline.1', 'name': edit.path.stem, 'metadata': {'mmk': {'id': 'timeline_main'}},
                'tracks': {'OTIO_SCHEMA': 'Stack.1', 'name': 'tracks',
                           'children': [track(f'V{n}', 'Video') for n in range(1, video_tracks + 1)]
                           + [track(f'A{n}', 'Audio') for n in range(1, audio_tracks + 1)]}}
    project = {'format': 'mmk', 'schema_version': '1.0.0', 'project_id': f'project_{edit.path.stem}', 'revision': 0,
               'timeline': {'file': 'timeline.otio', 'content_hash': ''},
               'profile': {'width': edit.width, 'height': edit.height,
                           'frame_rate': {'num': edit.fps.numerator, 'den': edit.fps.denominator}, 'timebase': TICKS,
                           'audio_sample_rate': 48000,
                           'color': {'working_space': 'linear_rec709', 'display_transform': 'sRGB',
                                     'alpha_mode': 'premultiplied', 'config_ref': 'builtin-color-v1'}},
               'requires': [], 'assets': {}}
    package.mkdir(parents=True)
    (package / 'timeline.otio').write_text(json.dumps(timeline, indent=2), encoding='utf-8')
    (package / 'project.json').write_text(json.dumps(project, indent=2), encoding='utf-8')


MMK_FILES = ('project.json', 'timeline.otio', 'timeline.otio.prev', 'history', 'assets', 'cache', 'scripts', 'generators')
SOURCE = 'rn'  # the edit the project is exported from (write_source)
PATH_KEYS = ('src', 'image', 'file', 'listen')  # keys whose string is a file the edit reads, relative to the edit
FILTER_KEYS = ('vf', 'af')  # raw filter chains, whose file= options name files too


def write_source(edl_path: str | Path, package: Path, target: Path) -> Path:
    """The edit as `target`/edit.json with every file it reads under `target` (hard links): a file already in the
    project's rn/ keeps its place, any other goes to files/ (named as it was, so assets/material-<id>.mp4 stays
    material-<id>.mp4). Paths become relative to it. Returns the edit's path."""
    edit_path = Path(edl_path).resolve()
    edit_dir = edit_path.parent
    raw = json.loads(edit_path.read_text(encoding='utf-8-sig'))
    own = (package / SOURCE).resolve()
    placed: dict[Path, str] = {}

    def place(source: Path, folder: Path | None = None) -> str:
        source = source.resolve()
        if source in placed:
            return placed[source]
        if source.is_relative_to(own):
            relative = source.relative_to(own)
        else:
            stem, suffix, n = source.stem, source.suffix, 1
            relative = Path('files') / (folder or Path()) / source.name
            while (target / relative).exists():
                n += 1
                relative = Path('files') / (folder or Path()) / f'{stem}-{n}{suffix}'
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        if not (target / relative).exists():
            _link(source, target / relative)
        placed[source] = relative.as_posix()
        return placed[source]

    def sequence(pattern: str) -> str:
        """A numbered image sequence (frames/%04d.png): every file of its folder that fits the pattern, in a folder of its own."""
        path = (edit_dir / pattern).expanduser()
        match = _SEQ.search(path.name)
        if not match or not path.parent.is_dir():
            return pattern
        fits = re.compile(re.escape(path.name[:match.start()]) + r'\d+' + re.escape(path.name[match.end():]) + '$')
        frames = [f for f in sorted(path.parent.iterdir()) if fits.match(f.name)]
        if not frames:
            return pattern
        folder = Path(path.parent.name or 'frames')
        return str(Path(place(frames[0], folder)).parent / path.name).replace('\\', '/')

    def filters(chain: str) -> str:
        def relative(match: re.Match) -> str:
            path = Path((match.group(2) if match.group(2) is not None else match.group(3)).replace('\\:', ':'))
            path = path if path.is_absolute() else edit_dir / path
            return f"{match.group(1)}='{place(path)}'" if path.is_file() else match.group(0)
        return _FILTER_FILE.sub(relative, chain)

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in PATH_KEYS and isinstance(value, str) and (path := (edit_dir / value).expanduser()).is_file():
                if key == 'src' and path.suffix.lower() == '.json':
                    raise RnError(f'{value}: a nested edit cannot go into an MMK project; render it to a clip first '
                                  f'(rn assemble {value} -o clip.mp4) and cut that clip in')
                out[key] = place(path)
            elif key == 'frames' and isinstance(value, str):
                out[key] = sequence(value)
            elif key in FILTER_KEYS and isinstance(value, str):
                out[key] = filters(value)
            else:
                out[key] = walk(value)
        return out

    rewritten = walk(raw)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'edit.json').write_text(json.dumps(rewritten, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return target / 'edit.json'


def _replace_project(package: Path, staging: Path) -> None:
    """Swap MMK's own files and rn/ for the new ones; anything else in the folder (a Renoise workspace's .renoise/,
    hand-off notes) stays. The new files are hard links, so media that sat in the old ones survive."""
    package.mkdir(parents=True, exist_ok=True)
    for name in (*MMK_FILES, SOURCE):
        old = package / name
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        elif old.exists() or old.is_symlink():
            old.unlink()
    for item in staging.iterdir():
        item.rename(package / item.name)
    staging.rmdir()


def _commit(staging: Path, revision: int, key: str, operations: list[dict]) -> dict:
    return _mmk('edit', str(staging), '--request', '-', request={
        'api_version': 'mmk-edit@1', 'base_revision': revision, 'idempotency_key': key, 'mode': 'commit', 'actor': 'rn',
        'operations': operations})


def export_mmk(edl_path: str, out: str, *, strict: bool = False, placeholders: bool = False, source: bool = True) -> dict:
    """placeholders: sources not made yet are rn's placeholder cards (rn edit init on a project); source=False writes no
    rn/edit.json beside the project (a project rn edit changes itself)."""
    package = Path(out)
    if package.exists():
        ours = (package / 'project.json').exists() or (package / '.renoise' / 'state.json').exists()
        if not package.is_dir() or (not ours and any(package.iterdir())):
            raise RnError(f'{out}: exists and is not an MMK project; give a new path')
    version = _mmk('version', '--json').get('version', '0')
    if tuple(int(n) for n in re.findall(r'\d+', version)[:2]) < (0, 2):
        raise RnError(f'mmk {version}: rn export -o .mmkproj needs mmk 0.2.0 or later')
    edit = load(edl_path, placeholders=placeholders)
    nested = next((clip for clip in (*edit.base, *edit.overlays) if clip.kind == 'edit'), None)
    if nested is not None:
        raise RnError(f'{nested.where}: a nested edit cannot go into an MMK project; render it to a clip first '
                      f'(rn assemble {nested.source.name} -o clip.mp4) and cut that clip in')
    overlay_lanes, audio_lanes = _overlay_lanes(edit.overlays), _audio_lanes(edit)
    mattes = [clip.mask for clip in edit.overlays if clip.kind in PICTURES and clip.mask is not None and not _matte_problem(clip)]
    matte_lanes = _overlay_lanes(mattes)
    matte_tracks = {id(mask): f'track_V{2 + len(overlay_lanes) + lane_no}' for lane_no, lane in enumerate(matte_lanes) for _, mask in lane}
    # Built beside it, so sources inside the old project are still there to link from
    staging = package.parent / f'.{package.name}.rn-export'
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _skeleton(edit, staging, 1 + len(overlay_lanes) + len(matte_lanes), 1 + len(audio_lanes))
        convert = _Converter(edit, staging, matte_tracks)
        convert.main()
        convert.overlays(overlay_lanes)
        convert.audio(audio_lanes)
        if edit.background.lower() not in BLACK:
            convert.drop('output', 'background')
        committed = _commit(staging, 0, 'rn-export', convert.ops)
        # The helpers read the committed project and give their edit operations back (--dry-run changes nothing)
        extra = []
        if edit.subtitles:
            extra += _mmk('subtitles', str(staging), str(edit.subtitles), '--dry-run')['operations']
            convert.drop('subtitles', *(['ASS styles'] if edit.subtitles.suffix.lower() != '.srt' else []),
                         *(['force_style'] if edit.subtitle_style else []))
        if convert.held:
            extra += convert.ducked(_mmk('duck', str(staging), '--voice', ','.join(convert.roles['voice']),
                                         '--music', ','.join(convert.roles['music']), '--depth', f'{edit.duck_db:g}', '--dry-run'))
        if extra:
            committed = _commit(staging, committed['revision'], 'rn-export-helpers', extra)
        checked = _mmk('validate', str(staging))
        if strict and convert.dropped:
            raise RnError('--strict: MMK would leave out ' + '; '.join(f'{d["item"]}: {", ".join(d["dropped"])}' for d in convert.dropped[:10])
                          + ' (render those parts to clips and cut them in); the project was not changed')
        # After validate, so MMK checks only its own files
        if source:
            write_source(edl_path, package, staging / SOURCE)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _replace_project(package, staging)
    lufs = f' --lufs {edit.lufs:g}' if edit.lufs is not None else ''
    return {'path': str(package), 'adapter': 'mmk', 'frames': edit.total_frames, 'fps': str(edit.fps),
            'revision': committed.get('revision', checked.get('revision')), 'requires': checked.get('requires', []),
            'assets': len(convert.assets), 'dropped': convert.dropped, 'warnings': checked.get('warnings', []),
            'source': str(package / SOURCE / 'edit.json') if source else None, 'next': f'mmk export {package} out.mp4{lufs}'}
