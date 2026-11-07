"""rn captions: mechanical caption chunking, script alignment, safe-area / face-aware placement.

Writes an .ass, an .srt, and a JSON of events with word timing. The .ass is one plain readable style by default;
--style picks an animated one (clean, pop, karaoke, highlight, box) built from the same per-word timing. Every style is
plain ASS override tags the agent can read and change. Contract: SPEC.md (rn captions).
"""
from __future__ import annotations

import json
import re
import sys
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
FONTS = {'zh': 'Noto Sans CJK SC', 'yue': 'Noto Sans CJK SC', 'ja': 'Noto Sans CJK JP', 'ko': 'Noto Sans CJK KR', '': 'Noto Sans'}
# Windows ships no Noto face and libass would fall back silently, so name the faces every Windows has.
WINDOWS_FONTS = {'zh': 'Microsoft YaHei', 'yue': 'Microsoft YaHei', 'ja': 'Yu Gothic', 'ko': 'Malgun Gothic', '': 'Segoe UI'}
PAUSE_BREAK = 0.5  # seconds of silence that ends a caption
STYLES = ('plain', 'clean', 'pop', 'karaoke', 'highlight', 'box')
DEFAULT_ACCENT = {'karaoke': '#ffe000', 'highlight': '#ffe000', 'box': '#000000'}


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


def _hex(text: str, what: str) -> tuple[int, int, int]:
    value = text.strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', value):
        raise RnError(f'{what} "{text}": expected #rrggbb')
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _ass_color(rgb: tuple[int, int, int]) -> str:
    return f'&H{rgb[2]:02X}{rgb[1]:02X}{rgb[0]:02X}&'


def _tagged(lines: list[list[dict]], cjk: bool, upper: bool, tag) -> str:
    """Lines of units as ASS text, `tag(unit_index)` put before each unit; glue as _join."""
    out_lines, k = [], 0
    for line in lines:
        plain = ''
        text = ''
        for unit in line:
            word = unit['text'].upper() if upper else unit['text']
            glue = ' ' if plain and (not cjk or (plain[-1].isascii() and plain[-1].isalnum() and word[0].isascii() and word[0].isalnum())) else ''
            plain += glue + word
            text += glue + tag(k) + word
            k += 1
        out_lines.append(text)
    return '\\N'.join(out_lines)


def _styled_text(style: str, event: dict, lines: list[list[dict]], cjk: bool, upper: bool, base: str, accent: str) -> str:
    """The animation of one event, as ASS override tags around its words."""
    start_ms = event['start'] * 1000
    words = [u for line in lines for u in line]
    if style == 'pop':  # the chunk scales in with a small overshoot
        return ('{\\fad(60,0)\\fscx70\\fscy70\\t(0,110,\\fscx108\\fscy108)\\t(110,190,\\fscx100\\fscy100)}'
                + _tagged(lines, cjk, upper, lambda k: ''))
    if style == 'karaoke':  # words turn the accent colour as they are spoken and stay (\k: secondary -> primary)
        lead = max(0, round((words[0]['start'] * 1000 - start_ms) / 10))
        cs = []
        for k, unit in enumerate(words):
            nxt = words[k + 1]['start'] if k + 1 < len(words) else unit['end']
            cs.append(max(1, round((nxt - unit['start']) * 100)))
        return ('{\\k%d}' % lead if lead else '') + _tagged(lines, cjk, upper, lambda k: '{\\k%d}' % cs[k])
    if style == 'highlight':  # only the word being spoken is in the accent colour

        def tag(k: int) -> str:
            unit = words[k]
            on = max(0, round(unit['start'] * 1000 - start_ms))
            off = max(on + 1, round((words[k + 1]['start'] if k + 1 < len(words) else unit['end']) * 1000 - start_ms))
            return f'{{\\1c{base}\\t({on},{on + 1},\\1c{accent})\\t({off},{off + 1},\\1c{base})}}'
        return _tagged(lines, cjk, upper, tag)
    return _tagged(lines, cjk, upper, lambda k: '')


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def captions_command(words_json: str, *, script: str | None = None, faces_json: str | None = None, out: str, width: int, height: int,
                     max_chars: int | None = None, max_lines: int = 2, min_duration: float = 0.6, max_duration: float = 4.0,
                     safe: str = 'auto', language: str | None = None, style: str = 'plain', font: str | None = None,
                     color: str | None = None, accent: str | None = None, position: str = 'bottom', upper: bool = False) -> dict:
    try:
        import pysubs2
    except ImportError as error:
        raise RnError('captions needs pysubs2 (pip install pysubs2)') from error
    if safe not in ('auto', *SAFE_MARGINS):
        raise RnError(f'--safe must be one of auto, {", ".join(SAFE_MARGINS)}')
    if style not in STYLES:
        raise RnError(f'--style must be one of {", ".join(STYLES)}')
    if position not in ('bottom', 'middle', 'top'):
        raise RnError('--position must be bottom, middle or top')
    text_rgb = _hex(color or '#ffffff', '--color')
    accent_rgb = _hex(accent or DEFAULT_ACCENT.get(style, '#ffe000'), '--accent')
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
    # Animated styles read as short-form captions: fewer, bigger words per line (about 0.65 of plain's line).
    max_chars = max_chars or max(4, round(BASE_CHARS['cjk' if cjk else 'latin'] * width / 1080 * (1 if style == 'plain' else 0.65)))
    em_per_char = 1.0 if cjk else LATIN_EM
    size_share = (0.03 if height > width else 0.05) * (1 if style == 'plain' else 1.4)
    font_size = round(min(height * size_share, safe_width / (max_chars * em_per_char)))
    fonts = WINDOWS_FONTS if sys.platform == 'win32' else FONTS
    font_name = font or (fonts.get(language.split('-')[0].lower(), fonts['']) if language else fonts['zh' if cjk else ''])

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
    event_lines: list[list[list[dict]]] = []
    moved: list[dict] = []
    order = [position] + [b for b in ('bottom', 'top', 'middle') if b != position]
    line_height = font_size * 1.2
    for index, chunk in enumerate(chunks):
        laid = _layout(chunk, max_chars, cjk)
        event_lines.append(laid)
        lines = [_join([u['text'] for u in line], cjk) for line in laid]
        box_w = min(safe_width, max(_em(line) for line in lines) * font_size)
        box_h = len(lines) * line_height
        bands = {'bottom': height - margins['bottom'] * height - box_h, 'top': margins['top'] * height, 'middle': (height - box_h) / 2}
        box_x = (width - box_w) / 2
        span_faces = _faces_in_span(frames, chunk[0]['start'], chunk[-1]['end'], scale) if frames else []
        band = position
        if span_faces and _overlaps((box_x, bands[position], box_w, box_h), span_faces, 0.01 * height):
            free = [b for b in order[1:] if not _overlaps((box_x, bands[b], box_w, box_h), span_faces, 0.01 * height)]
            if free:
                band = free[0]
                moved.append({'event': index, 'start': round(chunk[0]['start'], 3), 'end': round(chunk[-1]['end'], 3), 'from': position, 'to': band,
                              'reason': f'face box overlaps the {position} caption band'})
            else:
                warnings.append(f'event {index} ({chunk[0]["start"]:.2f}s): faces overlap every caption band; left at {position}')
        events.append({'start': chunk[0]['start'], 'end': chunk[-1]['end'], 'lines': lines, 'text': _join([u['text'] for u in chunk], cjk),
                       'words': [{'text': u['text'], 'start': round(u['start'], 3), 'end': round(u['end'], 3)} for u in chunk],
                       'position': {'band': band, 'x': round(box_x), 'y': round(bands[band]), 'w': round(box_w), 'h': round(box_h),
                                    'anchor': {'bottom': 'bottom-center', 'top': 'top-center', 'middle': 'middle-center'}[band]},
                       'moved_for_face': band != position})
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
    text_color, accent_color = pysubs2.Color(*text_rgb, 0), pysubs2.Color(*accent_rgb, 0)
    base = {'fontname': font_name, 'fontsize': font_size, 'bold': True, 'primarycolor': text_color, 'outlinecolor': pysubs2.Color(0, 0, 0, 0),
            'backcolor': pysubs2.Color(0, 0, 0, 128), 'outline': max(2, round(font_size * 0.07)), 'shadow': 1,
            'alignment': pysubs2.Alignment.BOTTOM_CENTER, 'marginl': round(safe_left), 'marginr': round(width - safe_right),
            'marginv': round(margins['bottom'] * height)}
    if style in ('clean', 'pop', 'karaoke', 'highlight'):  # heavy outline + soft drop shadow, no plate
        base |= {'outline': max(3, round(font_size * 0.1)), 'shadow': max(2, round(font_size * 0.06)), 'backcolor': pysubs2.Color(0, 0, 0, 96)}
    if style == 'karaoke':  # \k fills SecondaryColour (not yet spoken) with PrimaryColour (spoken)
        base |= {'primarycolor': accent_color, 'secondarycolor': text_color}
    if style == 'box':  # BorderStyle 3: an opaque box behind each line (libass and VSFilter take its colour from OutlineColour)
        box = pysubs2.Color(*accent_rgb, 0x40 if accent is None else 0)
        base |= {'borderstyle': 3, 'outline': max(4, round(font_size * 0.22)), 'shadow': 0, 'outlinecolor': box, 'backcolor': box}
    subs.styles = {'Default': pysubs2.SSAStyle(**base)}
    for event, laid in zip(events, event_lines):
        override = ''
        if event['position']['band'] != 'bottom':
            where = event['position']
            an, y = (8, where['y']) if where['band'] == 'top' else (5, where['y'] + where['h'] / 2)
            override = f"{{\\an{an}\\pos({round(width / 2)},{round(y)})}}"
        if style == 'plain' and not upper:
            text = '\\N'.join(event['lines'])
        else:
            text = _styled_text(style, event, laid, cjk, upper, _ass_color(text_rgb), _ass_color(accent_rgb))
        subs.append(pysubs2.SSAEvent(start=round(event['start'] * 1000), end=round(event['end'] * 1000), style='Default', text=override + text))
    subs.save(str(ass_path), encoding='utf-8')

    srt_path.write_text(''.join(f"{i}\n{_srt_time(e['start'])} --> {_srt_time(e['end'])}\n" + '\n'.join(e['lines']) + '\n\n' for i, e in enumerate(events, 1)), encoding='utf-8')
    result = {'ass': str(ass_path), 'srt': str(srt_path), 'json': str(json_path), 'event_count': len(events), 'language': language or None,
              'script_alignment': alignment, 'moved_for_faces': moved, 'warnings': warnings, 'safe': safe_name,
              'font': font_name, 'font_size': font_size, 'max_chars': max_chars, 'max_lines': max_lines, 'style': style,
              'position': position}
    json_path.write_text(json.dumps({'meta': {k: result[k] for k in ('language', 'safe', 'font', 'font_size', 'max_chars', 'max_lines')} | {'width': width, 'height': height},
                                     'events': events}, ensure_ascii=False, indent=1), encoding='utf-8')
    return result


def summary_lines(result: dict) -> list[str]:
    lines = [f"captions: {result['event_count']} events, style {result['style']}, language {result['language'] or 'auto'}, safe={result['safe']}, "
             f"{result['font']} {result['font_size']}px, max {result['max_chars']} chars x {result['max_lines']} lines",
             f"  ass {result['ass']}  srt {result['srt']}  json {result['json']}"]
    if result['script_alignment']:
        stats = result['script_alignment']
        lines.append(f"  script: {stats['match_ratio']:.0%} of {stats['script_units']} units matched Whisper; {stats['unmatched_span_count']} unmatched spans")
        lines += [f"    {s['start']:.2f}-{s['end']:.2f}s script '{s['script']}' vs heard '{s['transcript']}'" for s in stats['unmatched_spans'][:5]]
    lines += [f"  moved event {m['event']} ({m['start']:.2f}-{m['end']:.2f}s) {m['from']} -> {m['to']}: {m['reason']}" for m in result['moved_for_faces']]
    lines += [f'  warning: {w}' for w in result['warnings']]
    return lines
