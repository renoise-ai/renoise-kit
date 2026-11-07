"""rn text: words on screen and when they are there (tesseract OCR on sampled frames). For reading a reference's titles
and captions, keeping new text clear of old, or checking a render's text. Measurement only; OCR misreads stylised type,
so treat the boxes as where to look and the strings as a draft. Each span also has the colours and size of its type,
measured on the frame where it was first read, so an overlay can be rebuilt without guessing.
"""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

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


def _hex(colour) -> str:
    return '#' + ''.join(f'{int(round(float(c))):02x}' for c in np.clip(colour, 0, 255))


def _ycc(colour) -> tuple[float, float, float]:
    r, g, b = (float(c) for c in colour)
    return (0.299 * r + 0.587 * g + 0.114 * b, 128 - 0.168736 * r - 0.331264 * g + 0.5 * b,
            128 + 0.5 * r - 0.418688 * g - 0.081312 * b)


def _mix_of(colour, a, b, *, luma_slack: float = 16, chroma_slack: float = 24) -> bool:
    """Whether colour can be a mix of a and b as video makes one: luma and chroma mixed separately (4:2:0 keeps luma
    sharp and blurs chroma), so anti-aliased and chroma-smeared edges are mixes and a real outline is not."""
    y, cb, cr = _ycc(colour)
    ya, cba, cra = _ycc(a)
    yb, cbb, crb = _ycc(b)
    if not min(ya, yb) - luma_slack <= y <= max(ya, yb) + luma_slack:
        return False
    along, off = np.array([cbb - cba, crb - cra]), np.array([cb - cba, cr - cra])
    t = float(np.clip(off @ along / max(float(along @ along), 1e-9), 0, 1))
    return float(np.hypot(*(off - t * along))) <= chroma_slack


def _touches(mask: np.ndarray) -> np.ndarray:
    """Pixels with a 4-neighbour in mask."""
    near = np.zeros_like(mask)
    near[1:] |= mask[:-1]
    near[:-1] |= mask[1:]
    near[:, 1:] |= mask[:, :-1]
    near[:, :-1] |= mask[:, 1:]
    return near


def _reach(free: np.ndarray) -> np.ndarray:
    """Pixels of free connected (4-neighbour) to the crop's edge: the background around the type, without the counters
    of letters or a seam between fill and outline that happens to have the background's colour."""
    seen = np.zeros_like(free)
    seen[[0, -1]] = free[[0, -1]]
    seen[:, [0, -1]] |= free[:, [0, -1]]
    while True:
        grown = (seen | _touches(seen)) & free
        if np.array_equal(grown, seen):
            return seen
        seen = grown


def _style(rgb_crop) -> dict:
    """Colours of one line of type in an RGB crop (H x W x 3) of its box with a margin of background around it.
    Background: the crop's border pixels; ink: pixels clearly away from that colour, split in two by colour (k-medians).
    When one group is a mix of the other and the background (an anti-aliased or chroma-smeared edge), the other is the
    fill and there is no outline. Otherwise the fill is the group that touches the surrounding background less, and the
    other group is its outline when it encloses the fill (a drop shadow does not) and differs clearly from fill and
    background. Colours are medians, #rrggbb; background is the median of the non-ink pixels."""
    pixels = np.asarray(rgb_crop, dtype=np.float64)[..., :3]
    if pixels.ndim != 3 or min(pixels.shape[:2]) < 3:
        return {'color': None, 'background': None, 'outline': None}
    border = np.concatenate([pixels[0], pixels[-1], pixels[1:-1, 0], pixels[1:-1, -1]])
    around = np.median(border, axis=0)
    distance = np.linalg.norm(pixels - around, axis=2)
    spread = np.quantile(np.linalg.norm(border - around, axis=1), 0.9)  # how much the background itself varies
    ink = distance > max(24.0, min(1.5 * spread, 0.5 * np.quantile(distance, 0.99)))
    background = np.median(pixels[~ink], axis=0) if (~ink).any() else around
    if ink.sum() < 8:
        return {'color': None, 'background': _hex(background), 'outline': None}
    colours = pixels[ink]
    # Start from the two ends of the ink's colours: the inkiest quarter, and the quarter furthest from that.
    far = np.median(colours[distance[ink] >= np.quantile(distance[ink], 0.75)], axis=0)
    away = np.linalg.norm(colours - far, axis=1)
    centres = np.array([far, np.median(colours[away >= np.quantile(away, 0.75)], axis=0)])
    for _ in range(12):
        gaps = np.linalg.norm(colours[:, None, :] - centres[None], axis=2)
        labels = np.argmin(gaps, axis=1)
        if labels.min() == labels.max():
            return {'color': _hex(np.median(colours, axis=0)), 'background': _hex(background), 'outline': None}
        moved = np.array([np.median(colours[labels == k], axis=0) for k in (0, 1)])
        if np.allclose(moved, centres):
            break
        centres = moved
    # Of each group's pixels, the share touching the background around the type (or the crop's edge). Pixels nearer the
    # background's colour than either group's (anti-aliasing, compression ringing) are left out.
    edge = _touches(_reach(~ink))
    edge[[0, -1]] = edge[:, [0, -1]] = True
    clear = np.linalg.norm(colours - background, axis=1) >= gaps.min(axis=1)
    bare = [float((edge[ink] & clear & (labels == k)).sum() / max(1, (clear & (labels == k)).sum())) for k in (0, 1)]
    mix = [_mix_of(centres[k], centres[1 - k], background) for k in (0, 1)]
    if mix[0] != mix[1]:  # one group is the edge between the other and the background: no outline
        fill = mix.index(False)
    elif bare[0] != bare[1]:  # an outline lies between the fill and the background
        fill = int(np.argmin(bare))
    else:
        fill = int(np.argmax([np.linalg.norm(c - background) for c in centres]))
    rim = 1 - fill
    outline = (not mix[rim] and bare[fill] < 0.7 * bare[rim] and float((labels == rim).mean()) >= 0.1
               and np.linalg.norm(centres[rim] - centres[fill]) >= 64 and np.linalg.norm(centres[rim] - background) >= 64)
    return {'color': _hex(centres[fill]), 'background': _hex(background), 'outline': _hex(centres[rim]) if outline else None}


def _span_style(picture: np.ndarray, box: list[int]) -> dict:
    """style for a line box [x, y, w, h] in a decoded RGB frame: colours measured on the box grown by 15% of its height
    (at least 3 px) so the border is background, plus its height in source pixels and as a share of the frame height."""
    x, y, w, h = box
    frame_h, frame_w = picture.shape[:2]
    pad = max(3, round(0.15 * h))
    crop = picture[max(0, y - pad):min(frame_h, y + h + pad), max(0, x - pad):min(frame_w, x + w + pad)]
    return {**_style(crop), 'height_px': h, 'height_share': round(h / frame_h, 4)}


def text_command(path: str, *, every: int | None, lang: str | None, min_conf: float) -> dict:
    from PIL import Image

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
    # Spans: the same line read in consecutive samples is one appearance. Built while the frames are on disk: a new
    # span's style is measured on the frame where it was first read.
    spans: list[dict] = []
    open_spans: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        frames = [(0, Path(path))] if still else extract(path, list(range(0, total, step)), Path(tmp))
        for index, png in frames:
            sample = {'f': index, 't': None if still else round(index / float(fps), 3), 'lines': _read(png, lang, min_conf)}
            samples.append(sample)
            picture = None
            seen = set()
            for line in sample['lines']:
                seen.add(line['text'])
                span = open_spans.get(line['text'])
                if span and span['last_frame'] >= sample['f'] - step:
                    span['last_frame'] = sample['f']
                else:
                    if picture is None:
                        with Image.open(png) as image:
                            picture = np.asarray(image.convert('RGB'))
                    span = {'text': line['text'], 'first_frame': sample['f'], 'last_frame': sample['f'], 'box': line['box'],
                            'style': _span_style(picture, line['box'])}
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
        style = span.get('style') or {}
        look = f'  {style["color"]} on {style["background"]}' if style.get('color') else ''
        look += f' outline {style["outline"]}' if style.get('outline') else ''
        lines.append(f'  {when}  "{span["text"]}"  at {x},{y} {w}x{h}{look}')
    if len(result['spans']) > 40:
        lines.append(f'  ... {len(result["spans"]) - 40} more in --json')
    return lines
