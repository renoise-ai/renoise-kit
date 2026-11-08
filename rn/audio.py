"""Loudness and true-peak finishing shared by `rn assemble` and `rn finish`.

The rule (spec/mcp/03-media-kit.md §5): mix without normalisation, gain to the LUFS target, limit with
alimiter's auto-level off, then measure the true peak on the ENCODED AAC — the encoder adds 1-2 dB of
overshoot, so a limiter that looks safe on PCM fails after encoding. Re-encode only the audio until it fits.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .ff import RnError, run

AAC_BITRATE = '192k'
SAMPLE_RATE = 48000
MAX_LIMITER_PASSES = 6
AAC = ('-c:a', 'aac', '-b:a', AAC_BITRATE)


def measure(path: str | Path, *, stream: str = 'a:0') -> dict:
    """Integrated loudness (LUFS), loudness range and true peak (dBTP) via ebur128."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', str(path), '-map', f'0:{stream}',
                             '-af', 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise RnError(f'loudness measurement failed: {result.stderr.strip().splitlines()[-1:]}')
    summary = result.stderr[result.stderr.rfind('Summary:'):]

    def pick(label: str) -> float | None:
        match = re.search(rf'{label}:\s+(-?inf|-?[\d.]+)', summary)
        if not match:
            return None
        value = match.group(1)
        return float('-inf') if 'inf' in value else float(value)

    return {'lufs': pick('I'), 'lra': pick('LRA'), 'true_peak': pick('Peak')}


def finish_audio(mix_wav: Path, out_m4a: Path, *, lufs: float | None, true_peak: float, reference: Path | None = None,
                 codec: tuple[str, ...] = AAC, channels: int = 2) -> dict:
    """mix_wav -> AAC (or `codec`, e.g. ('-c:a', 'pcm_s24le') for a WAV) whose encoded true peak is at or below `true_peak`
    dBTP. The LUFS gain is measured on `reference` (default mix_wav): an excerpt keeps the level of the whole mix."""
    gain_db = 0.0
    if lufs is not None:
        measured = measure(reference or mix_wav)['lufs']
        if measured is not None and measured != float('-inf'):
            gain_db = max(-30.0, min(30.0, lufs - measured))
    ceiling = true_peak - 0.5  # headroom for encoder overshoot; tightened below if still too hot
    attempts = []
    for _ in range(MAX_LIMITER_PASSES):
        limit = 10 ** (ceiling / 20)
        run(['ffmpeg', '-i', str(mix_wav), '-af',
             f'volume={gain_db:.3f}dB,alimiter=limit={max(0.0625, limit):.5f}:level=disabled:latency=1:attack=5:release=50,'
             f'aresample={SAMPLE_RATE}', *codec, '-ar', str(SAMPLE_RATE), '-ac', str(channels), '-y', str(out_m4a)])
        measured = measure(out_m4a)
        attempts.append({'limiter_dbfs': round(ceiling, 2), 'true_peak': measured['true_peak']})
        if measured['true_peak'] is None or measured['true_peak'] <= true_peak:
            return {'gain_db': round(gain_db, 2), **measured, 'attempts': attempts}
        ceiling -= max(0.5, measured['true_peak'] - true_peak)
    raise RnError(f'true peak stayed above {true_peak} dBTP after {MAX_LIMITER_PASSES} limiter passes: {attempts}')


# Frame properties win over encoder flags in ffmpeg 7+, so tag the frames themselves too.
BT709 = 'setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709'
VIDEO_ENCODE = ['-c:v', 'libx264', '-profile:v', 'high', '-pix_fmt', 'yuv420p',
                '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709']
# fixed_frame_rate_flag=1 in the SPS: with 0, some players and platforms time frames from the VUI tick and show a
# 30 fps film at half its length (promo-video, 2026-09-29). Only for constant-rate input: x264 then ignores timestamps.
CFR_VUI = ['-x264-params', 'force-cfr=1']
AUTO_MBPS = 12.0  # max_mbps "auto" at 1080x1920, 30 fps; scaled by pixels x fps, at least MIN_AUTO_MBPS
MIN_AUTO_MBPS = 1.0


def vbv(max_mbps, width: int, height: int, fps) -> tuple[list[str], float | None]:
    """A bitrate ceiling on the CRF encode: VBV maxrate = bufsize = the cap, so clean shots keep CRF quality and grain or
    noise stops at the cap (a minute of grainy 1080x1920 can otherwise reach 100 Mb/s). A clip of T seconds can average
    up to cap x (1 + 1/T): the encoder may spend the first buffer. Returns the ffmpeg flags and the cap in Mb/s."""
    if max_mbps is None:
        return [], None
    cap = max(MIN_AUTO_MBPS, AUTO_MBPS * width * height * float(fps) / (1080 * 1920 * 30)) if max_mbps == 'auto' else float(max_mbps)
    kbit = round(cap * 1000)
    return ['-maxrate', f'{kbit}k', '-bufsize', f'{kbit}k'], round(cap, 2)


def max_mbps_value(value, where: str):
    """'auto' or a number of Mb/s > 0 (CLI text or JSON)."""
    if value in (None, 'auto'):
        return value
    try:
        number = float(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        number = None
    if number is None or number <= 0:
        raise RnError(f'{where}: "auto" or Mb/s > 0, e.g. 12')
    return number


def mux(video: Path, audio_m4a: Path | None, out: Path, *, frames: int | None) -> None:
    """Copies both streams into an MP4 with moov first. The audio was already cut to the video's length by sample;
    no -shortest: on ffmpeg 7 it trims the video to the AAC stream, which can end a few ms early, dropping frames.
    frames=None copies every video packet and all the audio (rn clean: the sound keeps its own length)."""
    args = ['ffmpeg', '-i', str(video)]
    if audio_m4a:
        args += ['-i', str(audio_m4a), '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'copy']
    else:
        args += ['-map', '0:v:0']
    out.parent.mkdir(parents=True, exist_ok=True)
    run(args + ['-c:v', 'copy', *(['-frames:v', str(frames)] if frames else []), '-movflags', '+faststart', '-y', str(out)])
