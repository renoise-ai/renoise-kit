"""rn music fit: a music bed exactly as long as the edit that ends cleanly instead of being cut off mid-bar.

The bed starts on a downbeat and its final fade begins on a bar or phrase boundary (phrase = 4 bars counted from the
start), so the music sounds like it was meant to stop there. Music shorter than the edit is looped on phrase boundaries
with a short crossfade. Beats and downbeats come from rn listen (the file is analysed, or --listen gives a result).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .ff import RnError, probe

RATE = 48000
PRE_ROLL = 0.05  # cut points sit this much before the tracked beat so the attack is kept (trackers land just after it)
LOOP_FADE = 0.08  # crossfade at a loop point, seconds
MIN_TAIL = 0.25  # shortest final fade, seconds
PHRASE_BARS = 4
CODECS = {'.wav': ['-c:a', 'pcm_s16le'], '.m4a': ['-c:a', 'aac', '-b:a', '192k'], '.mp3': ['-c:a', 'libmp3lame', '-q:a', '2'],
          '.flac': ['-c:a', 'flac']}


def _grid(music: str, listen: str | None) -> dict:
    if listen:
        try:
            data = json.loads(Path(listen).read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError) as error:
            raise RnError(f'--listen {listen}: {error}') from None
        beats = data.get('beats') if isinstance(data, dict) else None
        if not isinstance(beats, dict) or 'times' not in beats:
            raise RnError(f'--listen {listen}: no beats (write it with rn listen MUSIC --json {Path(listen).name})')
        if beats.get('times') and 'downbeats' not in beats:  # written by an rn older than 0.7: assume the first beat is a one
            beats = {**beats, 'downbeats': beats['times'][::4], 'downbeat_strength': None}
        return beats
    from .listen import beats_of
    return beats_of(music)


def _decode(path: str) -> 'np.ndarray':
    import numpy as np
    pcm = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-i', path, '-vn', '-f', 'f32le', '-ac', '2',
                          '-ar', str(RATE), '-'], capture_output=True)
    if pcm.returncode:
        raise RnError(f'{path}: cannot decode audio: {pcm.stderr.decode(errors="replace").strip()[-300:]}')
    return np.frombuffer(pcm.stdout, dtype=np.float32).reshape(-1, 2).copy()


def _write(samples, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    codec = CODECS.get(out.suffix.lower())
    if codec is None:
        raise RnError(f'-o {out.name}: .wav, .m4a, .mp3 or .flac')
    result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-f', 'f32le', '-ar', str(RATE), '-ac', '2',
                             '-i', '-', *codec, '-y', str(out)], input=samples.astype('<f4').tobytes(), capture_output=True)
    if result.returncode:
        raise RnError(f'writing {out}: {result.stderr.decode(errors="replace").strip()[-300:]}')


def _segments(start: float, length: float, total: float, loop: tuple[float, float] | None) -> list[tuple[float, float, float]]:
    """(source in, source out, bed time of the in) pieces covering `length` seconds of bed. Without a loop one piece;
    with loop (a, b) the source plays from start to b, then a..b again and again."""
    if loop is None:
        return [(start, start + length, 0.0)]
    a, b = loop
    pieces, at, src = [], 0.0, start
    while at < length:
        out = min(b, src + (length - at))
        pieces.append((src, out, at))
        at += out - src
        src = a
    return pieces


def _to_bed(t: float, pieces) -> list[float]:
    """Every bed time at which source time t plays."""
    return [at + t - src for src, out, at in pieces if src <= t < out]


def _boundaries(kind: str, beats: list[float], downbeats: list[float], start_bar: int) -> list[tuple[float, int | None]]:
    """(source time, bar number from the start bar or None) of the points a final fade may begin on."""
    if kind == 'beat':
        return [(t, None) for t in beats]
    bars = [(t, k - start_bar) for k, t in enumerate(downbeats) if k >= start_bar]
    return bars if kind == 'downbeat' else [(t, n) for t, n in bars if n % PHRASE_BARS == 0]


def _plan(total: float, length: float, beats: list[float], downbeats: list[float], *, start: float | None, end_on: str,
          tail: float, loop: bool | None) -> dict:
    """Chooses start, loop and the point where the fade begins. Cost: how far the fade's length is from `tail`, plus a
    small price for starting later in the song (the opening of a track is usually the intended entry)."""
    bar = (downbeats[1] - downbeats[0]) if len(downbeats) > 1 else 2.0
    if start is None:
        starts = [(max(0.0, d - PRE_ROLL), k) for k, d in enumerate(downbeats)] or [(0.0, 0)]
    else:
        if not 0 <= start < total:
            raise RnError(f'--start {start}: inside the music (0..{total:.3f}s)')
        later = [k for k, d in enumerate(downbeats) if d >= start - PRE_ROLL]
        starts = [(start, later[0] if later else len(downbeats))]
    # Loop only when no start leaves enough music; a loop starts at the first candidate and repeats its longest phrase run.
    fitting = [(s, k) for s, k in starts if total - s >= length]
    looped = bool(loop) or not fitting
    starts = starts[:1] if looped else fitting
    best = None
    for kind in dict.fromkeys([end_on, 'downbeat', 'beat']):  # fall back to finer boundaries when nothing fits
        for s, bar_index in starts:
            pieces_loop = None
            if looped:
                phrases = [d for k, d in enumerate(downbeats) if k > bar_index and (k - bar_index) % PHRASE_BARS == 0
                           and d - PRE_ROLL + LOOP_FADE <= total]
                if not phrases:
                    phrases = [d for k, d in enumerate(downbeats) if k > bar_index and d - PRE_ROLL + LOOP_FADE <= total]
                if not phrases:
                    continue
                a = max(0.0, downbeats[bar_index] - PRE_ROLL) if bar_index < len(downbeats) else s
                pieces_loop = (a, phrases[-1] - PRE_ROLL)
                if pieces_loop[1] - pieces_loop[0] < 2 * LOOP_FADE:
                    continue
            pieces = _segments(s, length, total, pieces_loop)
            for t, n in _boundaries(kind, beats, downbeats, bar_index):
                for at in _to_bed(t - PRE_ROLL, pieces):
                    fade = length - at
                    if fade < MIN_TAIL or at <= 0:
                        continue
                    cost = abs(fade - tail) + 0.15 * s / max(bar, 0.5) / PHRASE_BARS
                    if best is None or cost < best['cost']:
                        best = {'cost': cost, 'start': s, 'fade_at': at, 'fade': fade, 'source_at': t, 'bar': n,
                                'end_on': kind, 'loop': pieces_loop, 'pieces': pieces}
        if best and best['fade'] <= max(tail * 2, tail + bar):
            break
    return best


def fit_command(music: str, *, duration: float, out: str | None, listen: str | None = None, start: float | None = None,
                end_on: str = 'phrase', fade: float | None = None, loop: bool | None = None) -> dict:
    import numpy as np

    info = probe(music)
    if not info['audio']:
        raise RnError(f'{music}: no audio stream')
    if duration <= 0:
        raise RnError('--duration: seconds > 0')
    total = float(info['audio']['duration'] or info['duration'] or 0)
    grid = _grid(music, listen)
    beats, downbeats = [float(t) for t in grid.get('times') or []], [float(t) for t in grid.get('downbeats') or []]
    clear = bool(grid.get('clear')) and len(downbeats) >= 2
    bar = (downbeats[1] - downbeats[0]) if clear else None
    tail = fade if fade is not None else (min(3.0, max(1.0, bar)) if bar else 1.5)
    warnings = []
    plan = _plan(total, duration, beats, downbeats, start=start, end_on=end_on, tail=tail, loop=loop) if clear else None
    if plan is None:  # no pulse to follow: a plain cut with a fade (and a plain loop when needed)
        if beats and not clear:
            warnings.append('no clear pulse in the music: plain cut with a fade, not on a bar')
        s = start or 0.0
        looped = loop or (loop is None and total - s < duration)
        pieces = _segments(s, duration, total, (0.0, total) if looped else None)
        fade_len = min(tail, duration / 2)
        plan = {'start': s, 'fade_at': duration - fade_len, 'fade': fade_len, 'source_at': None, 'bar': None,
                'end_on': None, 'loop': (0.0, total) if looped else None, 'pieces': pieces}
    elif plan['end_on'] != end_on:
        warnings.append(f'no {end_on} boundary gives a fade near {tail:.2f}s; the fade starts on a {plan["end_on"]} instead')
    if fade is not None and abs(plan['fade'] - fade) > 0.5:
        warnings.append(f'the nearest boundary gives a {plan["fade"]:.2f}s fade, not {fade}s')

    pieces = plan['pieces']
    result = {'path': out, 'source': music, 'duration': round(duration, 3), 'music_duration': round(total, 3),
              'bpm': grid.get('bpm'), 'clear': clear, 'bars_strength': grid.get('downbeat_strength'),
              'start': round(plan['start'], 3), 'end_on': plan['end_on'],
              'fade_out': {'at': round(plan['fade_at'], 3), 'seconds': round(plan['fade'], 3),
                           'source_time': None if plan['source_at'] is None else round(plan['source_at'], 3),
                           'bar': plan['bar'],
                           'beat': None if plan['source_at'] is None or not beats else
                           min(range(len(beats)), key=lambda i: abs(beats[i] - plan['source_at']))},
              'loops': [{'at': round(at, 3), 'from': round(pieces[k - 1][1], 3), 'to': round(src, 3)}
                        for k, (src, _, at) in enumerate(pieces) if k],
              'warnings': warnings}
    if out is None:
        if result['loops']:
            raise RnError(f'{Path(music).name} ({total:.2f}s from {plan["start"]:.2f}s) is shorter than {duration}s: '
                          'it has to be looped, so give -o to render the bed')
        result['edl'] = {'src': music, 'role': 'music', 'in': result['start'], 'out': round(plan['start'] + duration, 3),
                         'fade_out': result['fade_out']['seconds']}
        result['verdict'] = _verdict(result)
        return result

    source = _decode(music)
    n = int(round(duration * RATE))
    bed = np.zeros((n, 2), dtype=np.float32)
    ramp = int(LOOP_FADE * RATE)
    for k, (src, src_out, at) in enumerate(pieces):
        a, b, o = int(round(src * RATE)), int(round(src_out * RATE)), int(round(at * RATE))
        chunk = source[a:b].copy()
        if k:  # equal-power crossfade into the loop: this piece fades in over the tail of the one before
            m = min(ramp, len(chunk))
            chunk[:m] *= np.sin(np.linspace(0, np.pi / 2, m))[:, None]
            tail_src = source[int(round(pieces[k - 1][1] * RATE)):int(round(pieces[k - 1][1] * RATE)) + m].copy()
            tail_src *= np.cos(np.linspace(0, np.pi / 2, len(tail_src)))[:, None]
            bed[o:o + len(tail_src)] += tail_src[:max(0, n - o)]
        bed[o:o + len(chunk)] += chunk[:max(0, n - o)]
    first = int(0.005 * RATE)
    if plan['start'] > 0:
        bed[:first] *= np.linspace(0, 1, first)[:, None]
    f0 = int(round(plan['fade_at'] * RATE))
    bed[f0:] *= np.cos(np.linspace(0, np.pi / 2, n - f0))[:, None] ** 2  # smooth to silence at the last sample
    _write(bed, Path(out))
    result['edl'] = {'src': out, 'role': 'music'}
    result['verdict'] = _verdict(result)
    return result


def _verdict(result: dict) -> str:
    if not result['clear']:
        return 'plain: no clear beat, ends on a fade'
    where = f'fade from a {result["end_on"]}' + (f' (bar {result["fade_out"]["bar"]})' if result['fade_out']['bar'] is not None else '')
    return f'clean: starts on a downbeat, {where}' + (f', {len(result["loops"])} loop(s)' if result['loops'] else '')


def summary_lines(result: dict) -> list[str]:
    fade = result['fade_out']
    lines = [f'{result["path"] or "(not rendered)"}  {result["duration"]}s from {result["source"]} '
             f'({result["music_duration"]}s, {result["bpm"] or "no"} bpm)',
             f'{result["verdict"]}',
             f'  starts at {result["start"]}s of the music; fade {fade["seconds"]}s from {fade["at"]}s'
             + (f' (music {fade["source_time"]}s)' if fade['source_time'] is not None else '')]
    lines += [f'  loop at {loop["at"]}s: music {loop["from"]}s -> {loop["to"]}s' for loop in result['loops']]
    lines.append(f'EDL audio item: {json.dumps(result["edl"], ensure_ascii=False)}')
    lines += [f'warning: {w}' for w in result['warnings']]
    return lines
