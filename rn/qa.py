"""rn qa: delivery checks on the ENCODED file. FAIL blocks delivery; WARN needs a look (it may be intended)."""
from __future__ import annotations

import re
import subprocess
from fractions import Fraction
from pathlib import Path

from .audio import measure
from .edl import Edit, load
from .ff import RnError, probe


def _scan(path: str) -> dict:
    """One decode pass: black intervals, frozen intervals and the union crop box (black bars)."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-map', '0:v:0', '-vf',
                             'blackdetect=d=0.1:pix_th=0.10,freezedetect=n=-60dB:d=0.5,cropdetect=limit=24:round=2:reset=0',
                             '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise RnError(f'{path}: decode failed: {result.stderr.strip().splitlines()[-1:]}')
    log = result.stderr
    black = [(float(a), float(b)) for a, b in re.findall(r'black_start:([\d.]+) black_end:([\d.]+)', log)]
    starts = [float(v) for v in re.findall(r'freeze_start: ([\d.]+)', log)]
    ends = [float(v) for v in re.findall(r'freeze_end: ([\d.]+)', log)]
    freezes = [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]
    crops = re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', log)
    crop = tuple(int(v) for v in crops[-1]) if crops else None
    return {'black': black, 'freeze': freezes, 'crop': crop}


SILENCE_DB, SILENCE_MIN = -70, 0.06  # digital silence inside a mix reads as a glitch, not a pause
JUMP_DB = 15  # between adjacent 100 ms RMS windows: right for a designed hit, wrong for a music edit


def _sound(path: str, seconds: float) -> dict:
    """One decode pass: digital silences away from the head and tail, and level jumps between 100 ms windows."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-map', '0:a:0', '-af',
                             f'aresample=48000,silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN},asetnsamples=n=4800:p=0,'
                             'astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-',
                             '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise RnError(f'{path}: audio decode failed: {result.stderr.strip().splitlines()[-1:]}')
    starts = [float(v) for v in re.findall(r'silence_start: (-?[\d.]+)', result.stderr)]
    ends = [float(v) for v in re.findall(r'silence_end: ([\d.]+)', result.stderr)]
    silences = [(max(0.0, s), ends[i] if i < len(ends) else seconds) for i, s in enumerate(starts)]
    silences = [(a, b) for a, b in silences if a > 0.05 and b < seconds - 0.05]  # lead-in and tail-out are a choice
    levels = [float(v) for v in re.findall(r'RMS_level=(-?inf|-?[\d.]+)', result.stdout)]
    # The last window is partial and holds the encoder's tail, so it reads as a drop; skip it like the head.
    jumps = [(round(k * 0.1, 1), levels[k - 1], levels[k]) for k in range(1, len(levels))
             if k * 0.1 < seconds - 0.15 and abs(levels[k] - levels[k - 1]) >= JUMP_DB and max(levels[k], levels[k - 1]) > -60]
    return {'silence': silences, 'jumps': jumps}


def _fixed_frame_rate(path: str) -> bool | None:
    """The H.264 SPS fixed_frame_rate_flag, or None when the stream carries no timing info."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'trace_headers',
                             '-frames:v', '1', '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    flag = re.search(r'fixed_frame_rate_flag\s+[01]\s+=\s+([01])', result.stderr)
    return flag.group(1) == '1' if flag else None


def _intended(edit: Edit | None, kind: str) -> list[tuple[float, float]]:
    """Timeline ranges the EDL itself makes black or still, so qa does not flag them."""
    if not edit:
        return []
    ranges = []
    for clip in edit.base:
        if (kind == 'black' and clip.kind == 'color') or (kind == 'freeze' and clip.kind in ('image', 'color')):
            ranges.append((float(clip.timeline_start / edit.fps), float((clip.timeline_start + clip.frames) / edit.fps)))
    return ranges


def _covered(interval: tuple[float, float | None], ranges: list[tuple[float, float]], end: float) -> bool:
    start, stop = interval[0], interval[1] if interval[1] is not None else end
    return any(a - 0.05 <= start and stop <= b + 0.05 for a, b in ranges)


# cuts_in_speech. GUARD: s of output time each side of a cut edge that must all be sound for the edge to sit inside one;
# more than a 20 ms window (a whole window on each side of the edge), less than the shortest pause between words
# (~50 ms), so an edge placed anywhere in a pause passes. CONTEXT: s of source each side of the edge whose levels set
# the threshold (local: a take's level drifts). CONTRAST: dB between the context's sound (90th percentile window) and
# its floor (10th) below which it has no pauses to find (a tone, steady ambience) and is not judged.
SPEECH_GUARD = 0.03
SPEECH_CONTEXT = 3.0
SPEECH_CONTRAST = 12.0


def _edge_in_sound(source: Path, at: float, guard: float) -> dict:
    """Whether source time `at` falls inside a sound: every 20 ms window (10 ms hop, rn listen --speech's windows)
    centred within `guard` of it is above a threshold set by the source around it. The threshold is a third of the way
    (in dB) from the context's floor (10th percentile window) to its sound (90th percentile), never below SPEECH_DB
    (rn listen --speech's fixed one), so a noisy room and a clean TTS take are judged alike: on a clean take it is
    SPEECH_DB and the gaps found are --speech's; in room noise it sits above the noise. A third, not half: a word's
    quiet consonants and decaying end still count as sound (a cut there still clips the word); a pause only needs one
    window below it. A context with less than SPEECH_CONTRAST dB between the two is steady sound (a tone, ambience, a
    music wall) with no pauses to cut in: not judged. A guard that reaches past the source's first or last sample is
    not inside a sound: the source starts or ends there, the edit cut nothing away. Also the nearest quiet window
    centre before and after `at` (source seconds), for the fix."""
    import numpy as np

    from .listen import SPEECH_DB, SPEECH_HOP, SPEECH_RATE, SPEECH_WIN, speech_levels, speech_samples

    start = max(0.0, at - SPEECH_CONTEXT)
    samples = speech_samples(source, start, at + SPEECH_CONTEXT - start)
    levels = np.maximum(speech_levels(samples), -100.0)
    if len(levels) < 10:
        return {'judged': False}
    floor, sound = float(np.percentile(levels, 10)), float(np.percentile(levels, 90))
    if sound - floor < SPEECH_CONTRAST:
        return {'judged': False}
    threshold = max(SPEECH_DB, floor + (sound - floor) / 3)
    hop, win = SPEECH_HOP / SPEECH_RATE, SPEECH_WIN / SPEECH_RATE
    centres = start + np.arange(len(levels)) * hop + win / 2
    near = np.abs(centres - at) <= guard + 1e-9
    covered = at - guard - win / 2 >= start - 1e-9 and at + guard + win / 2 <= start + len(samples) / SPEECH_RATE + 1e-9
    quiet = centres[levels < threshold]
    before, after = quiet[quiet < at], quiet[quiet > at]
    return {'judged': True, 'inside': bool(covered and near.any() and (levels[near] >= threshold).all()),
            'threshold': round(threshold, 1), 'quiet_before': round(float(before[-1]), 3) if len(before) else None,
            'quiet_after': round(float(after[0]), 3) if len(after) else None}


def _ref(clip) -> str | None:
    """How rn edit names a main-track clip: its id, or "0:N" by position; None for a span of "ranges" (no own entry)."""
    if clip.id and '.ranges[' not in clip.where:
        return clip.id
    match = re.fullmatch(r'tracks\[0\]\.clips\[(\d+)\]', clip.where)
    return f'0:{match.group(1)}' if match else None


def cuts_in_speech(edit: Edit) -> tuple[list[dict], int, int]:
    """Main-track hard cuts that land inside a sound of the clips' own audio (a clipped word): (findings, cuts checked,
    edges not judged).

    Measured on each clip's SOURCE at its edges, not on the render: in the mix a music bed, a voice-over item or a
    room-tone item keeps sounding across every cut, and mix.cut_fade dips the clip's own sound right at the cut, so the
    render cannot tell a clipped word from a covered or faded one. The source has neither. For clip A ending at
    source time a_out and clip B starting at b_in, A's sound is cut off when A's source is still sounding from
    SPEECH_GUARD before a_out to SPEECH_GUARD after it (the word went on past the out point), and B starts mid-sound
    when B's source sounds across b_in the same way. A cut_fade (at most 0.25 s, usually ~10 ms of click removal)
    softens the edge but the word is still truncated, so it does not waive the finding. Only hard cuts (no transition:
    an xfade already crossfades the sound) and clips that play their own sound (video "src" with an audio stream,
    "audio" on, no speed ramp) count; audio items (music, voice, sfx) are never cut by a main-track cut. ponytail:
    nested edits are not opened (their sound is a render of its own); a clip's gain_db / keys.gain_db / af are not
    applied (a clip faded to silence at the cut by its own keys still reads as cut off); speech with no gap between two
    words reads as one sound, so a cut on that word boundary is flagged too."""
    fps = edit.fps
    findings, checked, unjudged = [], 0, 0
    for i in range(1, len(edit.base)):
        a, b = edit.base[i - 1], edit.base[i]
        if b.transition_frames:
            continue
        sides = {}
        for side, clip in (('tail', a), ('head', b)):
            if not (clip.kind == 'video' and clip.audio and clip.has_audio):
                continue
            edge = clip.start_s + (float(clip.frames / fps) * clip.speed if side == 'tail' else 0.0)
            sides[side] = (clip, round(edge, 3), _edge_in_sound(clip.source, edge, SPEECH_GUARD * clip.speed))
        if not sides:
            continue
        checked += 1
        unjudged += sum(1 for _, _, m in sides.values() if not m['judged'])
        hit = {side: v for side, v in sides.items() if v[2]['judged'] and v[2]['inside']}
        if not hit:
            continue
        finding = {'at': round(float(b.timeline_start / fps), 3), 'out_of': a.id or a.where, 'into': b.id or b.where}
        for side, (clip, edge, measured) in hit.items():
            # The nearer quiet point: before it drops (tail) / keeps (head) the clipped word, after it keeps / drops it.
            options = [t for t in (measured['quiet_before'], measured['quiet_after']) if t is not None and
                       (t > clip.start_s if side == 'tail' else t < clip.start_s + float(clip.frames / fps) * clip.speed)]
            fix = min(options, key=lambda t: abs(t - edge)) if options else None
            finding[side] = {'clip': clip.id or clip.where, 'ref': _ref(clip), 'source': str(clip.source), 'edge': edge,
                             'quiet_before': measured['quiet_before'], 'quiet_after': measured['quiet_after'],
                             ('out' if side == 'tail' else 'in'): fix}
        findings.append(finding)
    return findings, checked, unjudged


def _speech_detail(f: dict) -> str:
    parts = []
    if 'tail' in f:
        parts.append(f"{f['out_of']} cut off mid-sound (source {f['tail']['edge']:.2f}s)")
    if 'head' in f:
        parts.append(f"{f['into']} starts mid-sound (source {f['head']['edge']:.2f}s)")
    return f"{f['at']:.2f}s {f['out_of']}|{f['into']}: " + ', '.join(parts)


def parse_accept(values: list[str]) -> list[dict]:
    """--accept CHECK or CHECK@SECONDS."""
    accepted = []
    for value in values:
        name, _, at = value.partition('@')
        try:
            accepted.append({'check': name, **({'at': float(at)} if at else {}), 'note': 'command line'})
        except ValueError:
            raise RnError(f'--accept {value}: CHECK or CHECK@SECONDS, e.g. dead_air@12.5') from None
    return accepted


def qa_command(path: str, *, edl: str | None, placeholders: bool = False, frames: int | None, size: str | None, fps: str | None, lufs: float | None,
               lufs_tolerance: float, true_peak: float | None, require_audio: bool, accept: list[dict] | None = None) -> dict:
    edit = load(edl, placeholders=placeholders) if edl else None
    checks: list[dict] = []
    accepts = (edit.accept if edit else []) + (accept or [])

    def check(name: str, status: str, detail: str):
        checks.append({'check': name, 'status': status, 'detail': detail})

    def keep(name: str, found: list, span) -> tuple[list, int]:
        """Findings of an interval check minus those accepted (an accept "at" inside the interval, or without "at")."""
        kept = [f for f in found if not any(a['check'] == name and ('at' not in a or span(f)[0] - 0.05 <= a['at'] <= span(f)[1] + 0.05)
                                            for a in accepts)]
        return kept, len(found) - len(kept)

    def note(text: str, dropped: int) -> str:
        return text + (f' ({dropped} accepted)' if dropped else '')

    info = probe(path, exact=True)
    video, audio = info['video'], info['audio']
    if not video or video['still']:
        raise RnError(f'{path}: no video stream')
    expected_frames = frames or (edit.total_frames if edit else None)
    expected_size = size or (f'{edit.width}x{edit.height}' if edit else None)
    expected_fps = Fraction(fps) if fps else (edit.fps if edit else None)
    if lufs is None and edit and edit.lufs is not None:
        lufs = edit.lufs
    if true_peak is None:
        true_peak = edit.true_peak if edit else -1.0
    require_audio = require_audio or bool(edit and (edit.audio or any(c.audio and c.has_audio for c in edit.base)))

    actual_fps = Fraction(video['fps'])
    frame_time = float(1 / actual_fps)
    count = video['frames']
    if expected_frames is not None:
        check('frames', 'PASS' if count == expected_frames else 'FAIL', f'{count} decoded, expected {expected_frames}')
    else:
        check('frames', 'PASS', f'{count} decoded')
    if expected_fps is not None:
        check('fps', 'PASS' if actual_fps == expected_fps else 'FAIL', f'{video["fps"]}, expected {expected_fps}')
    dims = f'{video["width"]}x{video["height"]}'
    if expected_size:
        check('size', 'PASS' if dims == expected_size else 'FAIL', f'{dims}, expected {expected_size}')
    if video['width'] % 2 or video['height'] % 2:
        check('size', 'FAIL', f'{dims}: odd dimensions break yuv420p players')
    check('codec', 'PASS' if video['codec'] == 'h264' else ('WARN' if video['codec'] == 'hevc' else 'FAIL'),
          f'{video["codec"]} {video["profile"] or ""}'.strip() + ('' if video['codec'] == 'h264' else ' (deliver H.264 for widest playback)'))
    check('pix_fmt', 'PASS' if video['pix_fmt'] == 'yuv420p' else 'FAIL', f'{video["pix_fmt"]}' + ('' if video['pix_fmt'] == 'yuv420p' else ' (players and platforms expect yuv420p)'))
    check('sar', 'PASS' if video['sar'] == '1:1' else 'FAIL', f'{video["sar"]}' + ('' if video['sar'] == '1:1' else ' (non-square pixels display stretched)'))
    tags = (video['color_space'], video['color_primaries'], video['color_transfer'])
    check('color', 'PASS' if tags == ('bt709', 'bt709', 'bt709') else 'WARN', 'space/primaries/transfer = ' + '/'.join(str(t) for t in tags))
    if video['codec'] == 'h264':
        fixed = _fixed_frame_rate(path)
        if fixed is not None:
            check('fixed_frame_rate', 'PASS' if fixed else 'WARN', 'SPS fixed_frame_rate_flag=1' if fixed else
                  'SPS fixed_frame_rate_flag=0: some players time frames from the VUI and show the wrong length (rn finish fixes it)')
    if info['faststart'] is not None:
        check('faststart', 'PASS' if info['faststart'] else 'FAIL', 'moov before mdat' if info['faststart'] else 'moov at the end: remux with -movflags +faststart')

    video_seconds = count * frame_time
    if audio:
        # Matroska/WebM carry no per-stream duration; the container's is the best available then.
        audio_seconds = audio['duration'] if audio['duration'] is not None else info['duration']
        if audio_seconds is None:
            check('av_duration', 'WARN', f'video {video_seconds:.3f}s, audio duration unknown')
        else:
            drift = abs(audio_seconds - video_seconds)
            check('av_duration', 'PASS' if drift <= max(frame_time, 0.05) else 'WARN',
                  f'video {video_seconds:.3f}s, audio {audio_seconds:.3f}s')
        loud = measure(path)
        tp, integrated = loud['true_peak'], loud['lufs']
        if tp is not None:
            check('true_peak', 'PASS' if tp <= true_peak else 'FAIL', f'{tp:.1f} dBTP (limit {true_peak:.1f})')
        if integrated is not None and integrated != float('-inf'):
            if lufs is not None:
                ok = abs(integrated - lufs) <= lufs_tolerance
                check('loudness', 'PASS' if ok else 'WARN', f'{integrated:.1f} LUFS (target {lufs:.1f} ±{lufs_tolerance:g}), LRA {loud["lra"]}')
            else:
                check('loudness', 'PASS', f'{integrated:.1f} LUFS, LRA {loud["lra"]}')
            sound = _sound(path, video_seconds)
            silence, dropped = keep('dead_air', sound['silence'], lambda f: f)
            check('dead_air', 'WARN' if silence else 'PASS', note(
                ', '.join(f'{a:.2f}-{b:.2f}s' for a, b in silence) + ' below -70 dBFS (fill with a tail, room tone or bed)'
                if silence else 'no digital silence inside the mix', dropped))
            jumps, dropped = keep('level_jumps', sound['jumps'], lambda f: (f[0] - 0.1, f[0]))
            check('level_jumps', 'WARN' if jumps else 'PASS', note(
                ', '.join(f'{t:.1f}s {a:.0f}->{b:.0f} dB' for t, a, b in jumps[:8]) + (f' (+{len(jumps) - 8} more)' if len(jumps) > 8 else '')
                if jumps else f'no jump >= {JUMP_DB} dB between 100 ms windows', dropped))
            from .signal import clicks, pcm
            pops, dropped = keep('clicks', clicks(pcm(path)), lambda f: (f, f))
            check('clicks', 'WARN' if pops else 'PASS', note(
                ', '.join(f'{t:.3f}s' for t in pops[:8]) + ' (a lone sample step: an edit without a fade, or a glitch)'
                if pops else 'no lone sample steps', dropped))
        else:
            check('loudness', 'WARN', 'audio track is silent')
    else:
        check('audio', 'FAIL' if require_audio else 'WARN', 'no audio track')
    if edit and audio:
        found, checked, unjudged = cuts_in_speech(edit)
        if checked:
            clipped, dropped = keep('cuts_in_speech', found, lambda f: (f['at'], f['at']))
            check('cuts_in_speech', 'WARN' if clipped else 'PASS', note(
                ('; '.join(_speech_detail(f) for f in clipped[:6]) + (f' (+{len(clipped) - 6} more)' if len(clipped) > 6 else '')
                 + ' (a clipped word: move the edge into a pause)') if clipped else
                f'{checked} hard cuts with clip sound, none inside a sound', dropped)
                + (f'; {unjudged} edges in steady sound not judged' if unjudged else ''))
            checks[-1]['cuts'] = clipped
    container = info['duration'] or 0
    if container > video_seconds + max(frame_time, 0.05):
        check('container_duration', 'WARN', f'container {container:.3f}s > video {video_seconds:.3f}s (a longer stream stretches playback)')

    scan = _scan(path)
    black = [b for b in scan['black'] if not _covered(b, _intended(edit, 'black'), video_seconds)]
    black, dropped = keep('black_frames', black, lambda f: f)
    check('black_frames', 'WARN' if black else 'PASS',
          note(', '.join(f'{a:.2f}-{b:.2f}s' for a, b in black) if black else 'none outside intended color clips', dropped))
    freezes = [f for f in scan['freeze'] if not _covered(f, _intended(edit, 'freeze'), video_seconds)]
    freezes, dropped = keep('frozen_frames', freezes, lambda f: (f[0], f[1] if f[1] is not None else video_seconds))
    check('frozen_frames', 'WARN' if freezes else 'PASS', note(
        ', '.join(f'{a:.2f}-{(b if b is not None else video_seconds):.2f}s' for a, b in freezes) if freezes else 'none outside stills', dropped))
    if scan['crop']:
        w, h, x, y = scan['crop']
        bars = w < video['width'] - 8 or h < video['height'] - 8
        check('black_bars', 'WARN' if bars else 'PASS', f'picture area {w}x{h}+{x}+{y} of {dims}' if bars else 'full frame used')

    for item in checks:  # a whole-check accept (no "at") turns a WARN into an accepted PASS; a FAIL is never waived
        waiver = next((a for a in accepts if a['check'] == item['check'] and 'at' not in a), None)
        if item['status'] == 'WARN' and waiver:
            item['status'], item['accepted'] = 'PASS', waiver.get('note', '')
            item['detail'] += f' (accepted: {waiver.get("note") or "intended"})'
    failed = [c for c in checks if c['status'] == 'FAIL']
    warned = [c for c in checks if c['status'] == 'WARN']
    return {'path': path, 'status': 'FAIL' if failed else 'PASS', 'failed': len(failed), 'warnings': len(warned), 'checks': checks}
