"""rn text: words on screen and when they are there (tesseract OCR on sampled frames). For reading a reference's titles
and captions, keeping new text clear of old, or checking a render's text. Measurement only; OCR misreads stylised type,
so treat the boxes as where to look and the strings as a draft.
"""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from .ff import RnError, probe, source_fps
from .frames import extract

PREFERRED = ('eng', 'chi_sim', 'chi_tra', 'jpn', 'kor')


def languages() -> list[str]:
    if not shutil.which('tesseract'):
        raise RnError('tesseract not found: brew install tesseract (ffmpeg-full brings it), apt install tesseract-ocr '
                      '(+ tesseract-ocr-chi-sim for Chinese)')
    listed = subprocess.run(['tesseract', '--list-langs'], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout.split()
    return [lang for lang in listed if not lang.startswith('List') and lang != 'osd']


def _join(words: list[str]) -> str:
    """Tesseract splits CJK into single characters: no space between two CJK characters, a space elsewhere."""
    cjk = lambda c: ord(c) >= 0x2E80  # noqa: E731
    out = words[0]
    for word in words[1:]:
        out += ('' if cjk(out[-1]) and cjk(word[0]) else ' ') + word
    return out


def _read(png: Path, lang: str, min_conf: float) -> list[dict]:
    """Words grouped into lines (tesseract's block/paragraph/line numbers), each with its bounding box."""
    tsv = subprocess.run(['tesseract', str(png), '-', '-l', lang, '--psm', '11', 'tsv'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if tsv.returncode != 0:
        raise RnError(f'tesseract failed: {tsv.stderr.strip().splitlines()[-1:]}')
    lines: dict[tuple, dict] = {}
    for row in csv.DictReader(io.StringIO(tsv.stdout), delimiter='\t', quoting=csv.QUOTE_NONE):
        word = (row.get('text') or '').strip()
        if not word or float(row['conf']) < min_conf:
            continue
        key = (row['block_num'], row['par_num'], row['line_num'])
        x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        line = lines.setdefault(key, {'words': [], 'conf': [], 'box': [x, y, x + w, y + h]})
        line['words'].append(word)
        line['conf'].append(float(row['conf']))
        box = line['box']
        line['box'] = [min(box[0], x), min(box[1], y), max(box[2], x + w), max(box[3], y + h)]
    found = []
    for line in lines.values():
        x0, y0, x1, y1 = line['box']
        found.append({'text': _join(line['words']), 'box': [x0, y0, x1 - x0, y1 - y0], 'conf': round(sum(line['conf']) / len(line['conf']), 1)})
    return sorted(found, key=lambda item: (item['box'][1], item['box'][0]))


def text_command(path: str, *, every: int | None, lang: str | None, min_conf: float) -> dict:
    available = languages()
    lang = lang or '+'.join(l for l in PREFERRED if l in available) or 'eng'
    missing = [l for l in lang.split('+') if l not in available]
    if missing:
        raise RnError(f'tesseract has no {missing} data (installed: {available})')
    info = probe(path)
    video = info['video']
    if not video:
        raise RnError(f'{path}: no picture')
    still = video['still']
    fps = None if still else source_fps(info)
    total = 1 if still else video['frames']
    step = every or (1 if still else max(1, round(float(fps) / 2)))  # two reads a second by default
    samples = []
    with tempfile.TemporaryDirectory() as tmp:
        frames = [(0, Path(path))] if still else extract(path, list(range(0, total, step)), Path(tmp))
        for index, png in frames:
            samples.append({'f': index, 't': None if still else round(index / float(fps), 3), 'lines': _read(png, lang, min_conf)})
    # Spans: the same line read in consecutive samples is one appearance.
    spans: list[dict] = []
    open_spans: dict[str, dict] = {}
    for sample in samples:
        seen = set()
        for line in sample['lines']:
            seen.add(line['text'])
            span = open_spans.get(line['text'])
            if span and span['last_frame'] >= sample['f'] - step:
                span['last_frame'] = sample['f']
            else:
                span = {'text': line['text'], 'first_frame': sample['f'], 'last_frame': sample['f'], 'box': line['box']}
                open_spans[line['text']] = span
                spans.append(span)
        for text in [t for t in open_spans if t not in seen]:
            del open_spans[text]
    for span in spans:
        if fps:
            span['start'] = round(span['first_frame'] / float(fps), 3)
            span['end'] = round(min(total, span['last_frame'] + step) / float(fps), 3)  # up to the next sample not read
    return {'path': path, 'lang': lang, 'every': step, 'fps': str(fps) if fps else None, 'samples': samples, 'spans': spans}


def summary_lines(result: dict) -> list[str]:
    lines = [f'{result["path"]}: {len(result["samples"])} frames read (every {result["every"]}), {len(result["spans"])} text spans, '
             f'lang {result["lang"]}']
    for span in result['spans'][:40]:
        when = f'{span["start"]:.2f}-{span["end"]:.2f}s' if 'start' in span else 'still'
        x, y, w, h = span['box']
        lines.append(f'  {when}  "{span["text"]}"  at {x},{y} {w}x{h}')
    if len(result['spans']) > 40:
        lines.append(f'  ... {len(result["spans"]) - 40} more in --json')
    return lines
