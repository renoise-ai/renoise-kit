"""rn compare: two videos side by side at the same times (a cut against its reference, or two versions of a cut).
Measurement only: it shows where they differ in picture, cut timing and (with --shots) each shot's transition and camera
move, never which is better."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from .ff import RnError, probe, run, source_fps
from .frames import _compose, _save_under, _timecode, extract

PER_PAGE = 6  # times per page: A on the top row, B underneath
CUT_TOLERANCE = 0.25  # seconds: a cut in B this close to one in A counts as the same cut
VIDEO_MODES = {  # --video --mode: how A and B share the picture, as the summary line names it
    'side': 'side by side: {}  (A left, B right)',
    'stack': 'stacked: {}  (A top, B bottom)',
    'blend': 'blend: {}  (B at 50% over A: matching edges line up, moved ones show twice)',
    'diff': 'difference: {}  (|A - B| per channel: black where they match, bright where they differ)',
}


def _video(path: str) -> dict:
    info = probe(path)
    if not info['video'] or info['video']['still']:
        raise RnError(f'{path}: not a video')
    return info


def match_cuts(a: list[float], b: list[float], tolerance: float = CUT_TOLERANCE) -> dict:
    """Pairs each cut in A with the nearest unused cut in B within `tolerance`; offset = B - A in seconds."""
    unused = list(b)
    pairs = []
    for t in a:
        near = min(unused, key=lambda u: abs(u - t), default=None)
        if near is not None and abs(near - t) <= tolerance:
            unused.remove(near)
            pairs.append({'a': t, 'b': near, 'offset': round(near - t, 4)})
        else:
            pairs.append({'a': t, 'b': None, 'offset': None})
    matched = [p['offset'] for p in pairs if p['offset'] is not None]
    return {'pairs': pairs, 'only_b': unused, 'matched': len(matched),
            'mean_abs_offset': round(sum(abs(o) for o in matched) / len(matched), 4) if matched else None}


def _shot_motion(shot: dict, width: int) -> dict:
    m = shot['motion']
    return {'zoom_per_s': m['zoom_per_s'], 'pan_widths_per_s': round(m['pan_px_per_s'] / float(width), 4),
            'pan_dir_deg': m['pan_dir_deg'], 'rigid_fit': m['rigid_fit']}


def _span(s: dict) -> dict:
    """A shot as a compare pair shows it: its time and how it begins, with the transition's frames (and a wipe's edge, a
    fade's colour and parts) when rn shots measured them."""
    span = {'shot': s['shot'], 'start': s['start'], 'end': s['end'], 'transition_in': s['transition_in']}
    if s.get('transition'):
        span['transition'] = s['transition']
    return span


def _begins(span: dict) -> str:
    how, t = span['transition_in'] or 'start', span.get('transition') or {}
    extra = [f'{t["frames"]}f'] if t.get('frames') else []
    extra += [x for x in (t.get('edge'), t.get('through')) if x]
    return f'{how} ({" ".join(extra)})' if extra else how


def match_shots(a: dict, b: dict) -> dict:
    """rn shots results of A and B on one clock: each A shot with the B shot that overlaps it most. Per pair the
    transitions (with their measured frames), durations and the camera numbers (zoom per second, pan in frame widths per second so sizes may differ,
    pan direction, rigid fit), and how much of each frame-pair stays identical. B shots that are no A shot's best match
    are listed apart."""
    pairs, used = [], set()
    for sa in a['shots']:
        best, overlap = None, 0.0
        for sb in b['shots']:
            o = min(sa['end'], sb['end']) - max(sa['start'], sb['start'])
            if o > overlap:
                best, overlap = sb, o
        pair = {'a': _span(sa), 'b': _span(best) if best else None,
                'covered': round(overlap / max(1e-6, sa['end'] - sa['start']), 3) if best else 0.0}
        if best:
            used.add(best['shot'])
            pair['motion'] = {'a': _shot_motion(sa, a['size'][0]), 'b': _shot_motion(best, b['size'][0])}
            pair['identical_pairs'] = {'a': sa['still']['identical_pairs'], 'b': best['still']['identical_pairs']}
        pairs.append(pair)
    return {'a': len(a['shots']), 'b': len(b['shots']), 'pairs': pairs,
            'only_b': [_span(s) for s in b['shots'] if s['shot'] not in used],
            'transitions_differ': sum(1 for p in pairs if p['b'] and p['b']['transition_in'] != p['a']['transition_in'])}


def _video_graph(mode: str, a: dict) -> str:
    """The filter graph that puts A (input 0) and B (input 1) in one picture. Both play from their first frame on their
    own clocks; the shorter one holds its last frame. side: same height (A's, at most 720) next to each other. stack: same
    width (A's, at most 720) one above the other. blend / diff: B fitted inside A's frame at that height (letterboxed if
    the shapes differ, never stretched), then mixed 50/50 or subtracted per RGB channel, so a pixel that is the same in
    both is black in diff."""
    if mode == 'side':
        height = min(720, a['height']) // 2 * 2
        return f'[0:v]scale=-2:{height},setsar=1[a];[1:v]scale=-2:{height},setsar=1[b];[a][b]hstack=inputs=2[v]'
    if mode == 'stack':
        width = min(720, a['width']) // 2 * 2
        return f'[0:v]scale={width}:-2,setsar=1[a];[1:v]scale={width}:-2,setsar=1[b];[a][b]vstack=inputs=2[v]'
    height = min(720, a['height']) // 2 * 2
    width = max(2, round(a['width'] * height / a['height'] / 2) * 2)
    mix = 'all_mode=normal:all_opacity=0.5' if mode == 'blend' else 'all_mode=difference'
    return (f'[0:v]scale={width}:{height},setsar=1,format=gbrp[a];'
            f'[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=gbrp[b];[b][a]blend={mix}[v]')


def compare_command(path_a: str, path_b: str, *, out: str | None, count: int | None, seconds: list[float] | None, width: int,
                    max_kb: int, cuts: bool, video: str | None, shots: bool = False, jobs: int | None = None,
                    mode: str = 'side') -> dict:
    from PIL import Image, ImageOps

    if mode not in VIDEO_MODES:
        raise RnError(f'--mode {mode}: expected one of {", ".join(VIDEO_MODES)}')
    info_a, info_b = _video(path_a), _video(path_b)
    fps_a, fps_b = source_fps(info_a), source_fps(info_b)
    frames_a, frames_b = info_a['video']['frames'], info_b['video']['frames']
    length = max(float(frames_a / fps_a), float(frames_b / fps_b))
    if seconds:
        times = sorted({t for t in seconds if 0 <= t < length})
    else:
        n = max(1, count or 12)
        times = [round(k * length / n, 3) for k in range(n)]
    if not times:
        raise RnError(f'no times inside the longer video ({length:.2f}s)')
    index_a = {t: math.floor(t * float(fps_a) + 1e-6) for t in times}
    index_b = {t: math.floor(t * float(fps_b) + 1e-6) for t in times}

    base = Path(out) if out else Path('sheets') / f'compare-{Path(path_a).stem}-{Path(path_b).stem}.jpg'
    base.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'a').mkdir()
        (Path(tmp) / 'b').mkdir()
        images = {}
        for side, path, indices, frames in (('a', path_a, index_a, frames_a), ('b', path_b, index_b, frames_b)):
            wanted = [i for i in indices.values() if i < frames]
            if wanted:  # the shorter video may have no frame at any of the chosen times
                images.update({(side, i): Image.open(p).convert('RGB') for i, p in extract(path, wanted, Path(tmp) / side, width=width)})
        tile_h = max(image.height for image in images.values())
        blank = Image.new('RGB', (width, tile_h))
        page_count = math.ceil(len(times) / PER_PAGE)
        for page in range(page_count):
            part = times[page * PER_PAGE:(page + 1) * PER_PAGE]
            row_a = [ImageOps.pad(images[('a', index_a[t])], (width, tile_h)) if ('a', index_a[t]) in images else blank for t in part]
            row_b = [ImageOps.pad(images[('b', index_b[t])], (width, tile_h)) if ('b', index_b[t]) in images else blank for t in part]
            labels = [f'A #{index_a[t]}  {_timecode(index_a[t], fps_a)}' for t in part] + \
                     [f'B #{index_b[t]}  {_timecode(index_b[t], fps_b)}' for t in part]
            target = base if page_count == 1 else base.with_name(f'{base.stem}-p{page + 1}{base.suffix or ".jpg"}')
            title = f'top A {Path(path_a).name} ({fps_a}fps {frames_a}f)   bottom B {Path(path_b).name} ({fps_b}fps {frames_b}f)'
            size = _save_under(_compose([], fps_a, len(part), title, row_a + row_b, labels), target, max_kb)
            pages.append({'path': str(target), 'times': part, 'bytes': size})

    result: dict = {'a': {'path': path_a, 'fps': str(fps_a), 'frames': frames_a, 'duration': round(float(frames_a / fps_a), 4)},
                    'b': {'path': path_b, 'fps': str(fps_b), 'frames': frames_b, 'duration': round(float(frames_b / fps_b), 4)},
                    'pages': pages}
    if cuts:
        from .cuts import detect
        cuts_a = [c['time'] for c in detect(str(Path(path_a).resolve()))['cuts']]
        cuts_b = [c['time'] for c in detect(str(Path(path_b).resolve()))['cuts']]
        result['cuts'] = {'a': cuts_a, 'b': cuts_b, **match_cuts(cuts_a, cuts_b)}
    if shots:
        from .shots import shots_command
        result['shots'] = match_shots(shots_command(path_a, jobs=jobs), shots_command(path_b, jobs=jobs))
    if video:
        run(['ffmpeg', '-i', path_a, '-i', path_b, '-filter_complex', _video_graph(mode, info_a['video']),
             '-map', '[v]', *(['-map', '0:a:0', '-c:a', 'aac'] if info_a['audio'] else []), '-c:v', 'libx264', '-preset', 'veryfast',
             '-crf', '23', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-y', video])
        result['video'] = {'path': video, 'mode': mode}
    return result


def summary_lines(result: dict) -> list[str]:
    a, b = result['a'], result['b']
    lines = [f'A {a["path"]}  {a["fps"]}fps {a["frames"]}f {a["duration"]}s   B {b["path"]}  {b["fps"]}fps {b["frames"]}f {b["duration"]}s']
    lines += [f'{p["path"]}  t={p["times"]}  {p["bytes"] // 1024} KB' for p in result['pages']]
    if 'cuts' in result:
        c = result['cuts']
        lines.append(f'cuts A {len(c["a"])}, B {len(c["b"])}: {c["matched"]} matched within {CUT_TOLERANCE}s'
                     + (f', mean |offset| {c["mean_abs_offset"]}s' if c['mean_abs_offset'] is not None else ''))
        lines += [f'  A {p["a"]}s -> B {p["b"]}s ({p["offset"]:+}s)' if p['b'] is not None else f'  A {p["a"]}s -> no cut in B'
                  for p in c['pairs']]
        if c['only_b']:
            lines.append(f'  only in B: {c["only_b"]}')
    if 'shots' in result:
        sh = result['shots']
        lines.append(f'shots A {sh["a"]}, B {sh["b"]}; {sh["transitions_differ"]} begin differently'
                     + (f'; {len(sh["only_b"])} B shot(s) match no A shot' if sh['only_b'] else ''))

        def camera(m: dict) -> str:
            return f'zoom {m["zoom_per_s"] * 100:+.1f}%/s pan {m["pan_widths_per_s"] * 100:.1f}%w/s @{m["pan_dir_deg"]:.0f}° rigid {m["rigid_fit"]:.2f}'
        for p in sh['pairs']:
            a, b = p['a'], p['b']
            head = f'  A{a["shot"]} {a["start"]}-{a["end"]}s {_begins(a)}'
            if not b:
                lines.append(f'{head} -> no B shot')
                continue
            lines.append(f'{head} -> B{b["shot"]} {b["start"]}-{b["end"]}s {_begins(b)} ({p["covered"]:.0%} covered)'
                         f'\n      A {camera(p["motion"]["a"])}\n      B {camera(p["motion"]["b"])}')
    if 'video' in result:
        lines.append(VIDEO_MODES[result['video']['mode']].format(result['video']['path']))
    return lines
