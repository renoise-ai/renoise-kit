"""rn align: where one recording sits inside another, by their sound. Finds a song inside a reference edit, syncs camera
angles or a separately recorded voice, checks picture against sound. Measurement only: it reports the offset and how sure
it is; placing anything is yours (e.g. an audio item's "at", or "in" on a clip).
"""
from __future__ import annotations

import numpy as np

from .ff import RnError, probe
from .signal import pcm

COARSE_RATE = 200  # Hz envelope for the global search (5 ms)
FINE_RATE = 8000  # Hz waveform for refinement (0.125 ms)


def _envelope(samples: np.ndarray, rate: int) -> np.ndarray:
    """Onset-like envelope: log energy rises, which survive different mixes, levels and codecs better than waveforms."""
    hop = rate // COARSE_RATE
    frames = len(samples) // hop
    energy = np.log1p(1000 * np.sqrt(np.mean(samples[:frames * hop].reshape(frames, hop) ** 2, axis=1)))
    rise = np.maximum(np.diff(energy, prepend=energy[:1]), 0)
    return (rise - rise.mean()) / (rise.std() or 1.0)


def _lags(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Correlation of a with b at every lag (B's start inside A, in samples)."""
    size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    lags = np.concatenate([np.arange(0, len(a)), np.arange(-len(b) + 1, 0)])
    values = np.concatenate([corr[:len(a)], corr[size - len(b) + 1:]])
    return lags, values


def align_command(path_a: str, path_b: str, *, max_offset: float | None) -> dict:
    for path in (path_a, path_b):
        if not probe(path)['audio']:
            raise RnError(f'{path}: no audio to align by')
    a, b = pcm(path_a, FINE_RATE), pcm(path_b, FINE_RATE)
    if min(len(a), len(b)) < FINE_RATE:
        raise RnError('both recordings need at least 1 s of sound')
    env_a, env_b = _envelope(a, FINE_RATE), _envelope(b, FINE_RATE)
    lags, values = _lags(env_a, env_b)
    # Normalise by the overlap so short overlaps at the edges do not win on their own.
    overlap = np.minimum(len(env_a) - np.maximum(lags, 0), len(env_b) + np.minimum(lags, 0)).clip(min=1)
    score = values / overlap
    usable = overlap >= min(len(env_a), len(env_b)) * 0.25
    if max_offset is not None:
        usable &= np.abs(lags) <= max_offset * COARSE_RATE
    if not usable.any():
        raise RnError('no offset leaves enough overlap (at least a quarter of the shorter recording)')
    score = np.where(usable, score, -np.inf)
    best = int(np.argmax(score))
    lag = int(lags[best])
    # Confidence: the best peak against the best one at least 0.25 s away (a beat-periodic song has near-equal peaks).
    away = usable & (np.abs(lags - lag) > COARSE_RATE // 4)
    runner = float(score[away].max()) if away.any() else 0.0
    peak = float(score[best])
    # Refine on the waveform within +-10 ms of the coarse lag.
    coarse = lag / COARSE_RATE
    start_a = max(0, int(coarse * FINE_RATE))
    start_b = max(0, int(-coarse * FINE_RATE))
    length = min(len(a) - start_a, len(b) - start_b, 30 * FINE_RATE)
    window = FINE_RATE // 100
    seg_b = b[start_b:start_b + length]
    best_fine, best_value = 0, -np.inf
    if length > 2 * window:
        seg_a = a[max(0, start_a - window):start_a + length + window]
        fine_lags, fine_values = _lags(seg_a, seg_b)
        pad = start_a - max(0, start_a - window)
        mask = np.abs(fine_lags - pad) <= window
        if mask.any():
            pick = np.argmax(np.where(mask, fine_values, -np.inf))
            best_fine, best_value = int(fine_lags[pick]) - pad, float(fine_values[pick])
    offset = round(coarse + best_fine / FINE_RATE, 4)
    # How alike the two are where they overlap at that offset (Pearson on the waveform, up to 30 s).
    shift_a = max(0, round(offset * FINE_RATE))
    shift_b = max(0, round(-offset * FINE_RATE))
    span = min(len(a) - shift_a, len(b) - shift_b, 30 * FINE_RATE)
    match = float(np.corrcoef(a[shift_a:shift_a + span], b[shift_b:shift_b + span])[0, 1]) if span > FINE_RATE // 10 else 0.0
    info_a = probe(path_a)
    fps = info_a['video']['fps_float'] if info_a['video'] and not info_a['video']['still'] else None
    return {'a': path_a, 'b': path_b, 'offset': offset, 'offset_frames': round(offset * fps) if fps else None,
            'match': round(match, 3), 'distinct': round(max(0.0, min(1.0, (peak - runner) / peak)) if peak > 0 else 0.0, 3)}


def summary_lines(result: dict) -> list[str]:
    frames = f' ({result["offset_frames"]} frames of A)' if result['offset_frames'] is not None else ''
    where = 'starts' if result['offset'] >= 0 else 'started'
    return [f'B {where} at {result["offset"]:+.4f}s on A\'s clock{frames}: A time = B time + offset',
            f'match {result["match"]} (waveform correlation where they overlap: near 1 the same sound, ~0 unrelated; a different '
            f'mix of the same song reads lower), distinct {result["distinct"]} (how far ahead of the best offset 0.25 s or more '
            'away; low for repetitive music, whose next bar looks the same)']
