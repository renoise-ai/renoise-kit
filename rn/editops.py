"""rn edit: change an EDL by operations on clip ids instead of rewriting its JSON. Each operation is checked by laying the
whole edit out again (as rn assemble --dry-run would) and only then written back, so a bad step never lands.

`rn edit EDL batch` runs several operations as one and writes the file once, only if every step passes.
`rn edit EDL hits` places one sound effect at many points (cuts, beats, given seconds) as audio items.
`rn edit EDL silences` cuts a clip's long pauses out, as measured by rn listen, by writing its "ranges".

The main track always plays back to back, so removing or trimming a clip there ripples everything after it; overlays and
audio anchored by clip id ({"clip": "id"}) follow, ones placed at fixed seconds do not.
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

from .edl import ID, Clip, load, upgrade
from .ff import RnError, probe


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise RnError(f'no such file: {path}') from None
    except json.JSONDecodeError as error:
        raise RnError(f'{path}: invalid JSON: {error}') from None


def _write_checked(path: Path, raw: dict) -> dict:
    """Lays the edited JSON out next to the original (relative paths resolve the same), then replaces it. A source not
    made yet is a placeholder (as rn assemble --placeholders), listed as a warning: an edit is built before its media."""
    trial = path.with_name(f'.{path.stem}.rn-edit.json')
    trial.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    try:
        summary = load(trial, placeholders=True).summary()
    except RnError:
        trial.unlink()
        raise
    os.replace(trial, path)
    return summary


def _containers(raw: dict) -> list[tuple[str, list]]:
    tracks = raw.get('tracks') or []
    return [*((f'tracks[{t}]', track.get('clips', [])) for t, track in enumerate(tracks)), ('audio', raw.get('audio') or [])]


def _locate(raw: dict, ref: str) -> tuple[str, list, int]:
    """A clip by id, or by position as "track:index" (e.g. 0:3) / "audio:index"."""
    if ':' in ref:
        name, _, index = ref.partition(':')
        lists = dict(_containers(raw))
        key = 'audio' if name == 'audio' else f'tracks[{name}]'
        if key not in lists or not index.isdigit() or int(index) >= len(lists[key]):
            raise RnError(f'{ref}: no such clip (positions are "track:index" or "audio:index")')
        return key, lists[key], int(index)
    for key, items in _containers(raw):
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get('id') == ref:
                return key, items, i
    raise RnError(f'no clip with id "{ref}" (rn edit <edl> ids gives every clip one)')


def _all_ids(raw: dict) -> set[str]:
    return {item.get('id') for _, items in _containers(raw) for item in items if isinstance(item, dict) and item.get('id')}


def _fresh(raw: dict, base: str) -> str:
    taken = _all_ids(raw)
    n = 2
    while f'{base}-{n}' in taken:
        n += 1
    return f'{base}-{n}'


def _parsed(path: Path, key: str, index: int) -> tuple[Clip, Fraction]:
    edit = load(path, placeholders=True)
    where = f'{key}.clips[{index}]'
    clip = next((c for c in edit.base + edit.overlays if c.where == where), None)
    if clip is None:
        raise RnError(f'{where}: only video, image, sequence and colour clips can be split')
    return clip, edit.fps


def ids_op(path: Path) -> dict:
    raw = _read(path)
    taken, added = _all_ids(raw), []
    for key, items in _containers(raw):
        prefix = 'a' if key == 'audio' else ('c' if key == 'tracks[0]' else 'o')
        n = 1
        for item in items:
            if isinstance(item, dict) and not item.get('id'):
                while f'{prefix}{n}' in taken:
                    n += 1
                item['id'] = f'{prefix}{n}'
                taken.add(item['id'])
                added.append(item['id'])
    summary = _write_checked(path, raw)
    return {'op': 'ids', 'added': added, 'timeline': summary}


def _assign(target: dict, dotted: str, value) -> None:
    *parents, last = dotted.split('.')
    for name in parents:
        target = target.setdefault(name, {})
        if not isinstance(target, dict):
            raise RnError(f'{dotted}: {name} is not an object')
    target[last] = value


def set_op(path: Path, ref: str, assignments: list[str], unset: list[str]) -> dict:
    """key=value with value as JSON when it parses (numbers, objects, true) and as a string otherwise; dotted keys reach
    into objects (meta.label=hero, transition.duration=0.3)."""
    raw = _read(path)
    _, items, index = _locate(raw, ref)
    item = items[index]
    for assignment in assignments:
        key, sep, text = assignment.partition('=')
        if not sep or not key:
            raise RnError(f'{assignment}: expected key=value')
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = text
        _assign(item, key, value)
    for key in unset:
        *parents, last = key.split('.')
        target = item
        for name in parents:
            target = target.get(name, {}) if isinstance(target, dict) else {}
        if isinstance(target, dict):
            target.pop(last, None)
    return {'op': 'set', 'clip': ref, 'timeline': _write_checked(path, raw)}


def split_op(path: Path, ref: str, at: float, new_id: str | None) -> dict:
    """Cuts a clip at timeline second `at` into two that play exactly as the one did."""
    raw = _read(path)
    key, items, index = _locate(raw, ref)
    if key == 'audio':
        raise RnError(f'{ref}: audio items are split by giving two items in/out ranges')
    original = items[index]
    for blocked in ('keys', 'ranges'):
        if blocked in original:
            raise RnError(f'{ref}: has "{blocked}", which are relative to the clip; split it by hand')
    clip, fps = _parsed(path, key, index)
    k = round(at * fps) - clip.timeline_start
    if not 0 < k < clip.frames:
        raise RnError(f'{ref}: {at}s is not inside the clip (timeline frames {clip.timeline_start}..{clip.timeline_start + clip.frames - 1})')
    head_s, tail_s = float(k / fps), float((clip.frames - k) / fps)
    first, second = dict(original), {k2: v for k2, v in original.items() if k2 not in ('transition', 'fade_in')}
    first.pop('fade_out', None)
    first.pop('until', None)
    if clip.kind in ('video', 'sequence', 'edit'):
        cut = round(clip.start_s + head_s * clip.speed, 6)
        second['in'] = cut
        if 'duration' in original or 'until' in original:
            first['duration'] = round(head_s, 6)
            if 'duration' in original:
                second['duration'] = round(tail_s, 6)
        else:
            first['out'] = cut
    else:  # stills, colours and held frames: durations only
        first['duration'] = round(head_s, 6)
        first.pop('out', None)
        if 'until' not in original:
            second['duration'] = round(tail_s, 6)
            second.pop('out', None)
    if key != 'tracks[0]':
        second['at'] = round(float((clip.timeline_start + k) / fps), 6)
    if original.get('id'):
        second['id'] = new_id or _fresh(raw, original['id'])
    elif new_id:
        second['id'] = new_id
    items[index:index + 1] = [first, second]
    return {'op': 'split', 'clip': ref, 'into': [first.get('id'), second.get('id')], 'timeline': _write_checked(path, raw)}


def delete_op(path: Path, ref: str, lift: bool) -> dict:
    """Removes a clip. On the main track the rest ripples left; --lift leaves a background-coloured gap instead."""
    raw = _read(path)
    key, items, index = _locate(raw, ref)
    if lift:
        if key != 'tracks[0]':
            raise RnError('--lift is for the main track (overlays and audio leave no gap to fill)')
        clip, fps = _parsed(path, key, index)
        gap = {'color': (raw.get('output') or {}).get('background', '#000000'), 'duration': round(float(clip.frames / fps), 6)}
        items[index] = gap
    else:
        del items[index]
    return {'op': 'delete', 'clip': ref, 'lift': lift, 'timeline': _write_checked(path, raw)}


def _destination(raw: dict, before: str | None, after: str | None, track: str | None) -> tuple[list, int]:
    if sum(x is not None for x in (before, after, track)) != 1:
        raise RnError('give exactly one of --before ID, --after ID or --track N|audio')
    if track is not None:
        if track == 'audio':
            return raw.setdefault('audio', []), len(raw['audio'])
        if not track.isdigit():
            raise RnError('--track: a track number (0 = main) or "audio"')
        tracks = raw.setdefault('tracks', [])
        number = int(track)
        if number == len(tracks):
            tracks.append({'clips': []})
        if number >= len(tracks):
            raise RnError(f'--track {number}: tracks are 0..{len(tracks) - 1}; {len(tracks)} adds one')
        return tracks[number].setdefault('clips', []), len(tracks[number]['clips'])
    _, items, index = _locate(raw, before or after)
    return items, index + (1 if after else 0)


def insert_op(path: Path, clip_json: str, before: str | None, after: str | None, track: str | None) -> dict:
    """Adds a clip given as JSON (or @file.json)."""
    text = Path(clip_json[1:]).read_text(encoding='utf-8-sig') if clip_json.startswith('@') else clip_json
    try:
        clip = json.loads(text)
    except json.JSONDecodeError as error:
        raise RnError(f'--clip: invalid JSON: {error}') from None
    if not isinstance(clip, dict):
        raise RnError('--clip: one clip object')
    raw = _read(path)
    items, index = _destination(raw, before, after, track)
    items.insert(index, clip)
    return {'op': 'insert', 'clip': clip.get('id'), 'timeline': _write_checked(path, raw)}


def move_op(path: Path, ref: str, before: str | None, after: str | None, track: str | None) -> dict:
    raw = _read(path)
    _, items, index = _locate(raw, ref)
    clip = items.pop(index)
    marker = object()  # keep the clip's slot while the destination is looked up, so --before its neighbour works
    items.insert(index, marker)
    target, position = _destination(raw, before, after, track)
    target.insert(position, clip)
    for container in (items, target):
        if marker in container:
            container.remove(marker)
    return {'op': 'move', 'clip': ref, 'timeline': _write_checked(path, raw)}


def upgrade_op(path: Path) -> dict:
    """Rewrites a version-1 edit under version-2 names ("style", keys left / top); the timeline stays the same."""
    raw = _read(path)
    if raw.get('version') == 2:
        raise RnError(f'{path.name} is already version 2')
    return {'op': 'upgrade', 'timeline': _write_checked(path, upgrade(raw))}


def _edl_relative(path: Path, given: str, flag: str) -> str:
    """A file named on the command line (relative to the current folder) as the edit refers to it: relative to the
    EDL's folder. A path that exists only relative to the EDL is kept as given."""
    here = Path(given).expanduser()
    if here.is_file():
        try:
            return Path(os.path.relpath(os.path.abspath(here), os.path.abspath(path.parent))).as_posix()
        except ValueError:  # another drive (Windows)
            return Path(os.path.abspath(here)).as_posix()
    if (path.parent / here).is_file():
        return here.as_posix()
    raise RnError(f'{flag}{given}: no such file (relative to the current folder or to {path.name})')


def _beat_times(path: Path, listen: str) -> tuple[list[float], bool]:
    try:
        data = json.loads((path.parent / listen).read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as error:
        raise RnError(f'--listen {listen}: not JSON ({error}); write it with rn listen --json') from None
    beats = data.get('beats') if isinstance(data, dict) else None
    times = beats.get('times') if isinstance(beats, dict) else None
    if not times:
        raise RnError(f'--listen {listen}: no beats.times (rn listen MUSIC --json {Path(listen).name} writes them)')
    if not isinstance(times, list) or not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in times):
        raise RnError(f'--listen {listen}: beats.times is not a list of seconds')
    return [float(t) for t in times], bool(beats.get('clear', True))


def hits_op(path: Path, sfx: str, at: str, *, listen: str | None = None, offset: float = 0.0, pivot: float | None = None,
            gain: float | None = None, every: int = 1, prefix: str = 'sfx') -> dict:
    """One sound effect as an audio item (role sfx) at each hit point: every main-track cut (each clip's start but the
    first's), every beat of a rn listen result, or given seconds; --every N keeps the 1st, (N+1)th, ... of them.
    Cuts and beats are written as anchors ({"clip": id}, or {"clip": i} for a clip without an id; {"beat": n, "listen":
    f}), so the items follow later edits; given seconds stay put. offset moves every hit point (negative: earlier) and
    goes into the anchor; pivot puts that second of the sound (a whoosh's peak) on the point. A point before 0 s or at or
    after the end of the main track is not placed and is listed under "skipped"."""
    if every < 1:
        raise RnError('--every: 1 or more')
    if not ID.match(f'{prefix}1'):
        raise RnError(f'--id-prefix {prefix}: letters, digits, "_", "-" and "." (starting with a letter or digit)')
    raw = _read(path)
    src = _edl_relative(path, sfx, '')
    edit = load(path, placeholders=True)
    info = probe(path.parent / src)
    if not info['audio']:
        raise RnError(f'{sfx}: has no audio stream')
    if pivot is not None:
        length = info['audio']['duration'] or info['duration'] or 0
        if not 0 <= pivot < length:
            raise RnError(f'--pivot {pivot}: a second inside {Path(src).name} (0..{length:.3f})')
    if at == 'beats' and listen is None:
        raise RnError('--at beats needs --listen LISTEN.json (a rn listen --json result)')
    if at != 'beats' and listen is not None:
        raise RnError('--listen goes with --at beats')
    warnings, points = [], []  # points: (label, anchor or None for plain seconds, seconds)
    if at == 'cuts':
        for i, clip in enumerate(edit.base[1:], 1):
            points.append((clip.id or f'clip {i}', {'clip': clip.id or i}, float(clip.timeline_start / edit.fps)))
        if not points:
            raise RnError('--at cuts: the main track has one clip, so no cuts')
    elif at == 'beats':
        listen = _edl_relative(path, listen, '--listen ')
        times, clear = _beat_times(path, listen)
        if not clear:
            warnings.append(f'{Path(listen).name}: no clear pulse (beats.clear is false)')
        points = [(f'beat {n}', {'beat': n, 'listen': listen}, t) for n, t in enumerate(times)]
    else:
        try:
            given = [float(part) for part in at.split(',') if part.strip()]
        except ValueError:
            raise RnError(f'--at {at}: cuts, beats, or seconds like 1.2,3.5') from None
        if not given or any(t < 0 for t in given):
            raise RnError(f'--at {at}: cuts, beats, or seconds >= 0 like 1.2,3.5')
        points = [(f'{t:g}s', None, t) for t in given]
    if any(isinstance(anchor, dict) and isinstance(anchor.get('clip'), int) for _, anchor, _ in points[::every]):
        warnings.append('clips without an id are anchored by index ({"clip": i}), which points at another clip once one is '
                        'added or removed before it; rn edit EDL ids gives every clip one')

    taken = _all_ids(raw) | {c.id for c in edit.base + edit.overlays if c.id}
    n, items, placed, skipped = 1, [], [], []
    for label, anchor, seconds in points[::every]:
        point = seconds + offset
        if not 0 <= point < edit.duration:
            where = 'before 0 s' if point < 0 else f'at or after the end of the main track ({edit.duration:.3f}s)'
            skipped.append({'hit': label, 'time': round(point, 4), 'reason': where})
            continue
        while f'{prefix}{n}' in taken:
            n += 1
        ident = f'{prefix}{n}'
        taken.add(ident)
        if anchor is None:
            position = round(point, 6)
        else:
            position = {**anchor, 'offset': offset} if offset else anchor
        item = {'id': ident, 'src': src, 'role': 'sfx', 'at': position}
        if pivot is not None:
            item['pivot'] = pivot
        if gain is not None:
            item['gain_db'] = gain
        items.append(item)
        placed.append({'id': ident, 'hit': label, 'time': round(point, 4)})
    if not items:
        raise RnError(f'no hit lands inside the main track (0..{edit.duration:.3f}s): '
                      + ', '.join(f'{s["hit"]} at {s["time"]}s' for s in skipped[:8]) + ('...' if len(skipped) > 8 else ''))
    audio = raw.get('audio') or []
    audio.extend(items)
    raw['audio'] = audio
    summary = _write_checked(path, raw)
    return {'op': 'hits', 'src': src, 'at': at if at in ('cuts', 'beats') else 'times', 'added': [p['id'] for p in placed],
            'hits': placed, 'skipped': skipped, 'warnings': warnings, 'timeline': summary}


def _overlap(shot: dict) -> tuple[int, int, str] | None:
    """A dissolve or wipe rn shots measured, as an xfade overlap: (first source frame, frames, xfade type). A dissolve is
    made as xfade "fade" (the smooth mix; xfade's "dissolve" is a pixel-noise effect)."""
    t = shot.get('transition') or {}
    if shot.get('transition_in') not in ('dissolve', 'wipe') or not t.get('frames') or t.get('start_frame') is None:
        return None
    edge = t.get('edge')
    return int(t['start_frame']), int(t['frames']), f'wipe{edge}' if edge in ('left', 'right', 'up', 'down') else 'fade'


def init_op(path: Path, from_shots: str, *, size: str | None = None, fps: str | None = None, src: str | None = None,
            slots: str = 'gen', force: bool = False) -> dict:
    """A new edit whose main track is the reference's shots (an rn shots --json result), frame for frame: one clip per
    shot (s01, s02, ...), each lasting what its shot lasts. Dissolves and wipes are made again as xfade overlaps of their
    measured frames (the clip before runs on through the transition, the clip after starts where it starts), so the
    total length is the reference's; a fade through black or white is the clip before fading out (and holding) and the
    clip after fading in, as measured. Without --src each clip's source is <slots>/<id>.mp4, not made yet: a placeholder
    card until it exists. With --src every clip is that shot of the reference itself (its sound as one audio item):
    a rebuild to check the skeleton against the reference. meta keeps what the shot measured (times, transition,
    keyframe, camera, speed) for whoever fills the slot."""
    if path.exists() and not force:
        raise RnError(f'{path} exists; --force replaces it')
    try:
        measured = json.loads(Path(from_shots).read_text(encoding='utf-8-sig'))
        shots, src_fps, ref_frames = measured['shots'], Fraction(str(measured['fps'])), int(measured['frames'])
        ref_size = measured['size']
    except FileNotFoundError:
        raise RnError(f'--from-shots {from_shots}: no such file') from None
    except (ValueError, KeyError, TypeError) as error:
        raise RnError(f'--from-shots {from_shots}: not an rn shots --json result ({error})') from None
    if not shots:
        raise RnError(f'--from-shots {from_shots}: no shots')
    try:
        out_fps = Fraction(str(fps)) if fps else src_fps
        width, height = (int(v) for v in size.lower().split('x')) if size else (int(ref_size[0]), int(ref_size[1]))
    except (ValueError, ZeroDivisionError):
        raise RnError('--size WxH (e.g. 1080x1920), --fps a number or "30000/1001"') from None
    width, height = width // 2 * 2, height // 2 * 2
    path.parent.mkdir(parents=True, exist_ok=True)
    ref = None
    if src:
        ref = _edl_relative(path, src, '--src ')
        info = probe(path.parent / ref)
        video = info['video']
        if not video or abs((video['frames'] or 0) - ref_frames) > 1 or Fraction(video['fps']) != src_fps:
            raise RnError(f'--src {src}: {video and video["frames"]} frames at {video and video["fps"]} fps; {from_shots} measured '
                          f'{ref_frames} at {src_fps}: another file')

    # which transitions are made as overlaps: an overlap must be shorter than both clips and clear of the one before
    overlaps = {k: _overlap(s) for k, s in enumerate(shots) if k and _overlap(s)}
    dropped = []
    while True:
        starts = [overlaps[k][0] if k in overlaps else s['start_frame'] for k, s in enumerate(shots)]
        ends = [sum(overlaps[k + 1][:2]) if k + 1 in overlaps else (shots[k + 1]['start_frame'] if k + 1 < len(shots) else ref_frames)
                for k in range(len(shots))]
        bad = next((k for k, (lo, n, _) in overlaps.items()
                    if n > min(ends[k - 1] - starts[k - 1], ends[k] - starts[k]) - 1
                    or n + (overlaps[k - 1][1] if k - 1 in overlaps else 0) > ends[k - 1] - starts[k - 1]), None)
        if bad is None:
            break
        dropped.append(f's{bad + 1:02d}')
        del overlaps[bad]

    def at(frame: int) -> int:  # a reference frame on the output clock
        return round(frame * out_fps / src_fps)

    def secs(frames: int) -> float:
        return round(float(frames / out_fps), 6)

    width_id = max(2, len(str(len(shots))))
    clips, fades = [], {}
    for k, shot in enumerate(shots):
        ident = f's{k + 1:0{width_id}d}'
        t = shot.get('transition') or {}
        length = at(ends[k]) - at(starts[k])
        meta = {'label': f'shot {k}: {shot["start"]:.2f}-{shot["end"]:.2f}s', 'shot': k,
                'source': {'start': shot['start'], 'end': shot['end'], 'start_frame': shot['start_frame'], 'end_frame': shot['end_frame']},
                'transition_in': shot.get('transition_in')}
        for key in ('transition', 'flash_s', 'beat'):
            if shot.get(key) is not None:
                meta[key] = shot[key]
        if shot.get('keyframe', {}).get('path'):
            kf = Path(shot['keyframe']['path'])
            meta['keyframe'] = _edl_relative(path, str(kf), '') if kf.is_file() else kf.as_posix()
        if shot.get('motion'):
            meta['motion'] = {key: shot['motion'][key] for key in ('rigid_fit', 'zoom_per_s', 'pan_px_per_s', 'pan_dir_deg') if key in shot['motion']}
        if shot.get('speed'):
            meta['speed'] = {key: shot['speed'][key] for key in ('ramp', 'longest_hold_s') if key in shot['speed']}
        if ident in dropped:
            meta['transition_made'] = 'cut: no room for the overlap next to the transition before it'
        clip = {'id': ident, **({'src': ref, 'in': {'frame': starts[k]}, 'out': {'frame': ends[k]}, 'audio': False} if ref else
                                {'src': f'{slots}/{ident}.mp4', 'duration': secs(length)}), 'meta': meta}
        if k in overlaps:
            _, n, kind = overlaps[k]
            clip['transition'] = {'type': kind, 'duration': secs(at(starts[k] + n) - at(starts[k]))}
        elif shot.get('transition_in') == 'fade' and not ref and t.get('out_frames') is not None:
            color = t.get('through', 'black')
            out_n, hold_n, in_n = (secs(at(t.get(key) or 0)) for key in ('out_frames', 'hold_frames', 'in_frames'))
            before = clips[-1]
            fades.setdefault(k - 1, []).append(
                f'fade=t=out:st={max(0.0, float(before["duration"]) - out_n - hold_n):.6g}:d={out_n:.6g}:color={color}')
            fades.setdefault(k, []).insert(0, f'fade=t=in:st=0:d={in_n:.6g}:color={color}')
        clips.append(clip)
    for k, chain in fades.items():  # ponytail: placeholder slots only; with --src the reference's own frames hold the fade
        clips[k]['vf'] = ','.join(chain)
    raw = {'version': 2, 'output': {'width': width, 'height': height, 'fps': int(out_fps) if out_fps.denominator == 1 else str(out_fps)},
           'tracks': [{'clips': clips}],
           'note': f'the shots of {_edl_relative(path, from_shots, "--from-shots ")} ({ref_frames} frames at {src_fps} fps)'}
    if ref and info['audio']:
        raw['audio'] = [{'id': 'reference', 'src': ref, 'role': 'music'}]
    summary = _write_checked(path, raw)
    made = {}
    for clip in clips:
        kind = clip.get('transition', {}).get('type') or ('fade in' if 'fade=t=in' in clip.get('vf', '') else None)
        if kind:
            made[kind] = made.get(kind, 0) + 1
    return {'op': 'init', 'path': str(path), 'clips': [c['id'] for c in clips], 'transitions': made, 'dropped': dropped,
            'source': ref or f'{slots}/<id>.mp4', 'timeline': summary}


def silences_op(path: Path, ref: str, listen: str, *, min_silence: float = 0.5, pad: float = 0.1, keep_gap: float = 0.0) -> dict:
    """Cuts a main-track clip's long silences out by writing its "ranges": the spans of its source between the silences
    of `listen` (rn listen SOURCE --json, or rn listen SOURCE --speech --json) that last at least `min_silence` (silence
    before the first word or after the last is cut at any length, leaving the pad). `pad`
    seconds stay next to the speech on each side of a cut; `keep_gap` seconds of each removed pause stay in all (half on
    each side, at least the pads). Only inside the clip's own in/out (or its existing ranges, so it can run again)."""
    if min_silence <= 0 or pad < 0 or keep_gap < 0:
        raise RnError('--min must be > 0; --pad and --keep-gap >= 0')
    raw = _read(path)
    key, items, index = _locate(raw, ref)
    if key != 'tracks[0]':
        raise RnError(f'{ref}: silences are cut on the main track (ranges play back to back there)')
    original = items[index]
    if 'keys' in original:
        raise RnError(f'{ref}: has "keys", which ranges replace; remove them first')
    listen = _edl_relative(path, listen, '--listen ')
    try:
        data = json.loads((path.parent / listen).read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as error:
        raise RnError(f'--listen {listen}: not JSON ({error}); write it with rn listen SOURCE --json') from None
    silences = data.get('silences') if isinstance(data, dict) else None
    if not isinstance(silences, list):
        raise RnError(f'--listen {listen}: no "silences" (rn listen SOURCE --speech --json {Path(listen).name} writes them)')
    warnings = []
    edit = load(path)
    if 'ranges' in original:
        spans = [(float(a), float(b)) for a, b in original['ranges']]
        fps = edit.fps
    else:
        clip, fps = _parsed(path, key, index)
        if clip.kind not in ('video', 'edit'):
            raise RnError(f'{ref}: a {clip.kind} clip has no sound to cut silences from')
        spans = [(clip.start_s, clip.start_s + float(clip.frames / fps) * clip.speed)]
        if original.get('src') and isinstance(data.get('path'), str) and Path(data['path']).name != Path(original['src']).name:
            warnings.append(f'{Path(listen).name} measured {Path(data["path"]).name}, the clip plays {Path(original["src"]).name}')
    half = max(pad, keep_gap / 2)
    cuts = []
    for silence in silences:
        start, end = float(silence['start']), float(silence['end'])
        head, foot = start <= spans[0][0] + 1e-6, end >= spans[-1][1] - 1e-6
        if end - start < min_silence and not (head or foot):  # dead air before the first word or after the last always goes
            continue
        lead, tail = start + half, end - half
        if head:  # only the pad stays before the first word
            lead, tail = spans[0][0], end - pad
        if foot:
            lead, tail = start + pad, spans[-1][1]
        if tail > lead:
            cuts.append((lead, tail))
    frame = float(1 / fps)
    kept = []
    for a, b in spans:
        pieces = [(a, b)]
        for lead, tail in cuts:
            pieces = [part for x, y in pieces for part in ((x, min(y, lead)), (max(x, tail), y)) if part[1] - part[0] > 0]
        kept += [(x, y) for x, y in pieces if y - x >= frame]  # under a frame would play nothing
    if not kept:
        raise RnError(f'{ref}: every second of the clip is in a silence of {Path(listen).name}; nothing left to play')
    before, after = sum(b - a for a, b in spans), sum(b - a for a, b in kept)
    for field in ('in', 'out', 'duration'):
        original.pop(field, None)
    original['ranges'] = [[round(a, 6), round(b, 6)] for a, b in kept]
    summary = _write_checked(path, raw)
    return {'op': 'silences', 'clip': ref, 'listen': listen, 'ranges': original['ranges'], 'spans': len(kept),
            'removed': round(before - after, 3), 'kept': round(after, 3), 'warnings': warnings, 'timeline': summary}


def batch_op(path: Path, steps: list[str], apply) -> dict:
    """Several operations as one: each is applied and checked in turn on a working copy next to the EDL (so relative
    paths resolve the same), which replaces the EDL only after the last step passes. A failing step writes nothing.
    `apply(path, line)` runs one operation line ("set c3 out=5.5") against a file."""
    if not steps:
        raise RnError('batch: no operations')
    raw = _read(path)
    work = path.with_name(f'.{path.stem}.rn-batch.json')
    work.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    done = []
    try:
        for number, line in enumerate(steps, 1):
            try:
                result = apply(work, line)
            except RnError as error:
                raise RnError(f'step {number} ({line}): {error}; nothing written') from None
            done.append(summary_lines(result)[0])
        os.replace(work, path)
    finally:
        work.unlink(missing_ok=True)
    return {'op': 'batch', 'steps': done, 'timeline': result['timeline']}


def summary_lines(result: dict) -> list[str]:
    head = {'upgrade': lambda r: 'upgraded to version 2',
            'ids': lambda r: f'ids added: {", ".join(r["added"]) or "none (every clip has one)"}',
            'set': lambda r: f'set {r["clip"]}',
            'split': lambda r: f'split {r["clip"]} into {r["into"][0]} + {r["into"][1]}',
            'delete': lambda r: f'deleted {r["clip"]}' + (' (lifted: gap kept)' if r['lift'] else ''),
            'insert': lambda r: f'inserted {r["clip"] or "clip"}',
            'move': lambda r: f'moved {r["clip"]}',
            'hits': lambda r: f'added {len(r["added"])} x {Path(r["src"]).name} at {r["at"]}: '
                              + (r['added'][0] if len(r['added']) == 1 else f'{r["added"][0]}..{r["added"][-1]}'),
            'silences': lambda r: f'{r["clip"]}: cut {r["removed"]}s of silence, {r["spans"]} spans play ({r["kept"]}s)',
            'batch': lambda r: f'{len(r["steps"])} steps: ' + '; '.join(r['steps']),
            'init': lambda r: f'created {r["path"]}: {len(r["clips"])} clips from the reference\'s shots, sources {r["source"]}'
                              + (f'; transitions made: {", ".join(f"{n} {k}" for k, n in r["transitions"].items())}' if r['transitions'] else '')
                              + (f'; made as cuts (no room): {", ".join(r["dropped"])}' if r['dropped'] else '')}[result['op']](result)
    out = result['timeline']['output']
    lines = [head]
    if result['op'] == 'hits':
        def listed(entries: list[str]) -> str:
            return ', '.join(entries[:24]) + (f', ... ({len(entries)} in all; --json lists each)' if len(entries) > 24 else '')
        lines.append('  hits at ' + listed([f'{h["time"]:g}' for h in result['hits']]) + ' s')
        skipped = result['skipped']
        if skipped:
            reasons = '; '.join(dict.fromkeys(s['reason'] for s in skipped))
            lines.append(f'  not placed, {reasons}: ' + listed([f'{s["hit"]} ({s["time"]:g}s)' for s in skipped]))
        lines += [f'warning: {w}' for w in result['warnings']]
    if result['op'] == 'silences':
        lines.append('  ranges ' + ', '.join(f'{a:g}-{b:g}' for a, b in result['ranges'][:12]) + (' ...' if len(result['ranges']) > 12 else ''))
        lines += [f'warning: {w}' for w in result['warnings']]
    lines.append(f'timeline {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s')
    lines += [f'  {c["id"] or c["where"]}: frames {c["start_frame"]}..{c["start_frame"] + c["frames"] - 1}' for c in result['timeline']['base']]
    lines += [f'  {c["id"] or c["where"]}: overlay frames {c["start_frame"]}..{c["start_frame"] + c["frames"] - 1}'
              for c in result['timeline']['overlays']]
    missing = [w for w in result['timeline']['warnings'] if '; placeholder card for ' in w]
    if missing:
        names = [w.split(': ', 1)[1].split(' is missing')[0] for w in missing]
        lines.append(f'not made yet ({len(names)}; placeholder cards in rn assemble --placeholders): '
                     + ', '.join(names[:12]) + (', ...' if len(names) > 12 else ''))
    lines += [f'warning: {w}' for w in result['timeline']['warnings'] if w not in missing]
    return lines
