"""rn export EDL -o x.mmkproj: the edit as a MultiMediaKit project, the format the Renoise app previews and `mmk export`
renders. Built through MMK's own edit transactions (`mmk edit`), so MMK writes and checks every file. edit.json stays the
source: the project is written fresh each time.

Converts what MMK can express today: cuts, trims, constant speed, cover / contain / stretch, cross-dissolve and dips to
black or white, overlays of video and stills placed and sized, opacity, fades, keys (scale, x, y, rotate, opacity, gain_db)
and the sound of clips and audio items with gain and fades. Anything else (other transitions, solids other than black,
image sequences, nested edits, blend, mask, vf / af, speed ramps, subtitles, ducking, cut fades) is left out and listed
under "dropped"; rn assemble still renders the full edit. Loudness is a delivery step: run rn finish on the export.
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

from .edl import AudioItem, Clip, Edit, curve_value, load
from .ff import RnError, probe

TICKS = 120000  # MMK's timebase: ticks per second
SILENT_DB = -80.0  # where a fade starts or ends
TRANSITIONS = {'fade': ('mmk.transition.cross_dissolve', {}), 'dissolve': ('mmk.transition.cross_dissolve', {}),
               'fadeblack': ('mmk.transition.dip_to_color', {'color': [0.0, 0.0, 0.0, 1.0]}),
               'fadewhite': ('mmk.transition.dip_to_color', {'color': [1.0, 1.0, 1.0, 1.0]})}
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


class _Converter:
    def __init__(self, edit: Edit, package: Path):
        self.edit, self.package = edit, package
        self.rate = edit.fps
        frame_ticks = Fraction(TICKS) / edit.fps
        if frame_ticks.denominator != 1:
            raise RnError(f'output.fps {edit.fps}: MMK needs a whole number of 1/{TICKS} s per frame')
        self.frame_ticks = int(frame_ticks)
        self.ops: list[dict] = []
        self.dropped: list[dict] = []
        self.assets: dict[Path, dict] = {}  # source -> {id, uri, probed}
        self.sounds: set[str] = set()  # main-track clips' sound, for the transitions between them

    # -- helpers --
    def ticks(self, frames: int) -> int:
        return frames * self.frame_ticks

    def drop(self, where: str, *what: str) -> None:
        if not what:
            return
        entry = next((d for d in self.dropped if d['item'] == where), None)
        if entry is None:
            self.dropped.append(entry := {'item': where, 'dropped': []})
        entry['dropped'] += what

    def asset(self, source: Path, kind: str) -> dict:
        source = source.resolve()
        if source not in self.assets:
            stem = re.sub(r'[^A-Za-z0-9_.-]', '_', source.stem) or 'media'
            name, n = f'{stem}{source.suffix.lower()}', 1
            while (self.package / 'assets' / name).exists():
                n += 1
                name = f'{stem}-{n}{source.suffix.lower()}'
            target = self.package / 'assets' / name
            target.parent.mkdir(exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            uri = f'assets/{name}'
            probed = _mmk('probe', str(target))
            probed.pop('frame_count', None)
            # By what the file holds, not by its first use: one file can be an audio item and a clip
            if kind != 'image':
                kind = 'video' if (probed.get('streams') or {}).get('video') else 'audio'
            asset_id = f'asset_{len(self.assets) + 1}'
            self.assets[source] = {'id': asset_id, 'uri': uri, 'probed': probed}
            self.ops.append({'op': 'import_asset', 'asset': {'id': asset_id, 'kind': kind, 'uri': uri}})
            self.ops.append({'op': 'update_asset_probe', 'asset': asset_id, 'probed': probed})
        return self.assets[source]

    @staticmethod
    def pixels(asset: dict) -> tuple[float, float]:
        video = (asset['probed'].get('streams') or {}).get('video') or {}
        width, height = video.get('width'), video.get('height')
        if not width or not height:
            raise RnError(f'{asset["uri"]}: mmk probe found no picture size')
        if video.get('rotation') in (90, 270, -90):
            width, height = height, width
        return float(width), float(height)

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

    def insert(self, track: str, clip_id: str, asset: dict, at: int, start: int, length: int, speed: float) -> None:
        self.ops.append({'op': 'insert_clip', 'track': track, 'id': clip_id, 'at': at, 'asset_ref': asset['id'],
                         'source_range': {'start': start, 'end': start + length}, 'ripple': 'none'})
        if speed != 1:
            scalar = Fraction(speed).limit_denominator(1000)
            self.ops.append({'op': 'set_linear_time_warp', 'clip_id': clip_id,
                             'scalar': {'num': scalar.numerator, 'den': scalar.denominator}})

    def volume(self, clip_id: str, where: str, gain_db: float, keys: dict | None, fade_in: float, fade_out: float,
               seconds: float, offset_s: float = 0.0) -> None:
        if keys and 'gain_db' in keys:
            self.keyframes(clip_id, '/volume', keys['gain_db'], float, offset_s)
            self.drop(where, *(['fade_in', 'fade_out'] if fade_in or fade_out else []))
        elif fade_in or fade_out:
            # ponytail: a fade is a straight line in dB between two keys; more keys if it ever needs to sound linear in amplitude
            points = [(0.0, SILENT_DB if fade_in else gain_db, 'linear')]
            if fade_in:
                points.append((fade_in, gain_db, 'linear'))
            if fade_out:
                points += [(max(fade_in, seconds - fade_out), gain_db, 'linear'), (seconds, SILENT_DB, 'linear')]
            self.keyframes(clip_id, '/volume', points, float, offset_s)
        elif gain_db:
            self.prop(clip_id, '/volume', gain_db)

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
            if clip.keys and 'speed' in clip.keys:
                dropped.append('keys.speed')
            if clip.short_by:
                dropped.append(f'hold of the last frame ({clip.short_by} frames, left empty)')
            if clip.kind not in ('video', 'image'):
                if clip.kind != 'color' or clip.color.lower() not in BLACK | {edit.background.lower()}:
                    dropped.insert(0, f'{clip.kind} clip (left empty)')
                self.drop(clip_id, *dropped)
                previous = None
                continue
            asset = self.asset(clip.source, clip.kind)
            start = round((clip.start_s + head / self.rate * clip.speed) * TICKS) if clip.kind == 'video' else 0
            self.insert('track_V1', clip_id, asset, at, start, self.ticks(frames), clip.speed)
            sw, sh = self.pixels(asset)
            contain = min(W / sw, H / sh)
            if clip.fit == 'cover':
                base = (max(W / sw, H / sh) / contain,) * 2
            elif clip.fit == 'stretch':
                base = (W / (sw * contain), H / (sh * contain))
            else:
                base = (1.0, 1.0)
            keys, offset = clip.keys or {}, head / self.rate
            if 'scale' in keys:
                self.keyframes(clip_id, '/transform/scale', keys['scale'], lambda v: [base[0] * v, base[1] * v], offset)
            elif base != (1.0, 1.0):
                self.prop(clip_id, '/transform/scale', list(base))
            self.position(clip_id, keys, (0.0, 0.0), lambda x, y: (W / 2 + x * W, H / 2 + y * H), frames, offset)
            if 'rotate' in keys:
                self.keyframes(clip_id, '/transform/rotation', keys['rotate'], math.radians, offset)
            if transition:
                definition, parameters = transition
                self.ops.append({'op': 'add_transition', 'id': f't_{clip_id}', 'left': previous, 'right': clip_id,
                                 'definition': definition, 'parameters': parameters,
                                 'in_offset': self.ticks(head), 'out_offset': 0})
            if clip.kind == 'video' and clip.audio and clip.has_audio:
                sound = f'{clip_id}_audio'
                self.insert('track_A1', sound, asset, at, start, self.ticks(frames), clip.speed)
                if clip.speed != 1:
                    self.ops.append({'op': 'set_clip_audio_speed', 'clip_id': sound, 'policy': 'preserve_pitch'})
                self.volume(sound, clip_id, clip.gain_db, clip.keys, 0, 0, frames / self.rate, offset)
                self.ops.append({'op': 'link_clips', 'clips': [clip_id, sound]})
                if transition and f'{previous}_audio' in self.sounds:
                    self.ops.append({'op': 'add_transition', 'id': f't_{sound}', 'left': f'{previous}_audio', 'right': sound,
                                     'definition': 'mmk.transition.cross_dissolve', 'in_offset': self.ticks(head),
                                     'out_offset': 0})
                self.sounds.add(sound)
            self.drop(clip_id, *dropped)
            previous = clip_id
        if edit.cut_fade:
            self.drop('mix', 'cut_fade')

    def overlays(self, lanes: list[list[tuple[int, Clip]]]) -> None:
        for lane_no, lane in enumerate(lanes):
            track = f'track_V{lane_no + 2}'
            for index, clip in lane:
                clip_id = clip.id or f'o{index + 1}'
                dropped = [name for name in ('vf', 'af', 'blend') if getattr(clip, name)]
                if clip.mask is not None:
                    dropped.append('mask')
                if clip.kind not in ('video', 'image'):
                    self.drop(clip_id, f'{clip.kind} overlay', *dropped)
                    continue
                asset = self.asset(clip.source, clip.kind)
                start = round(clip.start_s * TICKS) if clip.kind == 'video' else 0
                self.insert(track, clip_id, asset, self.ticks(clip.timeline_start), start, self.ticks(clip.frames), clip.speed)
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
                if 'scale' in keys:
                    self.keyframes(clip_id, '/transform/scale', keys['scale'], lambda v: [base[0] * v, base[1] * v])
                else:
                    self.prop(clip_id, '/transform/scale', list(base))
                if not self.position(clip_id, keys, (clip.x, clip.y), lambda x, y: (x + ow / 2, y + oh / 2), clip.frames):
                    self.prop(clip_id, '/transform/position', [clip.x + ow / 2, clip.y + oh / 2])
                if 'rotate' in keys:
                    self.keyframes(clip_id, '/transform/rotation', keys['rotate'], math.radians)
                seconds = clip.frames / self.rate
                if 'opacity' in keys:
                    self.keyframes(clip_id, '/opacity', keys['opacity'], lambda v: v * clip.opacity)
                    dropped += ['fade_in', 'fade_out'] if clip.fade_in or clip.fade_out else []
                elif clip.fade_in or clip.fade_out:
                    points = [(0.0, 0.0 if clip.fade_in else clip.opacity, 'linear')]
                    if clip.fade_in:
                        points.append((clip.fade_in, clip.opacity, 'linear'))
                    if clip.fade_out:
                        points += [(max(clip.fade_in, float(seconds) - clip.fade_out), clip.opacity, 'linear'),
                                   (float(seconds), 0.0, 'linear')]
                    self.keyframes(clip_id, '/opacity', points, float)
                elif clip.opacity != 1:
                    self.prop(clip_id, '/opacity', clip.opacity)
                if clip.kind == 'video' and clip.audio and clip.has_audio:
                    dropped.append('sound of an overlay')
                self.drop(clip_id, *dropped)

    def audio(self, lanes: list[list[tuple[int, AudioItem, float]]]) -> None:
        for lane_no, lane in enumerate(lanes):
            track = f'track_A{lane_no + 2}'
            for index, item, seconds in lane:
                item_id = item.id or f'a{index + 1}'
                asset = self.asset(item.source, 'audio')
                self.insert(track, item_id, asset, round(item.at * TICKS), round(item.start_s * TICKS), round(seconds * TICKS), 1)
                self.volume(item_id, item_id, item.gain_db, item.keys, item.fade_in, item.fade_out, seconds)
                self.drop(item_id, *(['af'] if item.af else []))


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


def _replace_project(package: Path, staging: Path) -> None:
    """Swap MMK's own files for the new ones; anything else in the folder (a Renoise workspace's .renoise/, hand-off
    notes, the edit itself) stays. The new assets are hard links, so media that sat in the old assets/ survive."""
    package.mkdir(parents=True, exist_ok=True)
    for name in MMK_FILES:
        old = package / name
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        elif old.exists() or old.is_symlink():
            old.unlink()
    for item in staging.iterdir():
        item.rename(package / item.name)
    staging.rmdir()


def export_mmk(edl_path: str, out: str) -> dict:
    package = Path(out)
    if package.exists():
        ours = (package / 'project.json').exists() or (package / '.renoise' / 'state.json').exists()
        if not package.is_dir() or (not ours and any(package.iterdir())):
            raise RnError(f'{out}: exists and is not an MMK project; give a new path')
    edit = load(edl_path)
    overlay_lanes, audio_lanes = _overlay_lanes(edit.overlays), _audio_lanes(edit)
    # Built beside it, so sources inside the old project are still there to link from
    staging = package.parent / f'.{package.name}.rn-export'
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _skeleton(edit, staging, 1 + len(overlay_lanes), 1 + len(audio_lanes))
        convert = _Converter(edit, staging)
        convert.main()
        convert.overlays(overlay_lanes)
        convert.audio(audio_lanes)
        if edit.subtitles:
            convert.drop('subtitles', str(edit.subtitles.name))
        if edit.duck_db:
            convert.drop('mix', 'duck_db')
        if edit.background.lower() not in BLACK:
            convert.drop('output', 'background')
        committed = _mmk('edit', str(staging), '--request', '-', request={
            'api_version': 'mmk-edit@1', 'base_revision': 0, 'idempotency_key': 'rn-export', 'mode': 'commit', 'actor': 'rn',
            'operations': convert.ops})
        checked = _mmk('validate', str(staging))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _replace_project(package, staging)
    return {'path': str(package), 'adapter': 'mmk', 'frames': edit.total_frames, 'fps': str(edit.fps),
            'revision': committed.get('revision', checked.get('revision')), 'requires': checked.get('requires', []),
            'assets': len(convert.assets), 'dropped': convert.dropped, 'warnings': checked.get('warnings', []),
            'next': f'mmk export {package} out.mp4, then rn finish out.mp4 -o final.mp4 (loudness)'}
