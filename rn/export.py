"""rn export: an EDL as an editable project for other editors: OpenTimelineIO (.otio), Final Cut Pro X (.fcpxml) or
Premiere / Resolve / FCP 7 (.xml), with media referenced by absolute path. Cuts, trims, speed, transitions, nesting, overlay
and audio placement in time carry over; what NLE interchange cannot express (position and size, keys, blend, masks, raw
filters, gain curves) is kept as "rn" metadata and listed in the result.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .edl import Clip, Edit, load
from .ff import RnError, probe

ADAPTERS = {'.otio': 'otio_json', '.fcpxml': 'fcpx_xml', '.xml': 'fcp_xml'}
_SEQ = re.compile(r'%0?(\d*)d')


def _rt(otio, frames: float, rate: float):
    return otio.opentime.RationalTime(frames, rate)


def _lost(clip: Clip) -> dict:
    """What an NLE file cannot carry for this clip, kept as metadata."""
    keep = {k: getattr(clip, k) for k in ('keys', 'vf', 'af', 'blend') if getattr(clip, k)}
    if clip.mask is not None:
        keep['mask'] = {'source': str(clip.mask.source), 'mode': clip.mask_mode, 'invert': clip.mask_invert}
    if (clip.x, clip.y, clip.w, clip.h, clip.opacity) != (0, 0, None, None, 1.0):
        keep['place'] = {'x': clip.x, 'y': clip.y, 'w': clip.w, 'h': clip.h, 'opacity': clip.opacity}
    if clip.fade_in or clip.fade_out:
        keep['fades'] = [clip.fade_in, clip.fade_out]
    return {key: (value if isinstance(value, (dict, str, int, float)) else str(value)) for key, value in keep.items()}


STILL_HOURS = 1  # stills are endless in an NLE; FCP X still wants a range


def _available(otio, frames: float, rate: float):
    return otio.opentime.TimeRange(_rt(otio, 0, rate), _rt(otio, frames, rate))


def _reference(otio, clip: Clip, rate: float):
    """Media with its available range (Final Cut X refuses a reference without one). Not for solids (see _item)."""
    if clip.kind == 'sequence':
        pattern = clip.source
        match = _SEQ.search(pattern.name)
        count = sum(1 for f in pattern.parent.iterdir() if f.name.endswith(pattern.name[match.end():]))
        return otio.schema.ImageSequenceReference(
            target_url_base=str(pattern.parent.resolve()) + '/', name_prefix=pattern.name[:match.start()],
            name_suffix=pattern.name[match.end():], start_frame=clip.seq_start, frame_step=1, rate=float(clip.src_fps),
            frame_zero_padding=int(match.group(1) or 0), available_range=_available(otio, count, float(clip.src_fps)))
    reference = otio.schema.ExternalReference(target_url=str(clip.source.resolve()))
    if clip.kind == 'image':
        reference.available_range = _available(otio, STILL_HOURS * 3600 * rate, rate)
    else:
        video = probe(clip.source)['video']
        src_rate = float(clip.src_fps)
        reference.available_range = _available(otio, video['frames'] or round((video['duration'] or 0) * src_rate), src_rate)
    return reference


def _item(otio, clip: Clip, edit: Edit, head: int, tail: int, lost: list):
    """One clip on a track: `head`/`tail` timeline frames are given to the transitions on either side."""
    rate = float(edit.fps)
    frames = clip.frames - head - tail
    meta = _lost(clip)
    if clip.kind == 'color':  # no interchange format carries a solid (FCP X has no generator reference): a gap, colour kept
        meta['color'] = clip.color
    if meta:
        lost.append({'clip': clip.id or clip.where, 'kept_as_metadata': sorted(meta)})
    if clip.kind == 'edit':
        stack = _stack(otio, load(clip.source), lost)
        start = clip.start_s + head / rate * clip.speed
        stack.source_range = otio.opentime.TimeRange(_rt(otio, start * rate, rate), _rt(otio, frames, rate))
        stack.name = clip.id or clip.source.stem
        stack.metadata['rn'] = meta
        return stack
    if clip.kind == 'color':
        return otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=_rt(otio, frames, rate)), metadata={'rn': meta})
    src_rate = float(clip.src_fps) if clip.src_fps else rate
    start = (clip.start_s + head / rate * clip.speed) if clip.kind in ('video', 'sequence') else head / rate
    item = otio.schema.Clip(name=clip.id or (clip.source.name if clip.source else clip.color), media_reference=_reference(otio, clip, rate),
                            source_range=otio.opentime.TimeRange(_rt(otio, round(start * src_rate), src_rate),
                                                                 _rt(otio, frames * src_rate / rate, src_rate)))
    if clip.speed != 1:
        item.effects.append(otio.schema.LinearTimeWarp(time_scalar=clip.speed))
    item.metadata['rn'] = {**meta, **({'meta': clip.meta} if clip.meta is not None else {})}
    return item


def _lanes(items: list, start_of, length_of) -> list[list]:
    """Greedy: overlapping items go to separate lanes (an NLE track holds one item at a time)."""
    lanes: list[list] = []
    for item in sorted(items, key=start_of):
        lane = next((l for l in lanes if start_of(l[-1]) + length_of(l[-1]) <= start_of(item)), None)
        if lane is None:
            lanes.append([item])
        else:
            lane.append(item)
    return lanes


def _stack(otio, edit: Edit, lost: list):
    rate = float(edit.fps)
    stack = otio.schema.Stack(name=edit.path.stem)
    main = otio.schema.Track(name='V1', kind=otio.schema.TrackKind.Video)
    for i, clip in enumerate(edit.base):
        incoming = clip.transition_frames
        outgoing = edit.base[i + 1].transition_frames if i + 1 < len(edit.base) else 0
        if incoming:
            main.append(otio.schema.Transition(name=clip.transition, transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                                               in_offset=_rt(otio, incoming // 2, rate), out_offset=_rt(otio, incoming - incoming // 2, rate),
                                               metadata={'rn': {'xfade': clip.transition}}))
        # A transition's overlap is split at the cut: half from the end of one clip, half from the start of the next.
        main.append(_item(otio, clip, edit, incoming - incoming // 2, outgoing // 2, lost))
    stack.append(main)
    for n, lane in enumerate(_lanes(edit.overlays, lambda c: c.timeline_start, lambda c: c.frames), start=2):
        track = otio.schema.Track(name=f'V{n}', kind=otio.schema.TrackKind.Video)
        position = 0
        for clip in lane:
            if clip.timeline_start > position:
                track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=_rt(otio, clip.timeline_start - position, rate))))
            track.append(_item(otio, clip, edit, 0, 0, lost))
            position = clip.timeline_start + clip.frames
        stack.append(track)
    lengths = {}
    for item in edit.audio:
        available = probe(item.source)['audio']['duration'] or 0
        lengths[id(item)] = min(item.duration or available - item.start_s, edit.duration - item.at)
    for n, lane in enumerate(_lanes(edit.audio, lambda a: a.at, lambda a: lengths[id(a)]), start=1):
        track = otio.schema.Track(name=f'A{n}', kind=otio.schema.TrackKind.Audio)
        position = 0.0
        for item in lane:
            if item.at > position:
                track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=_rt(otio, round((item.at - position) * rate), rate))))
            seconds = lengths[id(item)]
            meta = {k: v for k, v in (('gain_db', item.gain_db), ('fade_in', item.fade_in), ('fade_out', item.fade_out),
                                      ('af', item.af), ('keys', str(item.keys) if item.keys else None), ('role', item.role)) if v}
            source = otio.schema.ExternalReference(target_url=str(item.source.resolve()),
                                                   available_range=_available(otio, round((probe(item.source)['audio']['duration'] or 0) * rate), rate))
            track.append(otio.schema.Clip(name=item.id or item.source.name, media_reference=source,
                                          source_range=otio.opentime.TimeRange(_rt(otio, round(item.start_s * rate), rate), _rt(otio, round(seconds * rate), rate)),
                                          metadata={'rn': {**meta, **({'meta': item.meta} if item.meta is not None else {})}}))
            position = item.at + round(seconds * rate) / rate
        stack.append(track)
    return stack


def export_command(edl_path: str, out: str, *, srt: str | None) -> dict:
    import opentimelineio as otio

    target = Path(out)
    adapter = ADAPTERS.get(target.suffix.lower())
    if not adapter:
        raise RnError(f'{out}: export to {", ".join(ADAPTERS)}')
    edit = load(edl_path)
    lost: list = []
    stack = _stack(otio, edit, lost)
    timeline = otio.schema.Timeline(name=edit.path.stem, global_start_time=_rt(otio, 0, float(edit.fps)))
    timeline.tracks = stack
    timeline.metadata['rn'] = {'width': edit.width, 'height': edit.height, 'fps': str(edit.fps)}
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        otio.adapters.write_to_file(timeline, str(target), adapter_name=adapter)
    except Exception as error:  # adapter plugins raise their own types
        raise RnError(f'{adapter} could not write {out}: {error} (install the export extra for .fcpxml / .xml)') from None
    result = {'path': str(target), 'adapter': adapter, 'frames': edit.total_frames, 'fps': str(edit.fps),
              'tracks': [{'name': t.name, 'kind': t.kind, 'items': len(t)} for t in stack], 'lost': lost}
    if srt:
        if not edit.subtitles:
            raise RnError('--srt: the edit has no "subtitles"')
        if edit.subtitles.suffix.lower() == '.srt':
            shutil.copyfile(edit.subtitles, srt)
        else:
            import pysubs2
            pysubs2.load(str(edit.subtitles)).save(srt, format_='srt')
        result['srt'] = srt
    return result


def summary_lines(result: dict) -> list[str]:
    lines = [f'{result["path"]} ({result["adapter"]})  {result["frames"]} frames at {result["fps"]} fps',
             '  ' + ', '.join(f'{t["name"]} {t["items"]} items' for t in result['tracks'])]
    if result['lost']:
        lines.append(f'  not expressible in NLE files, kept as "rn" metadata: '
                     + '; '.join(f'{l["clip"]}: {", ".join(l["kept_as_metadata"])}' for l in result['lost'][:10]))
    if 'srt' in result:
        lines.append(f'  subtitles: {result["srt"]}')
    return lines
