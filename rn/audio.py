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


def measure(path: str | Path, *, stream: str = 'a:0') -> dict:
    """Integrated loudness (LUFS), loudness range and true peak (dBTP) via ebur128."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', str(path), '-map', f'0:{stream}',
                             '-af', 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-'], capture_output=True, text=True)
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


def finish_audio(mix_wav: Path, out_m4a: Path, *, lufs: float | None, true_peak: float) -> dict:
    """mix_wav -> AAC whose encoded true peak is at or below `true_peak` dBTP."""
    gain_db = 0.0
    if lufs is not None:
        measured = measure(mix_wav)['lufs']
        if measured is not None and measured != float('-inf'):
            gain_db = max(-30.0, min(30.0, lufs - measured))
    ceiling = true_peak - 0.5  # headroom for encoder overshoot; tightened below if still too hot
    attempts = []
    for _ in range(MAX_LIMITER_PASSES):
        limit = 10 ** (ceiling / 20)
        run(['ffmpeg', '-i', str(mix_wav), '-af',
             f'volume={gain_db:.3f}dB,alimiter=limit={max(0.0625, limit):.5f}:level=disabled:latency=1:attack=5:release=50,'
             f'aresample={SAMPLE_RATE}', '-c:a', 'aac', '-b:a', AAC_BITRATE, '-ar', str(SAMPLE_RATE), '-ac', '2', '-y', str(out_m4a)])
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


def mux(video: Path, audio_m4a: Path | None, out: Path, *, frames: int) -> None:
    """Copies both streams into an MP4 with moov first. The audio was already cut to the video's length by sample;
    no -shortest: on ffmpeg 7 it trims the video to the AAC stream, which can end a few ms early, dropping frames."""
    args = ['ffmpeg', '-i', str(video)]
    if audio_m4a:
        args += ['-i', str(audio_m4a), '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'copy']
    else:
        args += ['-map', '0:v:0']
    out.parent.mkdir(parents=True, exist_ok=True)
    run(args + ['-c:v', 'copy', '-frames:v', str(frames), '-movflags', '+faststart', '-y', str(out)])
