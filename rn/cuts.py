"""rn cuts: shot boundaries of a video, by decoded frame index. Measurement only: it reports where the picture
changes, never where to cut. Continuous camera moves and morphs score low; the suspect list is for a human-style look."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .ff import RnError, probe, run, source_fps

DEFAULT_THRESHOLD = 0.3
_FRAME = re.compile(r'^frame:(\d+)')
_SCORE = re.compile(r'lavfi\.scene_score=([0-9.]+)')


def scene_scores(path: str | Path) -> list[float]:
    """ffmpeg's scene-change score (0..1) for every decoded frame, in decode order (frame 0 scores 0)."""
    result = run(['ffmpeg', '-i', str(path), '-an', '-vf', "select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=-",
                  '-f', 'null', '-'])
    scores: list[float] = []
    for line in result.stdout.splitlines():
        if (match := _FRAME.match(line)) and int(match.group(1)) == len(scores):
            scores.append(0.0)
        elif (match := _SCORE.search(line)) and scores:
            scores[-1] = float(match.group(1))
    if not scores:
        raise RnError(f'{path}: no frames decoded')
    return scores


@lru_cache(maxsize=32)
def detect(path: str, threshold: float = DEFAULT_THRESHOLD, min_gap: float = 0.3) -> dict:
    info = probe(path)
    if not info['video'] or info['video']['still']:
        raise RnError(f'{path}: not a video')
    fps = source_fps(info)
    scores = scene_scores(path)
    gap = max(1, round(min_gap * fps))
    cuts, suspects = [], []
    for index, score in enumerate(scores):
        if index == 0:
            continue
        entry = {'frame': index, 'time': round(float(index / fps), 4), 'score': round(score, 3)}
        if score >= threshold:
            # A flash or a two-frame dissolve scores twice in a row; keep the stronger of close neighbours.
            if cuts and index - cuts[-1]['frame'] < gap:
                if score > cuts[-1]['score']:
                    cuts[-1] = entry
                continue
            cuts.append(entry)
        elif score >= threshold / 2:
            suspects.append(entry)
    edges = [0, *(c['frame'] for c in cuts), len(scores)]
    shots = [{'shot': i, 'start_frame': a, 'end_frame': b, 'start': round(float(a / fps), 4), 'end': round(float(b / fps), 4),
              'duration': round(float((b - a) / fps), 4)} for i, (a, b) in enumerate(zip(edges, edges[1:]))]
    return {'path': str(path), 'fps': str(fps), 'frames': len(scores), 'threshold': threshold, 'cuts': cuts,
            'suspects': suspects, 'shots': shots}


def shot_span(path: str | Path, shot: int) -> tuple[float, float]:
    """Seconds [start, end) of shot `shot` (0-based) with the default detector; used by EDL {"shot": n} positions."""
    shots = detect(str(Path(path).resolve()))['shots']
    if not 0 <= shot < len(shots):
        raise RnError(f'{path}: shot {shot} does not exist (rn cuts found {len(shots)} shots: 0..{len(shots) - 1})')
    return shots[shot]['start'], shots[shot]['end']


def cuts_command(path: str, *, threshold: float, min_gap: float, sheet: str | None, max_kb: int) -> dict:
    result = dict(detect(str(Path(path).resolve()), threshold, min_gap))
    result['path'] = path
    if sheet:
        from .frames import sheet_command
        # Both sides of every cut and suspect, so a look confirms or rejects each one.
        indices = sorted({i for c in result['cuts'] + result['suspects'] for i in (c['frame'] - 1, c['frame'])})
        if indices:
            result['sheet'] = sheet_command([path], out=sheet, every=None, rate=None, indices=indices, seconds=None, count=None,
                                            width=192, columns=6, max_kb=max_kb)['pages']
    return result
