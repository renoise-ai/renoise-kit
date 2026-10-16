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
                             '-f', 'null', '-'], capture_output=True, text=True)
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
                             '-f', 'null', '-'], capture_output=True, text=True)
    if result.returncode != 0:
        raise RnError(f'{path}: audio decode failed: {result.stderr.strip().splitlines()[-1:]}')
    starts = [float(v) for v in re.findall(r'silence_start: (-?[\d.]+)', result.stderr)]
    ends = [float(v) for v in re.findall(r'silence_end: ([\d.]+)', result.stderr)]
    silences = [(max(0.0, s), ends[i] if i < len(ends) else seconds) for i, s in enumerate(starts)]
    silences = [(a, b) for a, b in silences if a > 0.05 and b < seconds - 0.05]  # lead-in and tail-out are a choice
    levels = [float(v) for v in re.findall(r'RMS_level=(-?inf|-?[\d.]+)', result.stdout)]
    jumps = [(round(k * 0.1, 1), levels[k - 1], levels[k]) for k in range(1, len(levels))
             if abs(levels[k] - levels[k - 1]) >= JUMP_DB and max(levels[k], levels[k - 1]) > -60]
    return {'silence': silences, 'jumps': jumps}


def _fixed_frame_rate(path: str) -> bool | None:
    """The H.264 SPS fixed_frame_rate_flag, or None when the stream carries no timing info."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-map', '0:v:0', '-c', 'copy', '-bsf:v', 'trace_headers',
                             '-frames:v', '1', '-f', 'null', '-'], capture_output=True, text=True)
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


def qa_command(path: str, *, edl: str | None, frames: int | None, size: str | None, fps: str | None, lufs: float | None,
               lufs_tolerance: float, true_peak: float | None, require_audio: bool) -> dict:
    edit = load(edl) if edl else None
    checks: list[dict] = []

    def check(name: str, status: str, detail: str):
        checks.append({'check': name, 'status': status, 'detail': detail})

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
            check('dead_air', 'WARN' if sound['silence'] else 'PASS',
                  ', '.join(f'{a:.2f}-{b:.2f}s' for a, b in sound['silence']) + ' below -70 dBFS (fill with a tail, room tone or bed)'
                  if sound['silence'] else 'no digital silence inside the mix')
            jumps = sound['jumps']
            check('level_jumps', 'WARN' if jumps else 'PASS',
                  ', '.join(f'{t:.1f}s {a:.0f}->{b:.0f} dB' for t, a, b in jumps[:8]) + (f' (+{len(jumps) - 8} more)' if len(jumps) > 8 else '')
                  if jumps else f'no jump >= {JUMP_DB} dB between 100 ms windows')
        else:
            check('loudness', 'WARN', 'audio track is silent')
    else:
        check('audio', 'FAIL' if require_audio else 'WARN', 'no audio track')
    container = info['duration'] or 0
    if container > video_seconds + max(frame_time, 0.05):
        check('container_duration', 'WARN', f'container {container:.3f}s > video {video_seconds:.3f}s (a longer stream stretches playback)')

    scan = _scan(path)
    black = [b for b in scan['black'] if not _covered(b, _intended(edit, 'black'), video_seconds)]
    check('black_frames', 'WARN' if black else 'PASS',
          ', '.join(f'{a:.2f}-{b:.2f}s' for a, b in black) if black else 'none outside intended color clips')
    freezes = [f for f in scan['freeze'] if not _covered(f, _intended(edit, 'freeze'), video_seconds)]
    check('frozen_frames', 'WARN' if freezes else 'PASS',
          ', '.join(f'{a:.2f}-{(b if b is not None else video_seconds):.2f}s' for a, b in freezes) if freezes else 'none outside stills')
    if scan['crop']:
        w, h, x, y = scan['crop']
        bars = w < video['width'] - 8 or h < video['height'] - 8
        check('black_bars', 'WARN' if bars else 'PASS', f'picture area {w}x{h}+{x}+{y} of {dims}' if bars else 'full frame used')

    failed = [c for c in checks if c['status'] == 'FAIL']
    warned = [c for c in checks if c['status'] == 'WARN']
    return {'path': path, 'status': 'FAIL' if failed else 'PASS', 'failed': len(failed), 'warnings': len(warned), 'checks': checks}
