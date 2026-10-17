"""rn captions: mechanical caption chunking, script alignment, safe-area / face-aware placement.

Writes one plain .ass (single readable style), an .srt, and a JSON of events with word timing so the agent can render
its own styled captions. Never imposes a style. Contract: spec/mcp/03-media-kit.md §3.1.
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from .ff import RnError

# Approximate platform UI clearance as fractions of the frame, NOT exact platform specs: the apps move their overlays
# between releases. top/bottom = reserved by caption/username/progress bars, left/right = reserved by action buttons.
SAFE_MARGINS = {
    'tiktok': {'top': 0.13, 'bottom': 0.22, 'left': 0.06, 'right': 0.16},
    'reels': {'top': 0.12, 'bottom': 0.20, 'left': 0.06, 'right': 0.12},
    'youtube': {'top': 0.05, 'bottom': 0.08, 'left': 0.05, 'right': 0.05},  # 16:9 title-safe + player controls
    'none': {'top': 0.02, 'bottom': 0.02, 'left': 0.02, 'right': 0.02},
}
BASE_CHARS = {'cjk': 14, 'latin': 32}  # per line at 1080 px width
LATIN_EM = 0.55  # average advance of a Latin glyph in em
OPENERS = set('「『（([“‘《〈【')
SENTENCE_END = set('。！？.!?…‼⁇')
SOFT_BREAK = set('，、；：,;:—')
CLOSERS = set('」』）)]”’》〉】"\'')
FONTS = {'zh': 'Noto Sans CJK SC', 'yue': 'Noto Sans CJK SC', 'ja': 'Noto Sans CJK JP', 'ko': 'Noto Sans CJK KR'}
PAUSE_BREAK = 0.5  # seconds of silence that ends a caption


def _norm(text: str) -> str:
    return ''.join(c for c in text.lower() if c.isalnum())


def _wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in 'WF'


def _width(text: str, cjk: bool) -> float:
    """Line-limit measure: a wide char is 1; in CJK mode a Latin char is half."""
    return sum(1 if _wide(c) else (0.5 if cjk else 1) for c in text)


def _em(text: str) -> float:
    return sum(1.0 if _wide(c) else LATIN_EM for c in text)


def _join(texts: list[str], cjk: bool) -> str:
    out = ''
    for text in texts:
        glue = ' ' if out and (not cjk or (out[-1].isascii() and out[-1].isalnum() and text[0].isascii() and text[0].isalnum())) else ''
        out += glue + text
    return out


def _is_cjk_language(language: str, words: list[dict]) -> bool:
    if language:
        return language.split('-')[0].lower() in ('zh', 'ja', 'yue')
    sample = ''.join(w['word'] for w in words)
    return bool(sample) and sum(_wide(c) for c in sample) / len(sample) > 0.3


def _attach_punctuation(tokens: list[dict]) -> list[dict]:
    """Closing punctuation joins the previous token, opening brackets join the next: no unit starts with 、。」 or ends with 「."""
    units: list[dict] = []
    prefix = ''
    for token in tokens:
        if not any(c.isalnum() for c in token['text']):
            if all(c in OPENERS for c in token['text']) or not units:
                prefix += token['text']
            else:
                units[-1]['text'] += token['text']
            continue
        units.append({**token, 'text': prefix + token['text']})
        prefix = ''
    if prefix and units:
        units[-1]['text'] += prefix
    return units


def _transcript_units(words: list[dict], cjk: bool) -> list[dict]:
    raw: list[dict] = []
    if cjk:
        for word in words:
            pieces = re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\S", word['word'])
            lexical = sum(any(c.isalnum() for c in p) for p in pieces) or 1
            span = (word['end'] - word['start']) / lexical
            cursor = word['start']
            for piece in pieces:
                if any(c.isalnum() for c in piece):
                    raw.append({'text': piece, 'start': cursor, 'end': cursor + span})
                    cursor += span
                else:
                    raw.append({'text': piece, 'start': cursor, 'end': cursor})
    else:
        spaced = any(w['word'].startswith(' ') for w in words[1:])
        for word in words:
            text = word['word'].strip()
            if not text:
                continue
            if spaced and raw and not word['word'].startswith(' '):  # Whisper sub-word piece: glue to the previous word
                raw[-1]['text'] += text
                raw[-1]['end'] = word['end']
            else:
                raw.append({'text': text, 'start': word['start'], 'end': word['end']})
    return _attach_punctuation(raw)


def _script_units(script: str, cjk: bool) -> list[dict]:
    pieces = re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\S", script) if cjk else script.split()
    return _attach_punctuation([{'text': p} for p in pieces])


def _align_script(transcript: list[dict], script: list[dict]) -> tuple[list[dict], dict]:
    """Script text with transcript timing; unmatched script runs get timing interpolated across the transcript gap."""
    matcher = SequenceMatcher(None, [_norm(u['text']) for u in transcript], [_norm(u['text']) for u in script], autojunk=False)
    aligned: list[dict] = [{}] * len(script)
    matched = 0
    unmatched: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            matched += j2 - j1
            for offset in range(j2 - j1):
                aligned[j1 + offset] = {'text': script[j1 + offset]['text'], 'start': transcript[i1 + offset]['start'], 'end': transcript[i1 + offset]['end']}
            continue
        if i2 > i1:
            span_start, span_end = transcript[i1]['start'], transcript[i2 - 1]['end']
        else:  # script-only run: use the silence between the neighbouring transcript units
            span_start = transcript[i1 - 1]['end'] if i1 > 0 else transcript[0]['start']
            span_end = transcript[i1]['start'] if i1 < len(transcript) else transcript[-1]['end']
        span_end = max(span_start, span_end)
        step = (span_end - span_start) / max(j2 - j1, 1)
        for offset in range(j2 - j1):
            aligned[j1 + offset] = {'text': script[j1 + offset]['text'], 'start': span_start + step * offset, 'end': span_start + step * (offset + 1)}
        unmatched.append({'script': _join([u['text'] for u in script[j1:j2]], False), 'transcript': _join([u['text'] for u in transcript[i1:i2]], False),
                          'start': round(span_start, 3), 'end': round(span_end, 3)})
    stats = {'match_ratio': round(matched / len(script), 3) if script else 0.0, 'script_units': len(script), 'transcript_units': len(transcript),
             'matched_units': matched, 'unmatched_spans': unmatched[:20], 'unmatched_span_count': len(unmatched)}
    return aligned, stats


def _layout(units: list[dict], max_width: float, cjk: bool) -> list[list[dict]]:
    """Greedy fill to find the line count, then the narrowest width that keeps it: lines come out balanced
    ("and a case that fits / in your pocket.", not "and a case that fits in your / pocket.")."""
    lines = _greedy(units, max_width, cjk)
    if len(lines) < 2:
        return lines
    width = max(_width(_join([u['text'] for u in line], cjk), cjk) for line in lines)
    while width > 1:
        tighter = _greedy(units, width - 0.5, cjk)
        if len(tighter) > len(lines):
            break
        lines, width = tighter, width - 0.5
    return lines


def _greedy(units: list[dict], max_width: float, cjk: bool) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for unit in units:
        if lines and _width(_join([u['text'] for u in lines[-1] + [unit]], cjk), cjk) <= max_width:
            lines[-1].append(unit)
        else:
            lines.append([unit])
    return lines


def _tail(text: str) -> str:
    stripped = text.rstrip(''.join(CLOSERS))
    return stripped[-1] if stripped else ''


def _chunk(units: list[dict], *, max_chars: int, max_lines: int, min_duration: float, max_duration: float, cjk: bool) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for index, unit in enumerate(units):
        if current and (unit['end'] - current[0]['start'] > max_duration or len(_layout(current + [unit], max_chars, cjk)) > max_lines):
            chunks.append(current)
            current = []
        current.append(unit)
        duration = unit['end'] - current[0]['start']
        gap = units[index + 1]['start'] - unit['end'] if index + 1 < len(units) else 0
        fill = _width(_join([u['text'] for u in current], cjk), cjk) / (max_chars * max_lines)
        tail = _tail(unit['text'])
        if duration >= min_duration and (tail in SENTENCE_END or gap >= PAUSE_BREAK or (tail in SOFT_BREAK and fill >= 0.5)):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _overlaps(box: tuple[float, float, float, float], faces: list[tuple[float, float, float, float]], pad: float) -> bool:
    x, y, w, h = box
    return any(x < fx + fw + pad and fx - pad < x + w and y < fy + fh + pad and fy - pad < y + h for fx, fy, fw, fh in faces)


def _faces_in_span(frames: list[dict], start: float, end: float, scale: tuple[float, float]) -> list[tuple[float, float, float, float]]:
    before = [f for f in frames if f['t'] < start]
    chosen = [f for f in frames if start <= f['t'] <= end] + before[-1:]  # sparse sampling: the last frame before the span still holds
    return [(f_['box'][0] * scale[0], f_['box'][1] * scale[1], f_['box'][2] * scale[0], f_['box'][3] * scale[1]) for f in chosen for f_ in f.get('faces', [])]


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def captions_command(words_json: str, *, script: str | None = None, faces_json: str | None = None, out: str, width: int, height: int,
                     max_chars: int | None = None, max_lines: int = 2, min_duration: float = 0.6, max_duration: float = 4.0,
                     safe: str = 'auto', language: str | None = None) -> dict:
    try:
        import pysubs2
    except ImportError as error:
        raise RnError('captions needs pysubs2 (pip install pysubs2)') from error
    if safe not in ('auto', *SAFE_MARGINS):
        raise RnError(f'--safe must be one of auto, {", ".join(SAFE_MARGINS)}')
    if width <= 0 or height <= 0 or max_lines < 1 or min_duration > max_duration:
        raise RnError('captions: width/height must be positive, max_lines >= 1, min_duration <= max_duration')
    try:
        listen = json.loads(Path(words_json).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise RnError(f'cannot read words json {words_json}: {error}') from error
    words = [w for w in listen.get('words', []) if str(w.get('word', '')).strip()]
    if not words:
        raise RnError(f'{words_json} has no words; run `rn listen --transcribe --json` first')
    language = language or listen.get('language') or ''
    cjk = _is_cjk_language(language, words)
    warnings: list[str] = []

    units = _transcript_units(words, cjk)
    alignment = None
    if script is not None:
        try:
            script_text = Path(script).read_text(encoding='utf-8-sig')
        except OSError as error:
            raise RnError(f'cannot read script {script}: {error}') from error
        script_units = _script_units(script_text, cjk)
        if not script_units:
            raise RnError(f'script {script} is empty')
        units, alignment = _align_script(units, script_units)
        if alignment['match_ratio'] < 0.5:
            warnings.append(f"script matches only {alignment['match_ratio']:.0%} of the transcript; check it is the same take and language")

    safe_name = safe if safe != 'auto' else ('tiktok' if width / height < 0.8 else 'youtube')
    margins = SAFE_MARGINS[safe_name]
    safe_left, safe_right = margins['left'] * width, width - margins['right'] * width
    safe_width = safe_right - safe_left
    max_chars = max_chars or max(4, round(BASE_CHARS['cjk' if cjk else 'latin'] * width / 1080))
    em_per_char = 1.0 if cjk else LATIN_EM
    font_size = round(min(height * (0.03 if height > width else 0.05), safe_width / (max_chars * em_per_char)))
    font_name = FONTS.get(language.split('-')[0].lower(), 'Noto Sans') if language else ('Noto Sans CJK SC' if cjk else 'Noto Sans')

    chunks = _chunk(units, max_chars=max_chars, max_lines=max_lines, min_duration=min_duration, max_duration=max_duration, cjk=cjk)
    for chunk in chunks:
        for unit in chunk:
            if _width(unit['text'], cjk) > max_chars:
                warnings.append(f"unit '{unit['text']}' at {unit['start']:.2f}s is wider than max_chars={max_chars}; kept whole")

    frames: list[dict] = []
    scale = (1.0, 1.0)
    if faces_json:
        try:
            faces = json.loads(Path(faces_json).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as error:
            raise RnError(f'cannot read faces json {faces_json}: {error}') from error
        frames = faces.get('frames', [])
        meta = faces.get('meta', {})
        scale = (width / meta.get('width', width), height / meta.get('height', height))

    events: list[dict] = []
    moved: list[dict] = []
    line_height = font_size * 1.2
    for index, chunk in enumerate(chunks):
        lines = [_join([u['text'] for u in line], cjk) for line in _layout(chunk, max_chars, cjk)]
        box_w = min(safe_width, max(_em(line) for line in lines) * font_size)
        box_h = len(lines) * line_height
        bands = {'bottom': height - margins['bottom'] * height - box_h, 'top': margins['top'] * height, 'middle': (height - box_h) / 2}
        box_x = (width - box_w) / 2
        span_faces = _faces_in_span(frames, chunk[0]['start'], chunk[-1]['end'], scale) if frames else []
        band = 'bottom'
        if span_faces and _overlaps((box_x, bands['bottom'], box_w, box_h), span_faces, 0.01 * height):
            free = [b for b in ('top', 'middle') if not _overlaps((box_x, bands[b], box_w, box_h), span_faces, 0.01 * height)]
            if free:
                band = free[0]
                moved.append({'event': index, 'start': round(chunk[0]['start'], 3), 'end': round(chunk[-1]['end'], 3), 'from': 'bottom', 'to': band,
                              'reason': 'face box overlaps the bottom caption band'})
            else:
                warnings.append(f'event {index} ({chunk[0]["start"]:.2f}s): faces overlap every caption band; left at bottom')
        events.append({'start': chunk[0]['start'], 'end': chunk[-1]['end'], 'lines': lines, 'text': _join([u['text'] for u in chunk], cjk),
                       'words': [{'text': u['text'], 'start': round(u['start'], 3), 'end': round(u['end'], 3)} for u in chunk],
                       'position': {'band': band, 'x': round(box_x), 'y': round(bands[band]), 'w': round(box_w), 'h': round(box_h),
                                    'anchor': {'bottom': 'bottom-center', 'top': 'top-center', 'middle': 'middle-center'}[band]},
                       'moved_for_face': band != 'bottom'})
    # Hold short captions up to min_duration, never into the next caption.
    for index, event in enumerate(events):
        next_start = events[index + 1]['start'] if index + 1 < len(events) else float('inf')
        event['end'] = round(min(max(event['end'], event['start'] + min_duration), next_start), 3)
        event['start'] = round(event['start'], 3)
        if event['end'] - event['start'] < min_duration - 1e-6:
            warnings.append(f"event {index} at {event['start']:.2f}s is {event['end'] - event['start']:.2f}s (< min_duration): the next caption follows too closely")

    ass_path = Path(out)
    srt_path, json_path = ass_path.with_suffix('.srt'), ass_path.with_suffix('.json')
    ass_path.parent.mkdir(parents=True, exist_ok=True)

    subs = pysubs2.SSAFile()
    subs.info.update({'PlayResX': str(width), 'PlayResY': str(height), 'WrapStyle': '2'})  # WrapStyle 2: only our own line breaks
    subs.styles = {'Default': pysubs2.SSAStyle(
        fontname=font_name, fontsize=font_size, bold=True, primarycolor=pysubs2.Color(255, 255, 255, 0), outlinecolor=pysubs2.Color(0, 0, 0, 0),
        backcolor=pysubs2.Color(0, 0, 0, 128), outline=max(2, round(font_size * 0.07)), shadow=1, alignment=pysubs2.Alignment.BOTTOM_CENTER,
        marginl=round(safe_left), marginr=round(width - safe_right), marginv=round(margins['bottom'] * height))}
    for event in events:
        override = ''
        if event['position']['band'] != 'bottom':
            position = event['position']
            an, y = (8, position['y']) if position['band'] == 'top' else (5, position['y'] + position['h'] / 2)
            override = f"{{\\an{an}\\pos({round(width / 2)},{round(y)})}}"
        subs.append(pysubs2.SSAEvent(start=round(event['start'] * 1000), end=round(event['end'] * 1000), style='Default', text=override + '\\N'.join(event['lines'])))
    subs.save(str(ass_path), encoding='utf-8')

    srt_path.write_text(''.join(f"{i}\n{_srt_time(e['start'])} --> {_srt_time(e['end'])}\n" + '\n'.join(e['lines']) + '\n\n' for i, e in enumerate(events, 1)), encoding='utf-8')
    result = {'ass': str(ass_path), 'srt': str(srt_path), 'json': str(json_path), 'event_count': len(events), 'language': language or None,
              'script_alignment': alignment, 'moved_for_faces': moved, 'warnings': warnings, 'safe': safe_name,
              'font': font_name, 'font_size': font_size, 'max_chars': max_chars, 'max_lines': max_lines}
    json_path.write_text(json.dumps({'meta': {k: result[k] for k in ('language', 'safe', 'font', 'font_size', 'max_chars', 'max_lines')} | {'width': width, 'height': height},
                                     'events': events}, ensure_ascii=False, indent=1), encoding='utf-8')
    return result


def summary_lines(result: dict) -> list[str]:
    lines = [f"captions: {result['event_count']} events, language {result['language'] or 'auto'}, safe={result['safe']}, "
             f"{result['font']} {result['font_size']}px, max {result['max_chars']} chars x {result['max_lines']} lines",
             f"  ass {result['ass']}  srt {result['srt']}  json {result['json']}"]
    if result['script_alignment']:
        stats = result['script_alignment']
        lines.append(f"  script: {stats['match_ratio']:.0%} of {stats['script_units']} units matched Whisper; {stats['unmatched_span_count']} unmatched spans")
        lines += [f"    {s['start']:.2f}-{s['end']:.2f}s script '{s['script']}' vs heard '{s['transcript']}'" for s in stats['unmatched_spans'][:5]]
    lines += [f"  moved event {m['event']} ({m['start']:.2f}-{m['end']:.2f}s) {m['from']} -> {m['to']}: {m['reason']}" for m in result['moved_for_faces']]
    lines += [f'  warning: {w}' for w in result['warnings']]
    return lines
