"""rn ref: the measurable half of breaking down a reference edit, in one folder. Shot boundaries (with suspects to
check by eye), shot lengths, the audio facts from rn listen, and contact sheets covering every frame (or every Nth),
paged so each page can be viewed. What a shot does and why it works is for you to read off the sheets; nothing here
interprets: GUIDE.md, copied into the folder, is how to read it into breakdown.md. Not yet measured: on-screen text (OCR)."""
from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path

from .cuts import DEFAULT_THRESHOLD, cuts_command
from .ff import RnError, probe
from .frames import sheet_command


def ref_command(path: str, *, out: str | None, every: int, transcribe: bool, language: str | None, model: str,
                threshold: float = DEFAULT_THRESHOLD, width: int = 192) -> dict:
    info = probe(path, exact=True)
    if not info['video'] or info['video']['still']:
        raise RnError(f'{path}: not a video')
    folder = Path(out) if out else Path('ref') / Path(path).stem
    folder.mkdir(parents=True, exist_ok=True)
    cuts = cuts_command(path, threshold=threshold, min_gap=0.3, sheet=str(folder / 'cuts.jpg'), max_kb=125)
    frames = info['video']['frames']
    coverage = sheet_command([path], out=str(folder / 'frames.jpg'), every=every, rate=None, indices=None, seconds=None,
                             count=None, width=width, columns=6, max_kb=125)['pages']
    listen = None
    if info['audio']:
        from .listen import listen_command
        listen = listen_command(path, out=str(folder / 'listen.jpg'), transcribe=transcribe, language=language, model=model)
    shutil.copyfile(Path(__file__).with_name('ref_guide.md'), folder / 'GUIDE.md')
    result = {
        'path': path,
        'folder': str(folder),
        'probe': info,
        'shots': cuts['shots'],
        'cuts': cuts['cuts'],
        'suspects': cuts['suspects'],
        'cut_sheets': [p['path'] for p in cuts.get('sheet', [])],
        'coverage': {'frames': frames, 'every': every, 'sampled': sum(len(p['indices']) for p in coverage),
                     'pages': [p['path'] for p in coverage],
                     'page_frames': [[p['indices'][0], p['indices'][-1]] for p in coverage]},
        'listen': listen,
        'cut_sync': cut_sync(cuts['cuts'], listen['onsets'] if listen else [], Fraction(cuts['fps'])),
    }
    (folder / 'ref.json').write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    return result


def cut_sync(cuts: list[dict], onsets: list[float], fps: Fraction, tolerance: int = 2) -> dict | None:
    """How tightly the cuts follow the audio: each cut's offset in frames to the nearest onset (negative = the cut
    leads the sound) and the share within +-tolerance frames. A high share means the edit is cut on the sound."""
    if not cuts or not onsets:
        return None
    offsets = [round(float((cut['time'] - min(onsets, key=lambda t: abs(t - cut['time']))) * fps)) for cut in cuts]
    return {'share': round(sum(abs(o) <= tolerance for o in offsets) / len(offsets), 2), 'tolerance_frames': tolerance,
            'offsets_frames': offsets}


def summary_lines(result: dict) -> list[str]:
    shots = result['shots']
    lengths = ', '.join(f'{s["duration"]:.2f}' for s in shots[:12]) + (' ...' if len(shots) > 12 else '')
    coverage = result['coverage']
    lines = [f'{result["folder"]}/ref.json',
             f'{len(shots)} shots ({lengths} s), {len(result["suspects"])} suspect cuts to check: {", ".join(result["cut_sheets"]) or "-"}',
             f'frame coverage: {coverage["sampled"]} of {coverage["frames"]} frames (every {coverage["every"]}) on {len(coverage["pages"])} pages']
    if result['listen']:
        from .listen import summary_lines as listen_lines
        lines += listen_lines(result['listen'])
    if result['cut_sync']:
        lines.append(f'cuts on an onset (+-2 frames): {result["cut_sync"]["share"]:.0%}')
    lines.append(f'next: read {result["folder"]}/GUIDE.md and write {result["folder"]}/breakdown.md')
    return lines
