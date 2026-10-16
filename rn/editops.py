"""rn edit: change an EDL by operations on clip ids instead of rewriting its JSON. Each operation is checked by laying the
whole edit out again (as rn assemble --dry-run would) and only then written back, so a bad step never lands.

The main track always plays back to back, so removing or trimming a clip there ripples everything after it; overlays and
audio anchored by clip id ({"clip": "id"}) follow, ones placed at fixed seconds do not.
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

from .edl import Clip, load
from .ff import RnError


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise RnError(f'no such file: {path}') from None
    except json.JSONDecodeError as error:
        raise RnError(f'{path}: invalid JSON: {error}') from None


def _write_checked(path: Path, raw: dict) -> dict:
    """Lays the edited JSON out next to the original (relative paths resolve the same), then replaces it."""
    trial = path.with_name(f'.{path.stem}.rn-edit.json')
    trial.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + '\n')
    try:
        summary = load(trial).summary()
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
    edit = load(path)
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
    text = Path(clip_json[1:]).read_text() if clip_json.startswith('@') else clip_json
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


def summary_lines(result: dict) -> list[str]:
    head = {'ids': lambda r: f'ids added: {", ".join(r["added"]) or "none (every clip has one)"}',
            'set': lambda r: f'set {r["clip"]}',
            'split': lambda r: f'split {r["clip"]} into {r["into"][0]} + {r["into"][1]}',
            'delete': lambda r: f'deleted {r["clip"]}' + (' (lifted: gap kept)' if r['lift'] else ''),
            'insert': lambda r: f'inserted {r["clip"] or "clip"}',
            'move': lambda r: f'moved {r["clip"]}'}[result['op']](result)
    out = result['timeline']['output']
    lines = [head, f'timeline {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s']
    lines += [f'  {c["id"] or c["where"]}: frames {c["start_frame"]}..{c["start_frame"] + c["frames"] - 1}' for c in result['timeline']['base']]
    lines += [f'  {c["id"] or c["where"]}: overlay frames {c["start_frame"]}..{c["start_frame"] + c["frames"] - 1}'
              for c in result['timeline']['overlays']]
    lines += [f'warning: {w}' for w in result['timeline']['warnings']]
    return lines
