"""rn edit X.mmkproj: the same operations as on an edit.json, made on a MultiMediaKit project itself. The project is the
edit: every operation is one MMK edit transaction (`mmk edit`, actor "rn", the command as its label), so it lands in the
project's history, the Renoise app shows it step by step and can undo it, and edits made in the app are kept (rn reads
the project each time, never an older copy). rn/edit.json, when an export left one, is not read or written any more.

Times are given in seconds as on an edit.json and become MMK ticks (120000 a second) on whole output frames. Clip ids are
MMK's object ids. Tracks: track_V1 is the main track (back to back), track_A1 its clips' own sound (linked to each
picture), track_V2.. overlays, audio items on track_A2... Changes on the main track ripple the main track and its sound
only (every other track is locked for the step): overlays and audio stay where they are.

What rn keeps beside MMK's own files, in rn/state.json: the rn fields set on each clip (fit, style, keys, fades, ...),
so a later `set` derives MMK's properties the way rn assemble would, and the undo tokens of rn's own steps.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from .edl import load
from .ff import RnError
from .mmk import TICKS, TRANSITIONS, _Converter, _audio_lanes, _link, _mmk

MAIN, SOUND = 'track_V1', 'track_A1'
STATE = 'rn/state.json'
RN_STATE = ('specs', 'xfades', 'baked')  # rn's records of clips, put back by undo
SPEC_FIELDS = {'fit', 'style', 'keys', 'opacity', 'fade_in', 'fade_out', 'gain_db', 'audio', 'blend', 'meta', 'note', 'role'}
TIMING_FIELDS = {'in', 'out', 'duration', 'speed', 'transition', 'src', 'image', 'frames', 'fps', 'at'}
# Which MMK properties each rn field decides: a `set` rewrites only those, so the app's other changes stay
PATHS = {'fit': {'/transform/scale'}, 'style': {'/transform/scale', '/transform/position', '/opacity'},
         'keys': {'/transform/scale', '/transform/position', '/transform/rotation', '/opacity', '/volume'},
         'opacity': {'/opacity'}, 'fade_in': {'/opacity', '/volume'}, 'fade_out': {'/opacity', '/volume'},
         'gain_db': {'/volume'}, 'blend': {'/blend_mode'}}
NOT_YET = {'mask'}  # rn fields an MMK project does not take yet
BAKED = {'vf', 'af'}  # rendered into the clip's footage by rn assemble (MMK has no ffmpeg filters)


def is_project(path: str | Path) -> bool:
    return Path(path).is_dir() and (Path(path) / 'project.json').is_file()


def _error_line(error: RnError) -> str:
    """mmk's JSON error ({code, message, object_id}) as one line."""
    text = str(error)
    start = text.find('{')
    try:
        detail = json.loads(text[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        detail = None
    if not isinstance(detail, dict) or 'code' not in detail:
        return text
    message = re.sub(r'^operation \d+ \((\w+)\): ', r'\1: ', detail.get('message', ''))
    return f'{detail["code"]}: {message}' + (f' ({detail["object_id"]})' if detail.get('object_id') else '')


class Project:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.anchoring = True  # off in batch's trial copy: anchored items follow once, on the real commit
        if not is_project(self.path):
            raise RnError(f'{path}: not an MMK project (no project.json)')
        self.linked: list[Path] = []  # files linked into assets/ by this step, removed if it fails
        self.load()

    # -- reading --
    def load(self) -> None:
        self.timeline = _mmk('info', str(self.path))
        self.doc = json.loads((self.path / 'project.json').read_text(encoding='utf-8'))
        rate = self.timeline['frame_rate']
        self.fps = Fraction(rate['num'], rate['den'])
        self.width, self.height = self.timeline['width'], self.timeline['height']
        frame = Fraction(TICKS) / self.fps
        if frame.denominator != 1:
            raise RnError(f'{self.path}: {self.fps} fps is not a whole number of ticks a frame')
        self.frame = int(frame)
        state = self.path / STATE
        self.state = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
        self.state.setdefault('specs', {})
        self.state.setdefault('undo', [])
        self.state.setdefault('anchors', {})
        self.notes: list[str] = []
        self.before = self._snapshot()

    def _snapshot(self) -> dict:
        return {key: json.loads(json.dumps(self.state.get(key, {}))) for key in RN_STATE}

    @property
    def revision(self) -> int:
        return self.timeline['revision']

    def track(self, track_id: str) -> dict | None:
        return next((t for t in self.timeline['tracks'] if t['id'] == track_id), None)

    def items(self, track_id: str, kind: str = 'clip') -> list[dict]:
        track = self.track(track_id)
        return sorted((i for i in (track or {}).get('items', []) if i['kind'] == kind), key=lambda i: i['range']['start'])

    def find(self, ref: str) -> tuple[dict, dict]:
        for track in self.timeline['tracks']:
            for item in track['items']:
                if item['id'] == ref:
                    return track, item
        clips = [i['id'] for t in self.timeline['tracks'] if t['id'] != SOUND for i in t['items'] if i['kind'] == 'clip']
        raise RnError(f'no clip "{ref}" in {self.path.name} (clips: {", ".join(clips[:20])}{" ..." if len(clips) > 20 else ""})')

    def group(self, clip_id: str) -> list[str]:
        return next((g for g in self.doc.get('link_groups', []) if clip_id in g), [clip_id])

    def sound_of(self, clip_id: str) -> str | None:
        """The main-track clip's own sound: the clip on track_A1 linked to it."""
        sounds = {i['id'] for i in self.items(SOUND)}
        return next((c for c in self.group(clip_id) if c in sounds), None)

    def speed(self, item: dict) -> Fraction:
        speed = item.get('speed') or {'num': 1, 'den': 1}
        return Fraction(speed['num'], speed['den'])

    def source_seconds(self, item: dict) -> tuple[float, float]:
        """Where the clip starts and ends in its source (what it uses, speed included), seconds."""
        start, length = item['source_range']['start'], item['range']['end'] - item['range']['start']
        return start / TICKS, (start + length * self.speed(item)) / TICKS

    def asset_seconds(self, asset_id: str) -> float | None:
        asset = self.doc['assets'].get(asset_id) or {}
        if asset.get('kind') == 'image':
            return None  # a still lasts as long as it is placed
        return (asset.get('duration_ticks') or 0) / TICKS

    def at_frame(self, seconds: float) -> int:
        """Seconds on the timeline as ticks on the nearest whole frame."""
        return round(seconds * TICKS / self.frame) * self.frame

    def seconds(self, ticks: int) -> float:
        return round(ticks / TICKS, 4)

    def fresh(self, base: str) -> str:
        taken = {i['id'] for t in self.timeline['tracks'] for i in t['items']} | {t['id'] for t in self.timeline['tracks']}
        if base not in taken:
            return base
        n = 2
        while f'{base}-{n}' in taken:
            n += 1
        return f'{base}-{n}'

    # -- writing --
    def ripple_main(self, ops: list[dict]) -> list[dict]:
        """ops on the main track that ripple it and its sound only: every other unlocked track is locked around them."""
        others = [t['id'] for t in self.timeline['tracks'] if t['id'] not in (MAIN, SOUND) and not t.get('locked')]
        return ([{'op': 'set_track_locked', 'track': t, 'locked': True} for t in others] + ops
                + [{'op': 'set_track_locked', 'track': t, 'locked': False} for t in others])

    def _send(self, ops: list[dict], label: str) -> dict:
        request = {'api_version': 'mmk-edit@1', 'base_revision': self.revision, 'idempotency_key': f'rn-{uuid.uuid4().hex[:12]}',
                   'mode': 'commit', 'actor': 'rn', 'label': label[:200], 'operations': ops}
        try:
            result = _mmk('edit', str(self.path), '--request', '-', request=request)
        except RnError as error:
            self.unlink_new()
            raise RnError(_error_line(error)) from None
        self.linked = []
        self.load_timeline()
        return result

    def unlink_new(self) -> None:
        """Media this step linked into assets/ and no transaction used: removed when the step fails."""
        for path in self.linked:
            path.unlink(missing_ok=True) if path.is_file() else shutil.rmtree(path, ignore_errors=True)
        self.linked = []

    def load_timeline(self) -> None:
        self.timeline = _mmk('info', str(self.path))
        self.doc = json.loads((self.path / 'project.json').read_text(encoding='utf-8'))

    def commit(self, ops: list[dict], label: str, adopt: dict | None = None) -> dict:
        """One rn step: the operations, then (a second transaction, undone with it) the anchored items that follow. The
        undo entry keeps rn's own state from before the step, which undo puts back. adopt: rn state to take on once the
        operations landed (batch: what its steps recorded)."""
        before = self.before
        if self.anchoring:
            self.notes += self._loosen()
        result = self._send(ops, label)
        if adopt is not None:
            for key in (*RN_STATE, 'anchors'):
                self.state[key] = adopt.get(key, {})
        entry = {'revision': result['revision'], 'tokens': [result['undo_token']], 'label': label[:200], 'state': before}
        moves = self._follow() if self.anchoring else []
        if moves:
            try:
                follow = self._send(moves, f'{label} (anchored items follow)')
                entry['tokens'].append(follow['undo_token'])
                entry['revision'] = follow['revision']
            except RnError as error:
                self.notes.append(f'anchored items could not follow ({error}); they stay where they were')
                for move in moves:
                    self.state['anchors'].pop(move['clip_id'], None)
        self.state['undo'].append(entry)
        self.state['undo'] = self.state['undo'][-50:]
        self.save_state()
        return result

    # -- anchors: overlays and audio items placed at a clip, marker, beat or word follow later edits --
    def _starts(self) -> dict[str, tuple[str, int]]:
        return {i['id']: (t['id'], i['range']['start']) for t in self.timeline['tracks'] for i in t['items'] if i['kind'] == 'clip'}

    def _loosen(self) -> list[str]:
        """Anchored items someone moved (in the app, or rn by an explicit move) keep their place: the anchor goes."""
        starts, notes = self._starts(), []
        for item_id, record in list(self.state['anchors'].items()):
            if item_id not in starts:
                del self.state['anchors'][item_id]
            elif starts[item_id][1] != record['placed']:
                del self.state['anchors'][item_id]
                notes.append(f'{item_id} was moved, so it stays where it is and no longer follows {json.dumps(record["at"])}')
        return notes

    def _follow(self) -> list[dict]:
        starts, moves = self._starts(), []
        for item_id, record in list(self.state['anchors'].items()):
            if item_id not in starts or starts[item_id][1] != record['placed']:
                del self.state['anchors'][item_id]  # this step removed or moved it
                continue
            try:
                target = self.at_frame(anchor_seconds(self, record['at']) - record.get('lead', 0.0))
            except RnError as error:
                del self.state['anchors'][item_id]
                self.notes.append(f'{item_id} stays at {self.seconds(record["placed"]):g}s: its anchor is gone ({error})')
                continue
            if target < 0:
                continue
            if target != record['placed']:
                moves.append({'op': 'move_clip', 'clip_id': item_id, 'to_track': starts[item_id][0], 'to': target, 'ripple': 'none'})
                record['placed'] = target
        return moves

    def anchor(self, item_id: str, at, lead: float = 0.0) -> None:
        """Remember that item_id follows `at` (its start is the anchor minus `lead` seconds, an audio item's pivot)."""
        if not isinstance(at, dict):
            return
        record = dict(at)
        if 'listen' in record:
            record['listen'] = str(Path(record['listen']).expanduser().resolve())
        self.state['anchors'][item_id] = {'at': record, 'placed': self._starts()[item_id][1], 'lead': lead}
        self.save_state()

    def save_state(self) -> None:
        path = self.path / STATE
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.before = self._snapshot()  # what an undo of the next step goes back to

    def import_file(self, source: Path, kind: str, sequence: tuple[int, Fraction] | None, ops: list[dict]) -> dict:
        """The project's asset for a file: one already imported with the same content, or a new one (linked into assets/
        and imported by the ops). Returns {id, uri, probed} as rn.mmk's converter wants it."""
        source = source.resolve()
        for asset_id, asset in self.doc['assets'].items():  # a file of the project itself (a set re-deriving a clip)
            if (self.path / asset['uri'].split('#')[0]).resolve() == source and ('#' in asset['uri']) == bool(sequence):
                return {'id': asset_id, 'uri': asset['uri'], 'probed': asset}
        if not sequence:
            probed = _mmk('probe', str(source))
            for asset_id, asset in self.doc['assets'].items():
                if asset.get('content_hash') and asset.get('content_hash') == probed.get('content_hash'):
                    return {'id': asset_id, 'uri': asset['uri'], 'probed': asset}
        stem = re.sub(r'[^A-Za-z0-9_.-]', '_', source.parent.name if sequence else source.stem) or 'media'
        suffix = '' if sequence else source.suffix.lower()
        name, n = f'{stem}{suffix}', 1
        while (self.path / 'assets' / name).exists():
            n += 1
            name = f'{stem}-{n}{suffix}'
        target = self.path / 'assets' / name
        target.parent.mkdir(exist_ok=True)
        if sequence:
            start, rate = sequence
            match = re.search(r'%0?(\d*)d', source.name)
            digits = int(match.group(1)) if match and match.group(1) else 1
            target.mkdir()
            number = start
            while (frame := source.parent / f'{source.name[:match.start()]}{number:0{digits}d}{source.name[match.end():]}').exists():
                _link(frame, target / frame.name)
                number += 1
            uri, path = f'assets/{name}/{source.name}#fps={rate}&start={start}', f'{target / source.name}#fps={rate}&start={start}'
        else:
            _link(source, target)
            uri, path = f'assets/{name}', str(target)
        self.linked.append(target)
        probed = _mmk('probe', path)
        probed.pop('frame_count', None)
        if kind != 'image':
            kind = 'video' if (probed.get('streams') or {}).get('video') else 'audio'
        taken = set(self.doc['assets'])
        asset_id = next(f'asset_{k}' for k in range(len(taken) + 1, len(taken) + 1000) if f'asset_{k}' not in taken)
        self.doc['assets'][asset_id] = {'uri': uri, 'kind': kind, **probed}  # seen by later look-ups in this step
        ops += [{'op': 'import_asset', 'asset': {'id': asset_id, 'kind': kind, 'uri': uri}},
                {'op': 'update_asset_probe', 'asset': asset_id, 'probed': probed}]
        return {'id': asset_id, 'uri': uri, 'probed': probed}


class _Placer(_Converter):
    """rn.mmk's converter writing into an existing project: assets are looked up first, clips land at `origin` ticks on
    the tracks given, and main-track clips ripple (see Project.ripple_main)."""

    def __init__(self, project: Project, edit, origin: int, overlay_track: str | None = None, audio_track: str | None = None):
        super().__init__(edit, project.path, {})
        self.project, self.origin = project, origin
        self._overlay, self._audio = overlay_track, audio_track

    def overlay_track(self, lane: int) -> str:
        return self._overlay or super().overlay_track(lane)

    def audio_track(self, lane: int) -> str:
        return self._audio or super().audio_track(lane)

    def asset(self, source: Path, kind: str, sequence=None) -> dict:
        key = (source.resolve(), sequence)
        if key not in self.assets:
            self.assets[key] = self.project.import_file(source, kind, sequence, self.ops)
        return self.assets[key]

    def insert(self, track: str, clip_id: str, asset: dict, at: int, start: int, length: int, speed: float) -> None:
        mark = len(self.ops)
        super().insert(track, clip_id, asset, at, start, length, speed)
        op = self.ops[mark]
        op['at'] += self.origin
        if track == MAIN:
            op.update(ripple='all_unlocked_tracks', locked_tracks='skip', links='follow')


def _spec(text: str) -> dict:
    raw = Path(text[1:]).read_text(encoding='utf-8-sig') if text.startswith('@') else text
    try:
        clip = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RnError(f'--clip: invalid JSON: {error}') from None
    if not isinstance(clip, dict):
        raise RnError('--clip: one clip object')
    return clip


def _mini_edit(project: Project, *, main: dict | None = None, overlay: dict | None = None, audio: dict | None = None):
    """One clip laid out by rn's own loader, in an edit of the project's size: paths are relative to the current folder."""
    fps = project.fps
    filler = {'color': 'black', 'duration': 3600}
    raw = {'version': 2, 'output': {'width': project.width, 'height': project.height,
                                    'fps': int(fps) if fps.denominator == 1 else f'{fps.numerator}/{fps.denominator}'},
           'tracks': [{'clips': [main or filler]}] + ([{'clips': [overlay]}] if overlay else []),
           'audio': [audio] if audio else [], 'mix': {'lufs': None}}
    trial = Path.cwd() / f'.rn-clip-{os.getpid()}.json'
    trial.write_text(json.dumps(raw), encoding='utf-8')
    try:
        return load(trial)
    finally:
        trial.unlink(missing_ok=True)


def _clip_ops(project: Project, spec: dict, place: str, at: int, track: str | None) -> tuple[list[dict], str]:
    """The operations that put one rn clip (main, overlay or audio item) into the project at `at` ticks."""
    unsupported = sorted(NOT_YET & set(spec))
    if unsupported:
        raise RnError(f'{", ".join(unsupported)}: not on an MMK project yet (render that part to a clip and insert the clip)')
    spec = {k: v for k, v in spec.items() if k != 'transition'}
    if 'color' in spec:
        return _solid_ops(project, spec, place, at, track), spec['id']
    if BAKED & set(spec):
        if place != 'main':
            raise RnError(f'{"/".join(sorted(BAKED & set(spec)))} on an overlay or audio item: render it first (rn assemble '
                          f'of an edit holding just that item, or ffmpeg) and insert the file')
        spec = _bake(project, spec)
    if place == 'main':
        edit = _mini_edit(project, main=spec)
        placer = _Placer(project, edit, at)
        edit.base[0].id = spec['id']
        placer.main()
    elif place == 'overlay':
        edit = _mini_edit(project, overlay={**spec, 'at': at / TICKS})
        placer = _Placer(project, edit, 0, overlay_track=track)
        placer.overlays([[(0, edit.overlays[0])]])
    else:  # rn's loader puts the pivot on `at`
        edit = _mini_edit(project, audio={**spec, 'at': at / TICKS})
        placer = _Placer(project, edit, 0, audio_track=track)
        placer.audio(_audio_lanes(edit))
    if placer.dropped:
        raise RnError('MMK would leave out ' + '; '.join(f'{d["item"]}: {", ".join(d["dropped"])}' for d in placer.dropped))
    return placer.ops, spec['id']


def _linear(color: str) -> list[float]:
    """A CSS / ffmpeg colour name or #RRGGBB as MMK's linear RGBA."""
    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb(color)[:3]
    except ValueError:
        raise RnError(f'color {color!r}: a name like black or #RRGGBB') from None
    return [round((c / 255) / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4, 6) for c in rgb] + [1.0]


def _solid_ops(project: Project, spec: dict, place: str, at: int, track: str | None) -> list[dict]:
    """A colour clip as an MMK scene (a filled rectangle), laid out by rn's loader (size, place, opacity, fades)."""
    if place == 'audio':
        raise RnError('a colour is a picture, not an audio item')
    if place == 'main':
        clip = _mini_edit(project, main=spec).base[0]
        x, y, w, h = 0, 0, project.width, project.height
    else:
        clip = _mini_edit(project, overlay={**spec, 'at': at / TICKS}).overlays[0]
        x, y, w, h = clip.x, clip.y, clip.w or project.width, clip.h or project.height
    length = clip.frames * project.frame
    scene = project.fresh(f'scene_{spec["id"]}')
    insert = {'op': 'insert_clip', 'track': MAIN if place == 'main' else track, 'id': spec['id'], 'at': at,
              'generator': {'kind': 'mmk.scene', 'target': scene}, 'source_range': {'start': 0, 'end': length}}
    if place == 'main':
        insert.update(ripple='all_unlocked_tracks', locked_tracks='skip', links='follow')
    ops = [{'op': 'create_scene', 'definition': {
        'schema': 'mmk.scene@1', 'id': scene, 'canvas': {'width': project.width, 'height': project.height}, 'duration_ticks': length,
        'root': 'fill', 'nodes': {'fill': {'type': 'rect', 'size': [w, h], 'fill': _linear(clip.color),
                                           'transform': {'position': [x, y], 'anchor': [0, 0], 'scale': [1, 1], 'rotation': 0,
                                                         'opacity': 1}}}}}, insert]
    seconds = clip.frames / float(project.fps)
    points = []
    if clip.fade_in or clip.fade_out:
        points = [(0.0, 0.0 if clip.fade_in else clip.opacity)] + ([(clip.fade_in, clip.opacity)] if clip.fade_in else [])
        points += [(max(clip.fade_in, seconds - clip.fade_out), clip.opacity), (seconds, 0.0)] if clip.fade_out else []
        ops.append({'op': 'set_keyframes', 'target': {'object': spec['id'], 'path': '/opacity'}, 'time_space': 'clip_local',
                    'keyframes': [{'time': round(t * TICKS), 'value': v, 'interpolation_to_next': 'linear'} for t, v in points]})
    elif clip.opacity != 1:
        ops.append({'op': 'set_property', 'target': {'object': spec['id'], 'path': '/opacity'}, 'value': clip.opacity})
    return ops


def _bake(project: Project, spec: dict) -> dict:
    """A main-track clip with vf / af rendered by rn assemble into rn/baked/ (frame-exact, at the project's size and rate),
    which the project then plays as footage. The rn clip is kept in rn/state.json, so a later set renders it again from
    its own source."""
    import hashlib

    from .assemble import assemble_command
    clips = spec['ranges_of'] if 'ranges_of' in spec else [{k: v for k, v in spec.items() if k not in ('id', 'meta', 'note', 'transition')}]
    fps = project.fps
    raw = {'version': 2, 'output': {'width': project.width, 'height': project.height,
                                    'fps': int(fps) if fps.denominator == 1 else f'{fps.numerator}/{fps.denominator}'},
           'tracks': [{'clips': clips}], 'mix': {'lufs': None}}
    sources = sorted(str(Path(v).expanduser().resolve()) for body in clips for k, v in body.items()
                     if k in ('src', 'image') and isinstance(v, str))
    stamp = [(path, os.path.getmtime(path), os.path.getsize(path)) for path in sources if os.path.exists(path)]
    key = hashlib.sha1(json.dumps([raw, stamp], sort_keys=True).encode()).hexdigest()[:10]
    out = project.path / 'rn' / 'baked' / f'{spec["id"]}-{key}.mp4'
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        trial = Path.cwd() / f'.rn-bake-{os.getpid()}.json'
        trial.write_text(json.dumps(raw), encoding='utf-8')
        try:
            assemble_command(str(trial), str(out), dry_run=False, keep=False, cache=False, full_size=True)
        finally:
            trial.unlink(missing_ok=True)
    project.state.setdefault('baked', {})[spec['id']] = spec
    project.notes.append(f'{spec["id"]}: {"/".join(sorted(BAKED & set(spec)))} rendered into rn/baked/{out.name}; the app plays '
                         f'it as footage (set {spec["id"]} vf=... renders it again, --unset vf puts the source back)')
    return {'id': spec['id'], 'src': str(out), **({'audio': spec['audio']} if 'audio' in spec else {})}


# -- anchors: where an overlay or audio item starts --
def markers(project: Project) -> dict[str, float]:
    """Named markers in seconds on the timeline: on tracks (timeline time) and on clips (source time, so they move
    with the clip: rn edit marks puts beats and words on the clip that plays them)."""
    otio = json.loads((project.path / 'timeline.otio').read_text(encoding='utf-8'))
    places = {i['id']: i for t in project.timeline['tracks'] for i in t['items'] if i['kind'] == 'clip'}
    found: dict[str, float] = {}
    for track in otio['tracks']['children']:
        for marker in track.get('markers') or []:
            found.setdefault(marker['name'], marker['marked_range']['start_time']['value'] / TICKS)
        for child in track.get('children') or []:
            item = places.get(((child.get('metadata') or {}).get('mmk') or {}).get('id'))
            for marker in child.get('markers') or []:
                if item is None:
                    continue
                source = marker['marked_range']['start_time']['value']
                at = item['range']['start'] + (source - item['source_range']['start']) / project.speed(item)
                if item['range']['start'] <= at <= item['range']['end']:
                    found.setdefault(marker['name'], at / TICKS)
    return found


def anchor_seconds(project: Project, value, where: str = 'at') -> float:
    """rn's anchor (seconds, {"clip"} / {"clip_end"} with an id or main-track index, {"marker"}, {"frame"}, {"time"},
    {"beat"/"word", "listen"}, each with "offset") resolved by rn's own rules on the project's timeline as it is now."""
    from .edl import Timeline, _anchor
    frame = project.frame

    def laid(item: dict) -> SimpleNamespace:
        return SimpleNamespace(timeline_start=item['range']['start'] // frame,
                               frames=(item['range']['end'] - item['range']['start']) // frame)
    base = [laid(i) for i in project.items(MAIN)]
    by_id = {i['id']: laid(i) for t in project.timeline['tracks'] if t['kind'] == 'video' for i in t['items'] if i['kind'] == 'clip'}
    timeline = Timeline(fps=project.fps, edit_dir=Path.cwd(), base=base, markers=markers(project), by_id=by_id)
    return _anchor(value, where, timeline)


def _at(project: Project, value) -> int:
    return project.at_frame(anchor_seconds(project, value))


# -- the operations --
def _main_index(project: Project, ref: str) -> int:
    ids = [i['id'] for i in project.items(MAIN)]
    if ref not in ids:
        raise RnError(f'{ref} is not on the main track')
    return ids.index(ref)


def split_op(project: Project, ref: str, at: float, new_id: str | None) -> dict:
    track, item = project.find(ref)
    tick = project.at_frame(at)
    start, end = item['range']['start'], item['range']['end']
    if not start < tick < end:
        raise RnError(f'--at {at}: {ref} plays {project.seconds(start)}..{project.seconds(end)} s; split inside it')
    second = new_id or project.fresh(f'{ref}-2')
    project.commit([{'op': 'split_clip', 'clip_id': ref, 'at': tick, 'id': second, 'links': 'follow'}], f'split {ref} --at {at}')
    if ref in project.state['specs']:
        project.state['specs'][second] = dict(project.state['specs'][ref])
        project.save_state()
    return {'op': 'split', 'clip': ref, 'into': [ref, second]}


def delete_op(project: Project, ref: str, lift: bool) -> dict:
    track, item = project.find(ref)
    if track['id'] == SOUND:
        picture = next((c for c in project.group(ref) if c != ref), None)
        raise RnError(f'{ref} is the sound of {picture}: rn edit set {picture} audio=false silences it'
                      if picture else f'{ref} is a main-track clip\'s own sound')
    if track['id'] == MAIN and not lift:
        # Ripples the main track; each later clip's sound follows its picture (linked)
        ops = [{'op': 'ripple_delete', 'objects': [ref], 'links': 'follow', 'ripple': 'track'}]
    else:
        ops = [{'op': 'delete', 'objects': [ref], 'links': 'follow', 'ripple': 'none'}]
    project.commit(ops, f'delete {ref}' + (' --lift' if lift else ''))
    project.state['specs'].pop(ref, None)
    project.save_state()
    return {'op': 'delete', 'clip': ref, 'lift': lift}


def _overlay_track(project: Project, number: int, ops: list[dict]) -> str:
    """Overlay track N (1 = the first above the main track) as track_V<N+1>, added when it is one past the last."""
    tracks = [t['id'] for t in project.timeline['tracks'] if t['kind'] == 'video']
    track_id = f'track_V{number + 1}'
    if track_id in tracks:
        return track_id
    if number + 1 != len(tracks) + 1:
        raise RnError(f'--track {number}: there are {len(tracks) - 1} overlay tracks; {len(tracks)} adds one')
    ops.append({'op': 'add_track', 'kind': 'video', 'id': track_id, 'name': f'V{number + 1}', 'index': len(tracks)})
    return track_id


def _audio_track(project: Project, role: str | None, ops: list[dict], span: tuple[int, int] | None = None,
                 planned: dict[str, list[tuple[int, int]]] | None = None) -> str:
    """The audio track for a role (track_voice, track_music, track_sfx; track_A2 without one), or the next of its lanes
    (track_sfx_2, ...) when `span` would overlap what is there or what this step already puts there (`planned`)."""
    base = f'track_{role}' if role in ('voice', 'music', 'sfx') else 'track_A2'
    lane = 1
    while True:
        track_id = base if lane == 1 else f'{base}_{lane}'
        taken = [(i['range']['start'], i['range']['end']) for i in project.items(track_id)] + (planned or {}).get(track_id, [])
        if span is None or not any(a < span[1] and span[0] < b for a, b in taken):
            break
        lane += 1
    added = {op.get('id') for op in ops if op['op'] == 'add_track'}
    if not project.track(track_id) and track_id not in added:
        ops.append({'op': 'add_track', 'kind': 'audio', 'id': track_id, 'name': track_id.removeprefix('track_')})
    if span is not None and planned is not None:
        planned.setdefault(track_id, []).append(span)
    return track_id


def _audio_ops(project: Project, spec: dict, at: int, ops: list[dict], planned: dict) -> str:
    """An audio item's operations onto the first lane of its role where it fits."""
    probe_ops, clip_id = _clip_ops(project, spec, 'audio', at, 'track_probe')
    insert = next(op for op in probe_ops if op['op'] == 'insert_clip')
    span = (insert['at'], insert['at'] + insert['source_range']['end'] - insert['source_range']['start'])
    track = _audio_track(project, spec.get('role'), ops, span, planned)
    for op in probe_ops:
        if op.get('track') == 'track_probe':
            op['track'] = track
    ops += probe_ops
    return clip_id


def insert_op(project: Project, clip_json: str, before: str | None, after: str | None, track: str | None) -> dict:
    spec = _spec(clip_json)
    ops: list[dict] = []
    neighbour = project.find(before or after)[0]['id'] if (before or after) else None
    if track == 'audio' or (neighbour and project.track(neighbour)['kind'] == 'audio' and neighbour != SOUND):
        place = 'audio'
    elif track not in (None, '0') or (neighbour and neighbour != MAIN):
        place = 'overlay'
    else:
        place = 'main'
    prefix = {'main': 'c', 'overlay': 'o', 'audio': 'a'}[place]
    spec['id'] = spec.get('id') or project.fresh(f'{prefix}{len(project.items(MAIN)) + 1}' if place == 'main' else f'{prefix}1')
    if any(i['id'] == spec['id'] for t in project.timeline['tracks'] for i in t['items']):
        raise RnError(f'id "{spec["id"]}" is taken')
    if place == 'main':
        clips = project.items(MAIN)
        if before:
            at = project.find(before)[1]['range']['start']
        elif after:
            at = project.find(after)[1]['range']['end']
        else:
            at = clips[-1]['range']['end'] if clips else 0
        built, clip_id = _clip_ops(project, spec, 'main', at, None)
        ops += project.ripple_main(built)
    else:
        if 'at' not in spec:
            raise RnError('an overlay or audio item needs "at" (seconds, or an anchor like {"clip": "c2", "offset": 0.5})')
        at = _at(project, spec['at'])
        if place == 'overlay':
            number = int(track) if track and track.isdigit() else (int(neighbour.split('V')[-1]) - 1 if neighbour else 1)
            built, clip_id = _clip_ops(project, spec, place, at, _overlay_track(project, number, ops))
            ops += built
        else:
            clip_id = _audio_ops(project, spec, at, ops, {})
    project.commit(ops, f'insert {clip_id}')
    _remember(project, clip_id, spec)
    if place != 'main':
        lead = float(spec.get('pivot', 0)) - float(spec.get('in', 0)) if 'pivot' in spec else 0.0
        project.anchor(clip_id, spec['at'], lead)
    if spec.get('transition') and place == 'main':
        transition_op(project, clip_id, spec['transition'])
    return {'op': 'insert', 'clip': clip_id}


def move_op(project: Project, ref: str, before: str | None, after: str | None, track: str | None) -> dict:
    current, item = project.find(ref)
    if current['id'] == MAIN and (before or after):
        target = project.find(before or after)[1]
        to = target['range']['start'] if before else target['range']['end']  # measured before the clip leaves its place
        if to in (item['range']['start'], item['range']['end']):
            raise RnError(f'{ref} is already there')
        ops = project.ripple_main([{'op': 'move_clip', 'clip_id': ref, 'to_track': MAIN, 'to': to, 'links': 'follow',
                                    'ripple': 'all_unlocked_tracks', 'locked_tracks': 'skip'}])
    elif track is not None and current['kind'] == 'video' and current['id'] != MAIN:
        ops: list[dict] = []
        to_track = _overlay_track(project, int(track), ops)
        ops.append({'op': 'move_clip', 'clip_id': ref, 'to_track': to_track, 'to': item['range']['start'], 'ripple': 'none'})
    else:
        raise RnError('move: a main-track clip with --before/--after ID, or an overlay with --track N')
    project.commit(ops, f'move {ref}')
    return {'op': 'move', 'clip': ref}


def _remember(project: Project, clip_id: str, spec: dict) -> None:
    kept = {k: v for k, v in spec.items() if k in SPEC_FIELDS | {'src', 'image', 'frames', 'fps'}}
    project.state['specs'][clip_id] = kept
    project.save_state()


XFADES = {'wipeleft', 'wiperight', 'wipeup', 'wipedown', 'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen',
          'circleclose', 'circlecrop', 'rectcrop', 'distance', 'fadegrays', 'radial', 'smoothleft', 'smoothright', 'smoothup',
          'smoothdown', 'horzopen', 'horzclose', 'vertopen', 'vertclose', 'diagbl', 'diagbr', 'diagtl', 'diagtr', 'hlslice',
          'hrslice', 'vuslice', 'vdslice', 'dissolve', 'pixelize', 'hblur', 'zoomin', 'squeezeh', 'squeezev', 'coverleft',
          'coverright', 'coverup', 'coverdown', 'revealleft', 'revealright', 'revealup', 'revealdown'}  # ffmpeg xfade, rendered


def _unbake(project: Project, ref: str, record: dict) -> tuple[list[dict], dict, dict]:
    """The operations that take a rendered transition out again (its clip goes, both clips get their frames back), and
    the left and right clips as they will be."""
    left, right = project.find(record['left'])[1], project.find(ref)[1]
    ins, outs = record['in'], record['out']
    ops = [{'op': 'delete', 'objects': [record['clip']], 'links': 'follow', 'ripple': 'none'},
           {'op': 'extend_clip', 'clip_id': left['id'], 'edge': 'tail', 'to': left['range']['end'] + ins, 'links': 'follow', 'ripple': 'none'},
           {'op': 'extend_clip', 'clip_id': ref, 'edge': 'head', 'to': right['range']['start'] - outs, 'links': 'follow', 'ripple': 'none'}]
    left = {**left, 'range': {**left['range'], 'end': left['range']['end'] + ins}}
    right = {**right, 'range': {**right['range'], 'start': right['range']['start'] - outs},
             'source_range': {**right['source_range'], 'start': right['source_range']['start'] - int(outs * project.speed(right))}}
    return ops, left, right


def transition_op(project: Project, ref: str, transition) -> dict:
    """A transition into main-track clip `ref`, centred on the cut where both clips have the source for it. fade /
    dissolve / fadeblack / fadewhite are MMK's own; other ffmpeg xfade types are rendered by rn assemble into a clip
    over the cut (the app plays it as footage)."""
    xfades = project.state.setdefault('xfades', {})
    ops: list[dict] = []
    if ref in xfades:
        ops, left, right = _unbake(project, ref, xfades[ref])
    else:
        clips = project.items(MAIN)
        index = _main_index(project, ref)
        if index == 0:
            raise RnError(f'{ref} is the first clip: a transition goes into a clip that follows another')
        left, right = clips[index - 1], clips[index]
        if left['range']['end'] != right['range']['start']:
            raise RnError(f'{ref}: there is a gap before it, so no cut to put a transition on')
    old = [t['id'] for t in project.items(MAIN, 'transition') if t.get('right') == ref]
    sound_left, sound_right = project.sound_of(left['id']), project.sound_of(ref)
    old += [t['id'] for t in project.items(SOUND, 'transition') if sound_right and t.get('right') == sound_right]
    ops += [{'op': 'delete', 'objects': old}] if old else []
    if transition in (None, 'none', 'cut') or (isinstance(transition, dict) and transition.get('type') in (None, 'none', 'cut')):
        if not ops:
            raise RnError(f'{ref} has no transition')
        project.commit(ops, f'set {ref} transition=none')
        xfades.pop(ref, None)
        project.save_state()
        return {'op': 'set', 'clip': ref}
    kind = transition.get('type', 'fade') if isinstance(transition, dict) else str(transition)
    seconds = float(transition.get('duration', 0.5)) if isinstance(transition, dict) else 0.5
    if kind not in TRANSITIONS and kind not in XFADES:
        raise RnError(f'transition {kind}: MMK has {", ".join(TRANSITIONS)}; rn renders the ffmpeg xfade types '
                      f'({", ".join(sorted(XFADES)[:8])}, ...)')
    total = project.at_frame(seconds)
    # Before the cut the right clip shows frames from before its in point, after it the left clip from past its out point
    right_handle = int(right['source_range']['start'] / project.speed(right))
    left_end = project.asset_seconds(left['asset'])
    left_handle = 10 ** 12 if left_end is None else int((left_end * TICKS - project.source_seconds(left)[1] * TICKS) / project.speed(left))
    right_handle = 10 ** 12 if project.asset_seconds(right['asset']) is None else right_handle
    frame = project.frame
    out_offset = min(total // 2 // frame * frame, max(0, left_handle) // frame * frame)
    in_offset = min(total - out_offset, max(0, right_handle) // frame * frame)
    out_offset = min(total - in_offset, max(0, left_handle) // frame * frame)
    if in_offset + out_offset < total:
        spare = project.seconds(in_offset + out_offset)
        raise RnError(f'{kind} {seconds}s into {ref}: only {spare}s of source to spare around the cut '
                      f'({left["id"]} has {project.seconds(max(0, left_handle))}s after its out, {ref} '
                      f'{project.seconds(max(0, right_handle))}s before its in); use a {spare}s transition or trim '
                      f'{left["id"]}\'s out / {ref}\'s in to make room')
    if kind in XFADES:
        return _render_transition(project, ref, left, right, kind, in_offset, out_offset, ops)
    definition, parameters = TRANSITIONS[kind]
    for left_id, right_id, suffix in ((left['id'], ref, ''), (sound_left, sound_right, '_audio')):
        if left_id and right_id:
            ops.append({'op': 'add_transition', 'id': project.fresh(f't_{ref}{suffix}'), 'left': left_id, 'right': right_id,
                        'definition': definition if not suffix else 'mmk.transition.cross_dissolve',
                        'parameters': parameters if not suffix else {}, 'in_offset': in_offset, 'out_offset': out_offset})
    project.commit(ops, f'set {ref} transition={kind}/{seconds}')
    xfades.pop(ref, None)
    project.save_state()
    return {'op': 'set', 'clip': ref}


def _side(project: Project, item: dict, first: float, last: float) -> dict:
    """The rn clip that plays `item`'s source from `first` to `last` seconds of the timeline around the cut."""
    asset = project.doc['assets'][item['asset']]
    path = str(project.path / asset['uri'].split('#')[0])
    spec = {k: v for k, v in project.state['specs'].get(item['id'], {}).items() if k in ('fit', 'audio', 'gain_db')}
    if asset.get('kind') == 'image':
        return {**spec, 'image': path, 'duration': round(last - first, 6)}
    speed = float(project.speed(item))
    origin = item['range']['start'] / TICKS
    start = item['source_range']['start'] / TICKS
    return {**spec, 'src': path, 'in': round(start + (first - origin) * speed, 6), 'out': round(start + (last - origin) * speed, 6),
            **({'speed': speed} if speed != 1 else {})}


def _render_transition(project: Project, ref: str, left: dict, right: dict, kind: str, in_offset: int, out_offset: int,
                       ops: list[dict]) -> dict:
    """An ffmpeg xfade rendered over the cut: both clips give up the transition's frames, and a clip of the two mixed
    (rn assemble, frame-exact, sound crossfaded) plays there."""
    cut, pad = right['range']['start'], project.frame  # a frame of each clip on its own either side: rn's xfade wants it
    if left['range']['end'] - left['range']['start'] <= in_offset + pad or right['range']['end'] - right['range']['start'] <= out_offset + pad:
        raise RnError(f'{kind}: {left["id"]} or {ref} is shorter than its half of the transition; use a shorter one')
    first, last = (cut - in_offset) / TICKS, (cut + out_offset) / TICKS
    a, b = _side(project, left, first - pad / TICKS, last), _side(project, right, first, last + pad / TICKS)
    b['transition'] = {'type': kind, 'duration': round(last - first, 6)}
    clip_id = project.fresh(f'x_{ref}')
    baked = _bake(project, {'id': clip_id, 'ranges_of': [a, b]})
    in_offset, out_offset = in_offset + pad, out_offset + pad
    ops += [{'op': 'trim_clip', 'clip_id': left['id'], 'edge': 'tail', 'to': cut - in_offset, 'links': 'follow', 'ripple': 'none'},
            {'op': 'trim_clip', 'clip_id': ref, 'edge': 'head', 'to': cut + out_offset, 'links': 'follow', 'ripple': 'none'}]
    built, _ = _clip_ops(project, baked, 'main', cut - in_offset, None)
    for op in built:
        if op['op'] == 'insert_clip':
            op['ripple'] = 'none'
            op.pop('locked_tracks', None)
    project.commit(ops + built, f'set {ref} transition={kind}/{project.seconds(in_offset + out_offset - 2 * pad)}')
    project.state['xfades'][ref] = {'clip': clip_id, 'left': left['id'], 'in': in_offset, 'out': out_offset, 'type': kind}
    project.state.get('baked', {}).pop(clip_id, None)
    project.notes = [n for n in project.notes if not n.startswith(f'{clip_id}: ')]  # the clip's own note: not for the agent
    project.notes.append(f'{kind}: rendered over the cut as {clip_id} (rn/baked/); set {ref} transition=... changes it, '
                         f'transition=none puts the cut back')
    project.save_state()
    return {'op': 'set', 'clip': ref}


def _trim(project: Project, item: dict, edge: str, to: int, main: bool) -> list[dict]:
    current = item['range']['start' if edge == 'head' else 'end']
    if to == current:
        return []
    shorter = to > current if edge == 'head' else to < current
    # The main track ripples; each later clip's sound follows its picture (linked)
    return [{'op': 'trim_clip' if shorter else 'extend_clip', 'clip_id': item['id'], 'edge': edge, 'to': to, 'links': 'follow',
             'ripple': 'track' if main else 'none'}]


def set_op(project: Project, ref: str, assignments: list[str], unset: list[str]) -> dict:
    from .editops import _assign
    track, item = project.find(ref)
    main = track['id'] == MAIN
    changes: dict = {}
    for text in assignments:
        key, sep, value = text.partition('=')
        if not sep:
            raise RnError(f'{text}: expected key=value')
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        _assign(changes, key, parsed)
    fields = set(changes) | {name.split('.')[0] for name in unset}
    unsupported = sorted(fields & NOT_YET)
    unknown = sorted(fields - SPEC_FIELDS - TIMING_FIELDS - NOT_YET - BAKED - {'id'})
    if unsupported or unknown:
        raise RnError(f'set {", ".join(unsupported + unknown)}: '
                      + ('not on an MMK project yet' if unsupported else 'not a field rn knows here'))
    if 'id' in fields:
        raise RnError('an MMK object keeps its id')
    label = f'set {ref} ' + ' '.join(assignments + [f'--unset {u}' for u in unset])
    transitioned = 'transition' in fields
    if transitioned:
        transition_op(project, ref, changes.get('transition'))
        fields.discard('transition')
    ops: list[dict] = []
    item = project.find(ref)[1]
    source_start, source_end = project.source_seconds(item)
    speed = float(project.speed(item))
    if fields & BAKED or (ref in project.state.get('baked', {}) and fields & {'src', 'image', 'frames', 'in', 'out', 'duration', 'speed'}):
        return _rebake(project, ref, changes, unset, label)  # timing of a rendered clip is its source's, so render again
    if fields & {'src', 'image', 'frames'}:
        return _replace(project, ref, changes, label)
    if 'speed' in fields:
        new = Fraction(changes.get('speed', 1)).limit_denominator(1000)
        if new <= 0:
            raise RnError('speed must be above 0')
        length = project.at_frame((source_end - source_start) / float(new))
        ops.append({'op': 'set_linear_time_warp', 'clip_id': ref, 'scalar': {'num': new.numerator, 'den': new.denominator}})
        sound = project.sound_of(ref)
        if sound:
            ops.append({'op': 'set_linear_time_warp', 'clip_id': sound, 'scalar': {'num': new.numerator, 'den': new.denominator}})
            ops.append({'op': 'set_clip_audio_speed', 'clip_id': sound, 'policy': 'preserve_pitch' if new != 1 else 'resample'})
        ops += _trim(project, item, 'tail', item['range']['start'] + length, main)
        item = {**item, 'range': {**item['range'], 'end': item['range']['start'] + length}}
        speed = float(new)
    if fields & {'in', 'out', 'duration'}:
        new_in = float(changes.get('in', source_start))
        start, end = item['range']['start'], item['range']['end']
        if 'in' in changes:  # the head moves in the source; the start on the timeline stays, the out point too
            delta = project.at_frame((new_in - source_start) / speed)
            ops += _trim(project, item, 'head', start + delta, main)  # main: MMK's ripple trim keeps the start
            if not main and delta:  # elsewhere the trim moves the start: put it back
                ops.append({'op': 'move_clip', 'clip_id': ref, 'to_track': track['id'], 'to': start, 'ripple': 'none'})
            end -= delta
        if 'duration' in changes:
            length = project.at_frame(float(changes['duration']))
        elif 'out' in changes:
            length = project.at_frame((float(changes['out']) - new_in) / speed)
        else:
            length = end - start
        if length <= 0:
            raise RnError(f'set {ref}: the clip would last {project.seconds(length)}s')
        ops += _trim(project, {**item, 'range': {'start': start, 'end': end}}, 'tail', start + length, main)
    if fields & SPEC_FIELDS:
        spec = dict(project.state['specs'].get(ref, {}))
        for key, value in changes.items():
            if key in SPEC_FIELDS:
                spec[key] = value
        for name in unset:
            parts = name.split('.')
            target = spec
            for part in parts[:-1]:
                target = target.get(part, {}) if isinstance(target, dict) else {}
            if isinstance(target, dict):
                target.pop(parts[-1], None)
        ops += _properties(project, ref, item, spec, fields & SPEC_FIELDS)
        project.state['specs'][ref] = spec
    if not ops:
        if transitioned or fields & {'meta', 'note', 'role'}:
            project.save_state()
            return {'op': 'set', 'clip': ref}
        raise RnError(f'set {ref}: nothing changes')
    project.commit(ops, label)
    return {'op': 'set', 'clip': ref}


def _properties(project: Project, ref: str, item: dict, spec: dict, fields: set[str]) -> list[dict]:
    """The MMK properties `fields` decide, derived by rn's own converter from the clip's rn fields (`spec`) and where the
    clip sits now; only those properties are written."""
    paths = set().union(*(PATHS.get(f, set()) for f in fields))
    track = project.find(ref)[0]
    asset = project.doc['assets'][item['asset']]
    source = project.path / asset['uri'].split('#')[0]
    start, end = project.source_seconds(item)
    timing = {'src': str(source), 'in': start, 'duration': project.seconds(item['range']['end'] - item['range']['start'])}
    if asset.get('kind') == 'image':
        timing = {'image': str(source), 'duration': timing['duration']}
    timing['speed'] = float(project.speed(item))
    body = {**{k: v for k, v in spec.items() if k in SPEC_FIELDS - {'meta', 'note', 'role'}}, **timing, 'id': ref}
    if track['id'] == MAIN:
        built, _ = _clip_ops(project, body, 'main', 0, None)
    elif track['kind'] == 'video':
        built, _ = _clip_ops(project, body, 'overlay', 0, track['id'])
    else:
        body.pop('speed', None)
        built, _ = _clip_ops(project, {**body, **({'role': spec['role']} if spec.get('role') else {})}, 'audio', 0, track['id'])
    sound = project.sound_of(ref)
    targets = {ref: ref, f'{ref}_audio': sound}
    out = []
    for op in built:
        target = (op.get('target') or {}).get('object')
        if op['op'] in ('set_property', 'set_keyframes') and target in targets and targets[target] and op['target']['path'] in paths:
            out.append({**op, 'target': {**op['target'], 'object': targets[target]}})
        elif op['op'] == 'set_clip_audio_speed' and op.get('policy') == 'mute' and 'audio' in fields:
            out.append({**op, 'clip_id': ref})
    if 'audio' in fields and sound:
        out.append({'op': 'set_enabled', 'object': sound, 'enabled': spec.get('audio', True) is not False})
    # A property rn no longer sets goes back to MMK's default
    defaults = {'/transform/rotation': 0.0, '/opacity': 1.0, '/volume': 0.0}
    written = {(op['target']['object'], op['target']['path']) for op in out if 'target' in op}
    for path in paths & set(defaults):
        for obj in ((ref,) if path != '/volume' else ((sound,) if sound else (ref,))):
            if (obj, path) not in written and obj:
                out.append({'op': 'set_property', 'target': {'object': obj, 'path': path}, 'value': defaults[path]})
    return out


def _rebake(project: Project, ref: str, changes: dict, unset: list[str], label: str) -> dict:
    """vf / af on a main-track clip: the rn clip (its source where it is now, or the one it was rendered from) with the
    change, rendered again; without vf and af any more it plays its own source again."""
    track, item = project.find(ref)
    if track['id'] != MAIN:
        raise RnError('vf / af: on a main-track clip (an overlay or audio item: render it first and insert the file)')
    recipe = dict(project.state.get('baked', {}).get(ref) or {})
    if not recipe:
        asset = project.doc['assets'][item['asset']]
        start, _ = project.source_seconds(item)
        recipe = {**project.state['specs'].get(ref, {}), 'src': str(project.path / asset['uri'].split('#')[0]), 'in': start,
                  'speed': float(project.speed(item))}
    length = project.seconds(item['range']['end'] - item['range']['start'])
    if 'duration' in changes:
        length = float(changes['duration'])
    elif 'out' in changes:
        length = (float(changes['out']) - float(changes.get('in', recipe.get('in', 0)))) / float(changes.get('speed', recipe.get('speed', 1)))
    elif 'speed' in changes or 'in' in changes:  # the same out point in the source
        source_end = float(recipe.get('in', 0)) + length * float(recipe.get('speed', 1))
        length = (source_end - float(changes.get('in', recipe.get('in', 0)))) / float(changes.get('speed', recipe.get('speed', 1)))
    recipe.update({k: v for k, v in changes.items() if k not in ('out', 'duration')})
    for name in unset:
        recipe.pop(name, None)
    recipe.pop('out', None)
    recipe['duration'] = round(length, 6)
    project.state.get('baked', {}).pop(ref, None)
    return _replace(project, ref, {}, label, spec=recipe)


def _replace(project: Project, ref: str, changes: dict, label: str, spec: dict | None = None) -> dict:
    """A new source for a clip, or the clip made again from `spec` (a whole rn clip): at the same place, as long as
    before unless the spec says otherwise (the main track ripples), its transitions kept."""
    track, item = project.find(ref)
    if spec is None:
        spec = dict(project.state['specs'].get(ref, {}))
        for key in ('src', 'image', 'frames', 'fps'):
            spec.pop(key, None)
        spec.update({k: v for k, v in changes.items() if k in SPEC_FIELDS | {'src', 'image', 'frames', 'fps', 'in', 'speed'}})
    spec.setdefault('duration', project.seconds(item['range']['end'] - item['range']['start']))
    spec['id'] = ref
    place = 'main' if track['id'] == MAIN else ('overlay' if track['kind'] == 'video' else 'audio')
    sound = project.sound_of(ref)
    joins = [t for tid in (MAIN, SOUND) for t in project.items(tid, 'transition') if {t.get('left'), t.get('right')} & {ref, sound}]
    kept = json.loads((project.path / 'project.json').read_text(encoding='utf-8')).get('extensions', {}).get('transitions', {})
    if place == 'main':
        ops = [{'op': 'ripple_delete', 'objects': [ref], 'links': 'follow', 'ripple': 'track'}]
        built, _ = _clip_ops(project, spec, place, item['range']['start'], None)
        ops += project.ripple_main(built)
    else:
        ops = [{'op': 'delete', 'objects': [ref], 'links': 'follow', 'ripple': 'none'}]
        built, _ = _clip_ops(project, spec, place, item['range']['start'], track['id'])
        ops += built
    rename = {ref: ref, sound: f'{ref}_audio'} if sound else {ref: ref}
    for t in joins:
        ops.append({'op': 'add_transition', 'id': t['id'], 'left': rename.get(t['left'], t['left']), 'right': rename.get(t['right'], t['right']),
                    'definition': t['definition'], 'parameters': (kept.get(t['id']) or {}).get('parameters', {}),
                    'in_offset': t['in_offset'], 'out_offset': t['out_offset']})
    project.commit(ops, label)
    _remember(project, ref, spec)
    return {'op': 'set', 'clip': ref}


# -- intent-level operations --
def _listen_data(listen: str, flag: str = '--listen ') -> dict:
    path = Path(listen).expanduser()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise RnError(f'{flag}{listen}: no such file') from None
    except json.JSONDecodeError as error:
        raise RnError(f'{flag}{listen}: not JSON ({error}); write it with rn listen --json') from None
    if not isinstance(data, dict):
        raise RnError(f'{flag}{listen}: not an rn listen --json result')
    return data


def _beats(data: dict, listen: str, to: str = 'beats') -> list[float]:
    beats = data.get('beats') or {}
    times = beats.get('downbeats' if to == 'downbeats' else 'times') if isinstance(beats, dict) else None
    if not times:
        raise RnError(f'--listen {listen}: no beats.{"downbeats" if to == "downbeats" else "times"} '
                      f'(rn listen MUSIC --json {Path(listen).name} writes them)')
    return [float(t) for t in times]


def hits_op(project: Project, sfx: str, at: str, *, listen: str | None = None, offset: float = 0.0, pivot: float | None = None,
            gain: float | None = None, every: int = 1, prefix: str = 'sfx') -> dict:
    """One sound at many points, one audio item (role sfx) each: every cut, every (Nth) beat, or given seconds. Cuts
    and beats are anchors, so the items follow later edits."""
    if every < 1:
        raise RnError('--every: 1 or more')
    pivot_from = 'given' if pivot is not None else None
    source = Path(sfx).expanduser()
    if not source.is_file():
        from . import pack
        if not pack.is_pack_id(sfx):
            raise RnError(f'{sfx}: no such file (relative to the current folder) and not an rn pack id')
        item = pack.find(sfx)
        if item['kind'] != 'sfx':
            raise RnError(f'{sfx}: a {item["kind"]}, not a sound (rn pack list --kind sfx)')
        source = Path(pack.path_of(item['id']))
        if pivot is None:
            pivot, pivot_from = item['peak'], 'pack peak'
    if (at == 'beats') != (listen is not None):
        raise RnError('--at beats goes with --listen LISTEN.json (a rn listen --json result)')
    end = project.timeline['duration'] / TICKS
    if at == 'cuts':
        clips = project.items(MAIN)
        points = [(c['id'], {'clip': c['id']}, c['range']['start'] / TICKS) for c in clips[1:]]
        if not points:
            raise RnError('--at cuts: the main track has one clip, so no cuts')
    elif at == 'beats':
        times = _beats(_listen_data(listen), listen)
        points = [(f'beat {n}', {'beat': n, 'listen': listen}, t) for n, t in enumerate(times)]
    else:
        try:
            given = [float(part) for part in at.split(',') if part.strip()]
        except ValueError:
            raise RnError(f'--at {at}: cuts, beats, or seconds like 1.2,3.5') from None
        points = [(f'{t:g}s', None, t) for t in given]
    ops: list[dict] = []
    planned: dict = {}
    added, hits, skipped = [], [], []
    for label, anchor, seconds in points[::every]:
        point = seconds + offset
        if not 0 <= point < end:
            skipped.append({'hit': label, 'time': round(point, 4), 'reason': 'before 0 s' if point < 0 else f'past the end ({end:g}s)'})
            continue
        ident = project.fresh(f'{prefix}{len(added) + 1}')
        while ident in added:
            ident = project.fresh(f'{prefix}{len(added) + 2}')
        place = ({**anchor, 'offset': offset} if offset else anchor) if anchor else round(point, 6)
        spec = {'id': ident, 'src': str(source), 'role': 'sfx', 'at': place, **({'pivot': pivot} if pivot is not None else {}),
                **({'gain_db': gain} if gain is not None else {})}
        _audio_ops(project, spec, project.at_frame(point), ops, planned)
        added.append((ident, spec))
        hits.append({'id': ident, 'hit': label, 'time': round(point, 4)})
    if not added:
        raise RnError('no hit lands inside the timeline: ' + ', '.join(f'{s["hit"]} at {s["time"]}s' for s in skipped[:8]))
    project.commit(ops, f'hits {Path(sfx).name} --at {at}')
    for ident, spec in added:
        project.anchor(ident, spec['at'], float(pivot or 0))
    return {'op': 'hits', 'src': str(source), 'at': at if at in ('cuts', 'beats') else 'times', 'added': [i for i, _ in added],
            'pivot': pivot, 'pivot_from': pivot_from, 'hits': hits, 'skipped': skipped}


def silences_op(project: Project, ref: str, listen: str, *, min_silence: float = 0.5, pad: float = 0.1, keep_gap: float = 0.0) -> dict:
    """A main-track clip's long pauses cut out (rn listen of its source): the clip becomes the pieces that stay (ref,
    ref-2, ...), back to back, and what follows ripples."""
    from .editops import kept_spans
    if min_silence <= 0 or pad < 0 or keep_gap < 0:
        raise RnError('--min must be > 0; --pad and --keep-gap >= 0')
    track, item = project.find(ref)
    if track['id'] != MAIN:
        raise RnError(f'{ref}: silences are cut on the main track')
    data = _listen_data(listen)
    if not isinstance(data.get('silences'), list):
        raise RnError(f'--listen {listen}: no "silences" (rn listen SOURCE --speech --json writes them)')
    span = project.source_seconds(item)
    kept = kept_spans([span], data['silences'], project.fps, min_silence=min_silence, pad=pad, keep_gap=keep_gap)
    if not kept:
        raise RnError(f'{ref}: every second of the clip is in a silence of {Path(listen).name}')
    speed = float(project.speed(item))
    start = item['range']['start']

    def place(seconds: float) -> int:  # a source second as a timeline tick, on the clip as it is
        return start + project.at_frame((seconds - span[0]) / speed)
    ops: list[dict] = []
    names = [ref] + [project.fresh(f'{ref}-{k + 1}') for k in range(1, len(kept))]
    if place(kept[-1][1]) < item['range']['end']:
        ops.append({'op': 'trim_clip', 'clip_id': ref, 'edge': 'tail', 'to': place(kept[-1][1]), 'links': 'follow', 'ripple': 'track'})
    for k in range(len(kept) - 1, 0, -1):  # from the end, so the places before are still where they were
        gap_start, gap_end = place(kept[k - 1][1]), place(kept[k][0])
        ops.append({'op': 'split_clip', 'clip_id': ref, 'at': gap_end, 'id': names[k], 'links': 'follow'})
        if gap_end > gap_start:
            gap = project.fresh(f'{ref}-gap{k}')
            ops.append({'op': 'split_clip', 'clip_id': ref, 'at': gap_start, 'id': gap, 'links': 'follow'})
            ops.append({'op': 'ripple_delete', 'objects': [gap], 'links': 'follow', 'ripple': 'track'})
    if place(kept[0][0]) > start:
        ops.append({'op': 'trim_clip', 'clip_id': ref, 'edge': 'head', 'to': place(kept[0][0]), 'links': 'follow', 'ripple': 'track'})
    if not ops:
        raise RnError(f'{ref}: no silence of {min_silence}s or more inside the clip')
    before = (item['range']['end'] - start) / TICKS
    project.commit(ops, f'silences {ref}')
    after = sum((i['range']['end'] - i['range']['start']) for i in project.items(MAIN) if i['id'] in names) / TICKS
    for name in names[1:]:
        if ref in project.state['specs']:
            project.state['specs'][name] = dict(project.state['specs'][ref])
    project.save_state()
    return {'op': 'silences', 'clip': ref, 'pieces': names, 'spans': len(kept), 'removed': round(before - after, 3),
            'kept': round(after, 3)}


def snap_op(project: Project, listen: str, *, to: str = 'beats', tolerance: float = 0.2, only: str | None = None,
            offset: float = 0.0) -> dict:
    """Main-track cuts moved onto the nearest beat by rolling edits (nothing else moves). offset: where the analysed
    music starts on the timeline."""
    if to not in ('beats', 'downbeats'):
        raise RnError('--to beats or downbeats')
    beats = [b + offset for b in _beats(_listen_data(listen), listen, to)]
    wanted = set(only.split(',')) if only else None
    clips = project.items(MAIN)
    transitions = {t['right']: t for t in project.items(MAIN, 'transition')}
    ops, snapped, kept = [], [], []
    for left, right in zip(clips, clips[1:]):
        if wanted is not None and right['id'] not in wanted:
            continue
        cut = right['range']['start']
        if left['range']['end'] != cut:
            kept.append({'cut': right['id'], 'time': project.seconds(cut), 'reason': 'a gap before it, not a cut'})
            continue
        t = transitions.get(right['id'])
        middle = cut + ((t['out_offset'] - t['in_offset']) // 2 if t else 0)
        beat = min(beats, key=lambda b: abs(b - middle / TICKS), default=None)
        if beat is None or abs(beat - middle / TICKS) > tolerance:
            kept.append({'cut': right['id'], 'time': project.seconds(cut), 'reason': f'no {to[:-1]} within {tolerance}s'})
            continue
        target = project.at_frame(beat) - (middle - cut)
        shift = target - cut
        if shift == 0:
            continue
        reason = None
        if shift > 0:
            spare = project.asset_seconds(left['asset'])
            if spare is not None and (spare - project.source_seconds(left)[1]) * TICKS < shift * project.speed(left) + (t['out_offset'] if t else 0):
                reason = f'{left["id"]} has no source past its out'
            elif right['range']['end'] - target < project.frame:
                reason = f'{right["id"]} would vanish'
        else:
            if project.asset_seconds(right['asset']) is not None and right['source_range']['start'] < -shift * project.speed(right) + (t['in_offset'] if t else 0):
                reason = f'{right["id"]} has no source before its in'
            elif target - left['range']['start'] < project.frame:
                reason = f'{left["id"]} would vanish'
        if reason:
            kept.append({'cut': right['id'], 'time': project.seconds(cut), 'reason': reason})
            continue
        ops.append({'op': 'roll', 'left': left['id'], 'right': right['id'], 'cut_to': target, 'links': 'follow'})
        snapped.append({'cut': right['id'], 'time': project.seconds(cut), 'beat': round(beat, 4), 'frames': shift // project.frame})
    if not ops:
        raise RnError('no cut moved: ' + '; '.join(f'{k["cut"]}: {k["reason"]}' for k in kept[:8]) if kept else 'no cut moved')
    project.commit(ops, f'snap --to {to}')
    return {'op': 'snap', 'to': to, 'snapped': snapped, 'kept': kept}


def marks_op(project: Project, on: str, listen: str, *, kind: str = 'beats', every: int = 1) -> dict:
    """rn listen's beats, downbeats or words as MMK markers on the clip that plays that sound, so they move with it,
    show on the app's timeline, and anchors can use them ({"marker": "beat 12"})."""
    track, item = project.find(on)
    data = _listen_data(listen)
    if kind == 'words':
        words = data.get('words') or []
        if not words:
            raise RnError(f'--listen {listen}: no words (rn listen --transcribe --json writes them)')
        points = [(f'word {n}: {w.get("word", "").strip()}'[:60], float(w['start'])) for n, w in enumerate(words)]
    else:
        points = [(f'{kind[:-1]} {n}', t) for n, t in enumerate(_beats(data, listen, kind))]
    start, end = project.source_seconds(item)
    ops = []
    for name, seconds in points[::every]:
        if not start <= seconds <= end:
            continue
        tick = round(seconds * TICKS)
        ops.append({'op': 'add_marker', 'target': on, 'range': {'start': tick, 'end': tick}, 'name': name,
                    'color': 'YELLOW' if kind != 'words' else 'CYAN',
                    'id': f'mk_{on}_{kind[:-1]}_{name.split(":")[0].split()[-1]}'})
    if not ops:
        raise RnError(f'{on} plays {start:g}-{end:g}s of its source; no {kind[:-1]} of {Path(listen).name} falls inside')
    project.commit(ops, f'marks {on} {kind}')
    return {'op': 'marks', 'clip': on, 'kind': kind, 'added': len(ops)}


def init_project(path: Path, from_shots: str, *, size: str | None = None, fps: str | None = None, src: str | None = None,
                 slots: str = 'gen', force: bool = False) -> dict:
    """A new project whose main track is a reference's shots (rn edit init, made as an MMK project): slots not made yet
    are placeholder cards, filled with set <id> src=...; dissolves are cross-dissolves, fades through black or white
    dips to that colour."""
    from .editops import init_op
    from .mmk import export_mmk
    path = Path(path).resolve()
    if is_project(path) and Project(path).timeline['duration'] and not force:
        raise RnError(f'{path} already has a cut; --force replaces it')
    draft = Path.cwd() / f'.rn-init-{os.getpid()}.json'
    try:
        made = init_op(draft, from_shots, size=size, fps=fps, src=src, slots=slots, force=True)
        raw = json.loads(draft.read_text(encoding='utf-8'))
        fades = {}  # fades through a colour: a dip transition into the clip that fades in
        clips = raw['tracks'][0]['clips']
        chains = [clip.pop('vf', '') for clip in clips]
        for k, clip in enumerate(clips):
            fade_in = re.search(r'fade=t=in:st=0:d=([\d.]+):color=(\w+)', chains[k])
            if fade_in and k:
                out = re.search(r'fade=t=out:st=[\d.]+:d=([\d.]+)', chains[k - 1])
                fades[clip['id']] = (fade_in.group(2), float(fade_in.group(1)) + (float(out.group(1)) if out else 0))
        draft.write_text(json.dumps(raw), encoding='utf-8')
        # Only MMK's own files are replaced; media already in assets/ (a Renoise workspace's materials) is kept
        keep = path.with_name(f'.{path.name}.rn-keep')
        if keep.exists():
            raise RnError(f'{keep} holds media an earlier rn edit init kept aside: move what is missing back into {path / "assets"}, then remove it')
        if (path / 'assets').is_dir():
            shutil.copytree(path / 'assets', keep, copy_function=_link_or_copy)
        exported = export_mmk(str(draft), str(path), placeholders=True, source=False)
        if keep.exists():
            for item in keep.iterdir():
                if not (path / 'assets' / item.name).exists():
                    (path / 'assets').mkdir(exist_ok=True)
                    item.rename(path / 'assets' / item.name)
            shutil.rmtree(keep)  # only once every file is back: until then it may hold their last copy
    finally:
        draft.unlink(missing_ok=True)
        keep = path.with_name(f'.{path.name}.rn-keep')
        if keep.exists() and all((path / 'assets' / item.name).exists() for item in keep.iterdir()):
            shutil.rmtree(keep)  # the export stopped before it replaced assets/: everything is still there
    project = Project(path)
    project.state['specs'] = {c['id']: {'meta': c['meta']} for c in raw['tracks'][0]['clips'] if c.get('meta')}
    project.save_state()
    for clip_id, (color, seconds) in fades.items():
        try:
            transition_op(project, clip_id, {'type': 'fadewhite' if color == 'white' else 'fadeblack', 'duration': seconds})
        except RnError:
            project.notes.append(f'{clip_id}: the fade through {color} is made as a cut (no room)')
    dropped = [f'{d["item"]}: {", ".join(d["dropped"])}' for d in exported['dropped']]
    return {'op': 'init', 'path': str(path), 'clips': made['clips'], 'source': made['source'],
            'transitions': made['transitions'], 'dropped': made['dropped'] + dropped}


def duck_op(project: Project, *, voice: str | None = None, music: str | None = None, depth: float = 10.0) -> dict:
    """The music lowered wherever the voice has sound (MMK's duck helper), on top of each music item's own level and
    fades. Voice: the given ids, else every clip on the voice tracks, else the main track's own sound. Run it again
    after the voice or the music changes."""
    def on(prefix: str) -> list[str]:
        return [i['id'] for t in project.timeline['tracks'] if t['id'] == prefix or t['id'].startswith(prefix + '_')
                for i in t['items'] if i['kind'] == 'clip']
    voices = voice.split(',') if voice else (on('track_voice') or [i['id'] for i in project.items(SOUND)])
    # Music: the music tracks; a project rn export made has its audio items on track_A2..: those, then
    musics = music.split(',') if music else (on('track_music') or [
        i['id'] for t in project.timeline['tracks'] if t['kind'] == 'audio' and re.fullmatch(r'track_A([2-9]|\d\d+)', t['id'])
        for i in t['items'] if i['kind'] == 'clip' and i['id'] not in voices])
    if not voices or not musics:
        raise RnError('duck: no ' + ('voice' if not voices else 'music') + ' clips (give --voice / --music ids, or insert '
                      'audio items with role voice / music)')
    for clip_id in voices + musics:
        project.find(clip_id)
    # Measured with the music at 0 dB (on a copy), so running it again does not duck twice; each item's own level and
    # fades are added back below
    work = _scratch(project)
    try:
        flat = Project(work)
        flat._send([{'op': 'set_property', 'target': {'object': m, 'path': '/volume'}, 'value': 0.0} for m in musics], 'flat')
        lowered = _mmk('duck', str(work), '--voice', ','.join(voices), '--music', ','.join(musics), '--depth', f'{depth:g}', '--dry-run')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    placer = _Placer(project, SimpleNamespace(fps=project.fps, width=project.width, height=project.height), 0)
    for clip_id in musics:
        item = project.find(clip_id)[1]
        spec = project.state['specs'].get(clip_id, {})
        placer.volume(clip_id, clip_id, float(spec.get('gain_db', 0)), _keys_of(spec), float(spec.get('fade_in', 0)),
                      float(spec.get('fade_out', 0)), (item['range']['end'] - item['range']['start']) / TICKS, hold=True)
    ops = placer.ducked(lowered)
    project.commit(ops, f'duck --depth {depth:g}')
    return {'op': 'duck', 'voice': voices, 'music': musics, 'depth': depth}


def _keys_of(spec: dict) -> dict | None:
    keys = spec.get('keys') or {}
    if 'gain_db' not in keys:
        return None
    return {'gain_db': [(float(p[0]), float(p[1]), p[2] if len(p) > 2 else 'linear') for p in keys['gain_db']]}


def subtitles_op(project: Project, file: str, *, size: float | None = None, offset: float = 0.0) -> dict:
    """An .srt / .ass file as text clips on a track of their own (MMK's subtitles helper: white with a dark outline,
    near the bottom; ASS styles are not kept). Animated caption styles (rn captions --style pop / karaoke): render them
    with rn captions and insert the rendered overlay instead."""
    path = Path(file).expanduser()
    if not path.is_file():
        raise RnError(f'{file}: no such file')
    args = ['subtitles', str(project.path), str(path.resolve()), '--dry-run']
    if size:
        args += ['--size', f'{size:g}']
    if offset:
        args += ['--offset', str(project.at_frame(offset))]
    made = _mmk(*args)
    project.commit(made['operations'], f'subtitles {path.name}')
    return {'op': 'subtitles', 'file': str(path), 'cues': made.get('cues', 0)}


# -- deliver --
DELIVERY = {'lufs': -14.0, 'true_peak': -1.0}  # rn's delivery standard; MMK applies loudness at export


def assemble_project(path: str, out: str, *, preview: bool = False, time_range: str | None = None) -> dict:
    """rn assemble X.mmkproj: the project rendered by `mmk export` (the same engine the app exports with), loudness to
    rn's standard. --preview: half size; --range A-B: those seconds of the timeline."""
    import subprocess

    from .mmk import mmk_binary
    project = Project(path)
    args = [mmk_binary(), 'export', str(project.path), str(Path(out).resolve()), '--lufs', f'{DELIVERY["lufs"]:g}']
    if preview:
        args += ['--width', str(project.width // 4 * 2), '--height', str(project.height // 4 * 2)]
    if time_range:
        try:
            a, b = (float(x) for x in time_range.split('-', 1))
        except ValueError:
            raise RnError(f'--range {time_range}: seconds like 3-6') from None
        args += ['--range', f'{project.at_frame(a)}:{project.at_frame(b)}']
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run = subprocess.run(args, capture_output=True, text=True)
    start = run.stdout.rfind('\n{')
    try:
        report = json.loads(run.stdout[start + 1 if start >= 0 else 0:])
    except json.JSONDecodeError:
        report = None
    if run.returncode != 0 or not report:
        raise RnError(f'mmk export failed: {(run.stderr or run.stdout).strip()[-400:]}')
    frames = report.get('frames', 0)
    return {'path': str(out), 'preview': preview,
            'output': {'width': project.width // 4 * 2 if preview else project.width, 'height': project.height // 4 * 2 if preview else project.height,
                       'fps': str(project.fps), 'frames': frames, 'duration': round(frames / float(project.fps), 3)},
            'degradations': report.get('degradations', [])}


def as_edit(path: str) -> SimpleNamespace:
    """The project as much of an rn Edit as rn qa reads: size, rate, frames, loudness targets and the main track's
    clips (for cuts_in_speech, and the stills and colours that are meant to hold)."""
    project = Project(path)
    sounds = {project.sound_of(i['id']) for i in project.items(MAIN)}
    base = []
    for n, item in enumerate(project.items(MAIN)):
        asset = project.doc['assets'].get(item.get('asset') or '', {})
        sound = project.sound_of(item['id'])
        enabled = next((i.get('enabled', True) for i in project.items(SOUND) if i['id'] == sound), False)
        start, _ = project.source_seconds(item)
        base.append(SimpleNamespace(
            kind='color' if not item.get('asset') else asset.get('kind', 'video'), id=item['id'], where=f'tracks[0].clips[{n}]',
            source=project.path / asset['uri'].split('#')[0] if asset else None, start_s=start, speed=float(project.speed(item)),
            frames=(item['range']['end'] - item['range']['start']) // project.frame, timeline_start=item['range']['start'] // project.frame,
            audio=bool(sound) and enabled, has_audio=bool((asset.get('streams') or {}).get('audio')),
            transition_frames=1 if any(t.get('right') == item['id'] for t in project.items(MAIN, 'transition')) else 0))
    others = [i for t in project.timeline['tracks'] if t['kind'] == 'audio' and t['id'] != SOUND for i in t['items'] if i['kind'] == 'clip']
    return SimpleNamespace(width=project.width, height=project.height, fps=project.fps, base=base, audio=others,
                           total_frames=project.timeline['duration'] // project.frame, lufs=DELIVERY['lufs'],
                           true_peak=DELIVERY['true_peak'], accept=[], sounds=sounds)


def undo_op(project: Project, steps: int = 1) -> dict:
    undone = []
    for _ in range(steps):
        if not project.state['undo']:
            raise RnError('nothing of rn\'s to undo' + (f' after {len(undone)} step(s)' if undone else ''))
        step = project.state['undo'].pop()
        try:
            for token in reversed(step.get('tokens') or [step['token']]):
                _mmk('agent', str(project.path), '--tool', 'undo', '--args', json.dumps({'undo_token': token}))
        except RnError as error:
            project.state['undo'].append(step)
            raise RnError(f'undo "{step["label"]}": {_error_line(error)}') from None
        undone.append(step['label'])
        for key, value in (step.get('state') or {}).items():  # rn's records as they were before that step
            project.state[key] = value
        project.save_state()
    project.load()
    starts = project._starts()  # anchored items are where the undo put them back: they follow from there
    for item_id in list(project.state['anchors']):
        if item_id in starts:
            project.state['anchors'][item_id]['placed'] = starts[item_id][1]
        else:
            del project.state['anchors'][item_id]
    project.save_state()
    return {'op': 'undo', 'undone': undone}


def show_op(project: Project, *, around: float | None = None, out: str | None = None, width: int = 240) -> dict:
    """The timeline as text and a proof sheet: the middle of every main-track clip (or both sides of the cut at --around)."""
    from PIL import Image

    from .frames import _compose, _save_under
    clips = project.items(MAIN)
    if around is not None:
        tick = project.at_frame(around)
        times = [(f'{around - 0.5:g}s', tick - project.at_frame(0.5)), (f'{around:g}s', tick), (f'{around + 0.5:g}s', tick + project.at_frame(0.5))]
        times = [(label, t) for label, t in times if 0 <= t < project.timeline['duration']]
    else:
        times = [(f'{c["id"]} {project.seconds((c["range"]["start"] + c["range"]["end"]) // 2):g}s',
                  (c['range']['start'] + c['range']['end']) // 2 // project.frame * project.frame) for c in clips]
    proof = None
    if times:
        work = project.path / 'cache' / 'rn-show'
        work.mkdir(parents=True, exist_ok=True)
        images = []
        for n, (_, tick) in enumerate(times):
            png = work / f'{n}.png'
            _render(project, tick, png)
            image = Image.open(png).convert('RGB')
            images.append(image.resize((width, max(2, round(image.height * width / image.width)))))
        sheet = _compose([(0, Path())] * len(images), project.fps, min(4, len(images)),
                         f'{project.path.name} rev {project.revision}', images=images, labels=[label for label, _ in times])
        proof = Path(out) if out else Path.cwd() / 'sheets' / f'show-{project.path.stem}.jpg'
        proof.parent.mkdir(parents=True, exist_ok=True)
        _save_under(sheet, proof, 160)
        shutil.rmtree(work, ignore_errors=True)
    return {'op': 'show', 'proof': str(proof) if proof else None}


def _render(project: Project, tick: int, png: Path) -> None:
    import subprocess

    from .mmk import mmk_binary
    run = subprocess.run([mmk_binary(), 'render', str(project.path), '--time', str(tick), '--out', str(png)], capture_output=True, text=True)
    if run.returncode != 0 or not png.exists():
        raise RnError(f'mmk render at {project.seconds(tick)}s failed: {(run.stderr or run.stdout).strip()[-300:]}')


def batch_op(project: Project, steps: list[str], apply) -> dict:
    """Several operations as one transaction: each runs in turn on a copy of the project (media hard-linked), and their
    operations are then committed to the project together, so its history has one step and nothing lands on a failure.
    Anchored items follow once, after the whole batch, on the project itself."""
    if not steps:
        raise RnError('batch: no operations')
    work = _scratch(project)
    sent: list[dict] = []
    done = []
    try:
        trial = Project(work)
        trial.state['undo'] = []
        trial.anchoring = False
        recorder = _Recorder(trial, sent)
        for number, line in enumerate(steps, 1):
            try:
                result = apply(recorder, line)
            except RnError as error:
                raise RnError(f'step {number} ({line}): {error}; nothing written') from None
            done.append(summary_head(result))
        for folder in ('assets', 'rn/baked'):  # media the steps linked in or rendered
            for item in (work / folder).iterdir() if (work / folder).is_dir() else []:
                target = project.path / folder / item.name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _link_or_copy(item, target) if item.is_file() else shutil.copytree(item, target, copy_function=_link_or_copy)
                    project.linked.append(target)
        project.notes += trial.notes
        project.commit(sent, 'batch: ' + '; '.join(steps), adopt=trial.state)
    finally:
        project.unlink_new()
        shutil.rmtree(work, ignore_errors=True)
    return {'op': 'batch', 'steps': done}


MMK_OWN = ('project.json', 'timeline.otio', 'timeline.otio.prev', 'history', 'scripts', 'generators')


def _scratch(project: Project) -> Path:
    """A working copy of the project beside it: MMK's files and rn's state copied (a step writes them, and must not
    write through to the project), media in assets/ and rn/baked/ hard-linked; nothing else in the folder."""
    work = project.path.with_name(f'.{project.path.name}.rn-batch')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    for name in MMK_OWN:
        source = project.path / name
        if source.is_dir():
            shutil.copytree(source, work / name)
        elif source.exists():
            shutil.copy2(source, work / name)
    if (project.path / STATE).exists():
        (work / 'rn').mkdir()
        shutil.copy2(project.path / STATE, work / STATE)
    for folder in ('assets', 'rn/baked'):
        if (project.path / folder).is_dir():
            shutil.copytree(project.path / folder, work / folder, copy_function=_link_or_copy, dirs_exist_ok=True)
    return work


def _link_or_copy(source, target) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


class _Recorder:
    """A Project whose commits are also kept, for batch."""

    def __init__(self, project: Project, sent: list[dict]):
        self.__dict__['_project'], self.__dict__['_sent'] = project, sent

    def __getattr__(self, name):
        return getattr(self._project, name)

    def __setattr__(self, name, value):
        setattr(self._project, name, value)

    def commit(self, ops: list[dict], label: str) -> dict:
        self._sent.extend(json.loads(json.dumps(ops)))
        return self._project.commit(ops, label)


def summary_head(result: dict) -> str:
    return {'set': lambda r: f'set {r["clip"]}',
            'split': lambda r: f'split {r["clip"]} into {r["into"][0]} + {r["into"][1]}',
            'delete': lambda r: f'deleted {r["clip"]}' + (' (lifted: gap kept)' if r['lift'] else ''),
            'insert': lambda r: f'inserted {r["clip"]}',
            'move': lambda r: f'moved {r["clip"]}',
            'undo': lambda r: 'undid ' + '; '.join(r['undone']),
            'show': lambda r: 'timeline',
            'batch': lambda r: f'{len(r["steps"])} steps as one: ' + '; '.join(r['steps']),
            'hits': lambda r: f'added {len(r["added"])} x {Path(r["src"]).name} at {r["at"]}: '
                              + (r['added'][0] if len(r['added']) == 1 else f'{r["added"][0]}..{r["added"][-1]}')
                              + (f' (pivot {r["pivot"]:g}s, {r["pivot_from"]})' if r.get('pivot') is not None else '')
                              + (f'; not placed: {", ".join(s["hit"] for s in r["skipped"][:8])}' if r['skipped'] else ''),
            'silences': lambda r: f'{r["clip"]}: cut {r["removed"]}s of silence, {r["spans"]} pieces play ({r["kept"]}s): '
                                  + ', '.join(r['pieces']),
            'snap': lambda r: f'snapped {len(r["snapped"])} cut(s) to {r["to"]}: '
                              + ', '.join(f'{c["cut"]} {c["time"]:g}s -> {c["beat"]:g}s' for c in r['snapped'])
                              + ''.join(f'; {c["cut"]} stays ({c["reason"]})' for c in r['kept']),
            'duck': lambda r: f'music {", ".join(r["music"])} lowered {r["depth"]:g} dB under {", ".join(r["voice"][:6])}',
            'subtitles': lambda r: f'{r["cues"]} subtitles from {Path(r["file"]).name} as text clips',
            'marks': lambda r: f'{r["added"]} {r["kind"]} markers on {r["clip"]} (anchor with {{"marker": "{r["kind"][:-1]} N"}})',
            'init': lambda r: f'created {r["path"]}: {len(r["clips"])} clips from the reference\'s shots, sources {r["source"]}'
                              + (f'; made as cuts: {", ".join(r["dropped"])}' if r['dropped'] else '')}[result['op']](result)


def summary_lines(project: Project, result: dict) -> list[str]:
    """What changed, then the timeline as it is now: every clip by track, in seconds."""
    lines = [summary_head(result)]
    duration = project.timeline['duration']
    lines.append(f'{project.path.name}: revision {project.revision}, {project.width}x{project.height} {project.fps}fps, '
                 f'{project.seconds(duration):g}s')
    for track in project.timeline['tracks']:
        entries = []
        for item in sorted(track['items'], key=lambda i: i['range']['start']):
            start, end = project.seconds(item['range']['start']), project.seconds(item['range']['end'])
            if item['kind'] == 'transition':
                kind = {'mmk.transition.cross_dissolve': 'fade', 'mmk.transition.dip_to_color': 'dip'}.get(item['definition'], item['definition'])
                entries.append(f'[{kind} {project.seconds(item["in_offset"] + item["out_offset"]):g}s]')
                continue
            if track['id'] == SOUND:
                continue
            asset = project.doc['assets'].get(item.get('asset'), {})
            name = Path(asset.get('uri', '?').split('#')[0]).name
            speed = project.speed(item)
            extra = (f' x{float(speed):g}' if speed != 1 else '') + ('' if item.get('enabled', True) else ' (off)')
            entries.append(f'{item["id"]} {start:g}-{end:g} {name}{extra}')
        if entries:
            lines.append(f'  {track.get("name") or track["id"]}: ' + '  '.join(entries))
    lines += [f'note: {note}' for note in project.notes]
    if result.get('proof'):
        lines.append(f'proof: {result["proof"]}  (look at it before you go on)')
    if (project.path / 'rn' / 'edit.json').exists():
        lines.append('note: rn/edit.json is what this project was exported from; rn edit changes the project itself, so '
                     'that file no longer follows it (kept, read-only)')
    return lines
