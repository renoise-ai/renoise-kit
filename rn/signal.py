"""rn signal: a video's and its sound's measurable state, frame by frame, as numbers. One series per quantity on one
shared clock (the video's frames; 30 per second for audio alone), so flashes, freezes, pacing, beats and music-driven
motion are all read from the same data instead of a detector per question. Facts only.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .ff import RnError, probe, run

SR = 48000
BANDS = {'low': (20, 250), 'mid': (250, 4000), 'high': (4000, 20000)}
FLASH = 0.2  # luma jump (0..1) up and back within FLASH_FRAMES
FLASH_FRAMES = 4
CLICK_WINDOW = 0.05  # seconds
_KEY = re.compile(r'lavfi\.signalstats\.(YAVG|YDIF|SATAVG)=([0-9.]+)')


def pcm(path: str | Path, rate: int = SR) -> np.ndarray:
    """The first audio stream as mono float32 at `rate`."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0', '-ac', '1',
                             '-ar', str(rate), '-f', 'f32le', '-'], capture_output=True)
    if result.returncode != 0:
        raise RnError(f'{path}: audio decode failed: {result.stderr.decode(errors="replace").strip().splitlines()[-1:]}')
    return np.frombuffer(result.stdout, dtype=np.float32)


def clicks(samples: np.ndarray, rate: int = SR, *, edge: float = 0.05) -> list[float]:
    """Lone sample steps: a step more than 8x the 99.5th percentile of its 50 ms window and 2.5x the window's next largest.
    The head and tail (`edge` seconds) are skipped: a file starting or ending mid-wave is not a click in the edit."""
    size = int(rate * CLICK_WINDOW)
    steps = np.abs(np.diff(samples))
    found = []
    for start in range(int(edge * rate), len(steps) - size - int(edge * rate), size):
        window = steps[start:start + size]
        top = np.partition(window, -2)[-2:]
        largest, second = top.max(), top.min()
        if largest > 0.05 and largest > 8 * np.percentile(window, 99.5) and largest > 2.5 * second:
            seconds = round((start + int(window.argmax())) / rate, 4)
            if not found or seconds - found[-1] > 0.01:  # an impulse steps up and down, maybe across two windows
                found.append(seconds)
    return found


def _video(path: str) -> tuple[list[float], list[float], list[float]]:
    result = run(['ffmpeg', '-i', path, '-map', '0:v:0', '-vf', 'signalstats,metadata=print:file=-', '-f', 'null', '-'])
    luma, diff, sat = [], [], []
    for line in result.stdout.splitlines():
        if line.startswith('frame:'):
            luma.append(0.0), diff.append(0.0), sat.append(0.0)
        elif (match := _KEY.search(line)) and luma:
            value = float(match.group(2))
            if match.group(1) == 'YAVG':
                luma[-1] = round(min(1.0, max(0.0, (value - 16) / 219)), 4)  # limited range -> 0..1
            elif match.group(1) == 'YDIF':
                diff[-1] = round(value / 255, 4)
            else:
                sat[-1] = round(value / 128, 4)
    return luma, diff, sat


def _onset(samples: np.ndarray, n: int, fps: float) -> list[float]:
    """Spectral flux per frame: the mean rise in dB over 32 log-spaced bands (like a mel scale, so a kick is not diluted
    by a thousand empty bins) between STFT frames (2048 / hop 512), the largest within each output frame. A steady tone
    reads ~0 whatever its phase; a hit or a new note reads several dB."""
    n_fft, hop = 2048, 512
    if len(samples) < n_fft + hop:
        return [0.0] * n
    window = np.hanning(n_fft)
    edges = np.unique(np.geomspace(1, n_fft // 2 + 1, 33).astype(int))
    band_of = np.digitize(np.arange(n_fft // 2 + 1), edges[1:-1])
    count = 1 + (len(samples) - n_fft) // hop
    flux = np.zeros(count)
    previous = None
    for first in range(0, count, 1024):  # bounded memory for long files
        idx = np.arange(n_fft)[None, :] + hop * np.arange(first, min(count, first + 1024))[:, None]
        power = np.abs(np.fft.rfft(samples[idx] * window, axis=1)) ** 2
        bands = np.stack([power[:, band_of == b].sum(axis=1) for b in range(band_of.max() + 1)], axis=1)
        db = 10 * np.log10(np.maximum(bands, 1e-12))
        db = np.maximum(db, db.max() - 60)  # leakage far below the content flickers; floor it (librosa's top_db)
        # The first STFT frame has nothing before it; later chunks continue from the previous chunk's last frame.
        start = first + 1 if previous is None else first
        stacked = db if previous is None else np.vstack([previous, db])
        flux[start:first + len(db)] = np.maximum(np.diff(stacked, axis=0), 0).mean(axis=1)
        previous = db[-1:]
    centre = (np.arange(count) * hop + n_fft / 2) / SR
    which = np.minimum((centre * fps).astype(int), n - 1)
    per_frame = np.zeros(n)
    np.maximum.at(per_frame, which, flux)
    return [round(float(v), 3) for v in per_frame]


def _audio(samples: np.ndarray, frames: int, fps: float) -> dict:
    hop = SR / fps
    n = frames or int(len(samples) / hop)
    size = int(round(hop))
    freqs = np.fft.rfftfreq(size, 1 / SR)
    window = np.hanning(size)
    rms, bands = [], {name: [] for name in BANDS}
    for k in range(n):
        chunk = samples[int(k * hop):int(k * hop) + size]
        if len(chunk) < size:
            chunk = np.pad(chunk, (0, size - len(chunk)))
        level = float(np.sqrt(np.mean(chunk ** 2)))
        rms.append(round(20 * np.log10(level), 2) if level > 1e-7 else -140.0)
        spectrum = np.abs(np.fft.rfft(chunk * window))
        for name, (lo, hi) in BANDS.items():
            energy = float(np.sqrt(np.sum(spectrum[(freqs >= lo) & (freqs < hi)] ** 2)) / size)
            bands[name].append(round(20 * np.log10(energy), 2) if energy > 1e-7 else -140.0)
    return {'rms_db': rms, **{f'{name}_db': values for name, values in bands.items()}, 'onset_db': _onset(samples, n, fps)}


def _flashes(luma: list[float]) -> list[int]:
    """Frames where the picture jumps brighter and comes back within a few frames (a white flash, a strobe hit)."""
    found = []
    for i in range(1, len(luma)):
        if luma[i] - luma[i - 1] >= FLASH and any(luma[i] - luma[j] >= FLASH for j in range(i + 1, min(len(luma), i + 1 + FLASH_FRAMES))):
            found.append(i)
    return found


def _plot(result: dict, out: Path) -> None:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows = [k for k in ('video', 'audio') if result.get(k)]
    figure, axes = plt.subplots(len(rows), 1, figsize=(12, 2.4 * len(rows)), sharex=True, squeeze=False)
    t = np.arange(result['frames']) / result['fps']
    for axis, kind in zip(axes[:, 0], rows):
        for name, values in result[kind].items():
            values = np.asarray(values, dtype=float)
            if name == 'onset_db':
                values = np.clip(values / 12, 0, 1)  # 0..12 dB of rise -> 0..1
            elif kind == 'audio':
                values = np.clip((values + 60) / 60, 0, 1)  # -60..0 dB -> 0..1
            axis.plot(t[:len(values)], values, label=name, linewidth=0.9)
        axis.set_ylim(0, 1.05)
        axis.legend(loc='upper right', fontsize=7, ncol=6)
    for frame in result.get('flashes', []):
        axes[0, 0].axvline(frame / result['fps'], color='orange', linewidth=0.8)
    for seconds in result.get('clicks', []):
        axes[-1, 0].axvline(seconds, color='red', linewidth=0.8)
    axes[-1, 0].set_xlabel('seconds')
    figure.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, dpi=90)
    plt.close(figure)


def signal_command(path: str, *, out: str | None) -> dict:
    info = probe(path)
    video = info['video'] if info['video'] and not info['video']['still'] else None
    if not video and not info['audio']:
        raise RnError(f'{path}: no video or audio stream')
    result: dict = {'path': path}
    luma = []
    if video:
        luma, diff, sat = _video(path)
        result.update(fps=float(video['fps_float']), frames=len(luma), video={'luma': luma, 'diff': diff, 'sat': sat},
                      flashes=_flashes(luma))
    else:
        result.update(fps=30.0, frames=0)
    if info['audio']:
        samples = pcm(path)
        result['audio'] = _audio(samples, result['frames'], result['fps'])
        result['frames'] = result['frames'] or len(result['audio']['rms_db'])
        result['clicks'] = clicks(samples)
    if out:
        _plot(result, Path(out))
        result['image'] = out
    return result


def summary_lines(result: dict) -> list[str]:
    lines = [f'{result["path"]}: {result["frames"]} frames at {result["fps"]:g} fps']
    if 'video' in result:
        luma = result['video']['luma']
        lines.append(f'luma {min(luma):.2f}..{max(luma):.2f}  mean diff {sum(result["video"]["diff"]) / len(luma):.3f}  '
                     f'flashes at frames {result["flashes"]}')
    if 'audio' in result:
        loud = [v for v in result['audio']['rms_db'] if v > -140]
        lines.append(f'audio rms {min(loud, default=-140):.1f}..{max(loud, default=-140):.1f} dB  clicks at {result["clicks"]} s')
    if 'image' in result:
        lines.append(f'image {result["image"]}')
    lines.append('series per frame in --json: video luma/diff/sat (0..1), audio rms/low/mid/high (dB), onset (dB of spectral rise)')
    return lines
