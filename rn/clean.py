"""rn clean: voice cleanup that measures first and keeps every word clear.

Measure (noise floor, speech level and the gap between them, clipping, mains hum), then clean with ffmpeg filters only:
high-pass for rumble, notches at the hum and its harmonics (only when hum was found), afftdn with a noise floor from the
measurement, gentle mud / presence EQ, de-esser, gentle compression, then loudness to the target with rn's finishing
code (rn/audio.py). With faster-whisper the speech is transcribed before and after each strength, from the requested one
down, and the strongest that loses no word and barely any confidence is kept. A video keeps its picture untouched
(stream copy): only the sound is replaced, same length, no offset.
"""
from __future__ import annotations

import difflib
import importlib.util
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from .ff import RnError, probe, run

STRENGTHS = ('light', 'medium', 'strong')
# afftdn per strength: nr = dB of reduction, nf_above = how far above the measured floor its noise floor sits. afftdn
# (no noise tracking) leaves noise below its floor alone, so a floor a few dB over the measured one lets nr act fully;
# measured on white noise under tone bursts (ffmpeg 6.1): floor +8/nr 10 takes ~9 dB off, +10/14 ~13 dB, +14/24 ~23 dB.
DENOISE = {'light': {'nr': 10, 'nf_above': 8}, 'medium': {'nr': 14, 'nf_above': 10}, 'strong': {'nr': 24, 'nf_above': 14}}
NF_RANGE = (-80.0, -20.0)  # afftdn's own limits
NF_BELOW_SPEECH = 12.0  # its floor stays at least this far under the speech level, whatever the gap
CLEAN_FLOOR = -75.0  # dBFS: a floor (after rumble and hum filters) below this needs no denoise
HIGHPASS_HZ = 80  # rumble, handling noise, most of the mains fundamental; voice fundamentals sit above
EQ = 'equalizer=f=250:t=q:w=1:g=-2,equalizer=f=3500:t=q:w=1:g=2'  # mud down 2 dB, presence up 2 dB, one octave wide
DEESSER = 'deesser=i=0.4:m=0.5:f=0.5:s=o'
COMP_BELOW_SPEECH = 6.0  # compressor threshold: dB under the speech level; ratio 2 takes ~3 dB off the loudest syllables
TRUE_PEAK = -1.0
SAMPLE_SECONDS = 10.0
CHECK_SECONDS = 30.0  # the clarity check transcribes the busiest 30 s of speech, not the whole file
CONFIDENCE_DROP = 0.015  # mean word probability may drop this much at most

RATE = 16000  # measurement rate: speech band and mains harmonics fit; 50 ms windows, 25 ms hop
WIN, HOP = RATE // 20, RATE // 40
QUIET_SHARE, LOUD_SHARE = 0.05, 0.10  # noise floor: quietest 5 % of windows; speech level: loudest 10 %
SILENT_DB = -100.0  # windows quieter than this are digital silence (padding), not the noise floor
GATED_SHARE = 0.2  # ... unless this share of the windows is digitally silent: then that silence is the floor
MAINS = (50, 60)
HUM_DB = 10.0  # a mains frequency is hum when one of its first 3 harmonics stands this far over its neighbours
HARMONIC_DB = 6.0  # further harmonics (up to the 8th) are notched when they stand out this much
MAX_NOTCHES = 5
NOTCH_Q = 25  # 2 Hz wide at 50 Hz, 10 Hz at 250 Hz: mains drift fits, a voice loses a sliver
FULL_SCALE = 0.999  # clipping: runs of at least CLIP_RUN samples at full scale (flat tops)
CLIP_RUN = 3

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv'}
AUDIO_CODECS = {'.wav': ('-c:a', 'pcm_s24le'), '.flac': ('-c:a', 'flac'), '.mp3': ('-c:a', 'libmp3lame', '-b:a', '192k'),
                '.m4a': audio_mod.AAC, '.aac': audio_mod.AAC, '.opus': ('-c:a', 'libopus', '-b:a', '128k')}


# ---------------------------------------------------------------- measurement

def _pcm(path: Path, rate: int = RATE, af: str | None = None) -> np.ndarray:
    result = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0',
                             *(['-af', af] if af else []), '-ac', '1', '-ar', str(rate), '-f', 'f32le', '-'], capture_output=True)
    if result.returncode != 0:
        raise RnError(f'{path}: audio decode failed: {result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]}')
    return np.frombuffer(result.stdout, dtype=np.float32).astype(np.float64)


def _window_db(samples: np.ndarray) -> np.ndarray:
    """Power in dBFS of every 50 ms window (25 ms hop)."""
    count = max(0, (len(samples) - WIN) // HOP + 1)
    if not count:
        return np.array([10 * np.log10(max(float(np.mean(samples ** 2)) if len(samples) else 0.0, 1e-12))])
    energy = np.concatenate([[0.0], np.cumsum(samples ** 2)])
    starts = np.arange(count) * HOP
    return 10 * np.log10(np.maximum((energy[starts + WIN] - energy[starts]) / WIN, 1e-12))


def _power_mean_db(db: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(10 ** (db / 10)))) if len(db) else -120.0


def levels(samples: np.ndarray) -> dict:
    """Noise floor (quietest 5 % of 50 ms windows), speech level (loudest 10 %), the gap in dB. Digital silence (padding)
    is left out of the floor, unless it is a fifth of the file or more: then the sound is gated (TTS) and has no floor."""
    db = np.sort(_window_db(samples))
    live = db[db > SILENT_DB]
    if len(live) < len(db) * (1 - GATED_SHARE):
        live = db
    floor = _power_mean_db(live[:max(1, int(len(live) * QUIET_SHARE))]) if len(live) else -120.0
    speech = _power_mean_db(db[-max(1, int(len(db) * LOUD_SHARE)):])
    return {'noise_floor_db': round(floor, 1), 'speech_db': round(speech, 1), 'gap_db': round(speech - floor, 1)}


def _spectrum(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Averaged power spectrum with ~1 Hz bins (Welch, Hann, half overlap), at most 400 segments."""
    size = 16384 if len(samples) >= 16384 else 8192 if len(samples) >= 8192 else 0
    if not size:
        return None
    starts = np.arange(0, len(samples) - size + 1, size // 2)
    if len(starts) > 400:
        starts = starts[np.linspace(0, len(starts) - 1, 400).round().astype(int)]
    window = np.hanning(size)
    power = sum(np.abs(np.fft.rfft(samples[s:s + size] * window)) ** 2 for s in starts)
    return np.fft.rfftfreq(size, 1 / RATE), power


def _prominence(spectrum, hz: float) -> float:
    """dB of the strongest bin within 1.5 Hz of hz over the median of the bins 4-12 Hz away on both sides."""
    freqs, power = spectrum
    off = np.abs(freqs - hz)
    peak = float(power[off <= 1.5].max())
    near = float(np.median(power[(off >= 4) & (off <= 12)]))
    return 10 * np.log10(max(peak, 1e-20) / max(near, 1e-20))


def hum(samples: np.ndarray) -> dict:
    """Mains hum: per mains frequency, how far its harmonics stand over neighbouring bins; found when one of the first
    three stands HUM_DB over. The notches: the fundamental plus harmonics that stand HARMONIC_DB over."""
    spectrum = _spectrum(samples)
    if spectrum is None:
        return {'found': False, 'hz': None, 'prominence_db': None, 'harmonics': [], 'note': 'under 0.6 s: not measured'}
    best = None
    for mains in MAINS:
        harmonics = [{'hz': mains * k, 'prominence_db': round(_prominence(spectrum, mains * k), 1)} for k in range(1, 9)]
        top = max(h['prominence_db'] for h in harmonics[:3])
        if best is None or top > best[1]:
            best = (mains, top, harmonics)
    mains, top, harmonics = best
    found = top >= HUM_DB
    notches = [h['hz'] for h in harmonics if h['hz'] == mains or h['prominence_db'] >= HARMONIC_DB][:MAX_NOTCHES] if found else []
    return {'found': found, 'hz': mains if found else None, 'prominence_db': top, 'harmonics': harmonics if found else [],
            'notches': notches}


def clipping(path: Path) -> dict:
    """Samples in flat tops: runs of CLIP_RUN or more at full scale on any channel, read in chunks at the file's rate."""
    info = probe(path)['audio']
    channels = max(1, int(info.get('channels') or 1))
    proc = subprocess.Popen(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0',
                             '-f', 'f32le', '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    clipped, total, run_len, chunk = 0, 0, 0, 4 * channels * 262144
    while True:
        data = proc.stdout.read(chunk)
        if not data:
            break
        frames = np.frombuffer(data[:len(data) // (4 * channels) * 4 * channels], dtype=np.float32).reshape(-1, channels)
        flags = np.abs(frames).max(axis=1) >= FULL_SCALE
        total += len(flags)
        # Lengths of the runs of True, carrying the run that crosses the chunk boundary.
        padded = np.concatenate([[False], flags, [False]])
        edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
        runs = list(edges[1::2] - edges[0::2])
        if runs and flags[0]:
            runs[0] += run_len
        elif run_len >= CLIP_RUN:
            clipped += run_len
        run_len = 0
        if runs and flags[-1]:
            run_len = runs.pop()
        clipped += sum(r for r in runs if r >= CLIP_RUN)
    proc.wait()
    clipped += run_len if run_len >= CLIP_RUN else 0
    return {'clipped_samples': int(clipped), 'clipped_share': round(clipped / total, 6) if total else 0.0, 'clipped': clipped > 0}


def measure_voice(path: Path) -> dict:
    samples = _pcm(path)
    return {**levels(samples), 'hum': hum(samples), **clipping(path), 'duration': round(len(samples) / RATE, 3)}


def busiest(samples: np.ndarray, floor_db: float, speech_db: float, seconds: float) -> float:
    """Start (s) of the `seconds` span with the most speech: windows louder than halfway between floor and speech."""
    db = _window_db(samples)
    span = int(round(seconds * RATE / HOP))
    if len(samples) <= seconds * RATE or len(db) <= span:
        return 0.0
    active = np.concatenate([[0], np.cumsum(db >= (floor_db + speech_db) / 2)])
    counts = active[span:] - active[:-span]
    return round(int(np.argmax(counts)) * HOP / RATE, 3)


# ---------------------------------------------------------------- the filters

def _latency(denoise: str) -> int:
    """Samples of delay afftdn adds at the working rate (ffmpeg 6.1: half its window, 1200 at 48 kHz), measured by
    passing loud noise through the same filter: the chain trims it off so the sound stays on the picture."""
    rate = audio_mod.SAMPLE_RATE
    noise = (np.random.default_rng(7).uniform(-0.5, 0.5, rate) * 32767).astype(np.int16)
    with tempfile.TemporaryDirectory(prefix='rn-clean-lat-') as tmp:
        src = Path(tmp) / 'noise.wav'
        with wave.open(str(src), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(noise.tobytes())
        out = _pcm(src, rate, denoise)
    a = noise.astype(np.float64) / 32768
    n = min(len(a), len(out))
    size = 1 << int(np.ceil(np.log2(2 * n)))
    corr = np.fft.irfft(np.conj(np.fft.rfft(a[:n], size)) * np.fft.rfft(out[:n], size), size)
    lag = int(np.argmax(corr[:n // 2]))  # delays only: out[i] = in[i - lag]
    return lag


def _pre(notches: list[int]) -> list[str]:
    """Rumble and hum: before the denoiser, so its floor is the floor that is left."""
    return [f'highpass=f={HIGHPASS_HZ}:p=2'] + [f'bandreject=f={hz}:t=q:w={NOTCH_Q}' for hz in notches]


def plan(measured: dict, prefiltered: dict, *, eq: bool) -> dict:
    """Everything but the denoise strength: from the measurement of the source and of the source after _pre."""
    pre = _pre(measured['hum'].get('notches') or [])
    threshold_db = max(-60.0, min(-1.0, measured['speech_db'] - COMP_BELOW_SPEECH))
    post = ([EQ] if eq else []) + [DEESSER, f'acompressor=threshold={10 ** (threshold_db / 20):.6f}:ratio=2:attack=10:release=150:'
                                            f'knee=4:makeup=1']
    floor = prefiltered['noise_floor_db']
    return {'pre': pre, 'post': post, 'floor_db': floor, 'speech_db': prefiltered['speech_db'], 'denoise': floor > CLEAN_FLOOR,
            'compressor_threshold_db': round(threshold_db, 1)}


def denoise_filter(p: dict, strength: str) -> str:
    setting = DENOISE[strength]
    nf = min(p['floor_db'] + setting['nf_above'], p['speech_db'] - NF_BELOW_SPEECH)
    nf = max(NF_RANGE[0], min(NF_RANGE[1], nf))
    return f'afftdn=nr={setting["nr"]}:nf={nf:.1f}'


def chain(p: dict, strength: str | None, latency: int = 0) -> str:
    """The -af graph. afftdn delays the sound: pad its end by the delay and cut that much off the head, so every sample
    leaves where it came in (same length, no offset)."""
    steps = list(p['pre'])
    if strength:
        steps.append(denoise_filter(p, strength))
    steps += p['post']
    if latency:
        steps = [f'apad=pad_len={latency}', *steps, f'atrim=start_sample={latency}', 'asetpts=PTS-STARTPTS']
    return ','.join(steps)


# ---------------------------------------------------------------- the clarity check

def default_transcriber(language: str | None, model: str):
    """faster-whisper as rn listen --transcribe runs it, or None when it is not installed. A transcriber takes a WAV path
    and returns {'words': [{'word', 'p', ...}]}; the clarity check names its files check-<variant>.wav."""
    if importlib.util.find_spec('faster_whisper') is None:
        return None
    from .listen import _transcribe
    return lambda wav: _transcribe(Path(wav), language=language, model=model)


def _heard(result: dict) -> tuple[list[str], float | None]:
    from .listen import _tokens
    words = result.get('words') or []
    tokens = [t for w in words for t in _tokens(w['word'])]
    probs = [w['p'] for w in words if w.get('p') is not None]
    return tokens, (round(float(np.mean(probs)), 3) if probs else None)


def _lost(before: list[str], after: list[str]) -> list[str]:
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    return [t for tag, i1, i2, _, _ in matcher.get_opcodes() if tag in ('delete', 'replace') for t in before[i1:i2]]


def _clarity(transcriber, original: Path, render, candidates: list[str], start: float, length: float, work: Path) -> dict:
    """Transcribe the excerpt before and after each candidate strength (strongest first); the first that keeps every
    word with a mean confidence at most CONFIDENCE_DROP lower wins."""
    excerpt = {'start': round(start, 3), 'end': round(start + length, 3)}
    before_wav = work / 'check-original.wav'
    run(['ffmpeg', '-ss', f'{start:.3f}', '-t', f'{length:.3f}', '-i', str(original), '-c:a', 'pcm_f32le', '-y', str(before_wav)])
    try:
        before_tokens, before_p = _heard(transcriber(before_wav))
    except Exception as error:  # no model, no network: clean anyway, say so
        return {'status': 'skipped', 'reason': f'transcription failed ({type(error).__name__}: {str(error)[:120]})', 'excerpt': excerpt}
    if not before_tokens:
        return {'status': 'skipped', 'reason': 'no words heard in the original excerpt', 'excerpt': excerpt}
    tries = []
    for strength in candidates:
        wav = work / f'check-{strength}.wav'
        render(before_wav, wav, strength)
        try:
            after_tokens, after_p = _heard(transcriber(wav))
        except Exception as error:
            return {'status': 'skipped', 'reason': f'transcription failed at {strength} ({type(error).__name__}: {str(error)[:120]})',
                    'excerpt': excerpt}
        lost = _lost(before_tokens, after_tokens)
        drop = round(before_p - after_p, 3) if before_p is not None and after_p is not None else None
        ok = not lost and (drop is None or drop <= CONFIDENCE_DROP)
        tries.append({'strength': strength, 'words': len(after_tokens), 'mean_p': after_p, 'confidence_drop': drop,
                      'lost': lost[:20], 'ok': ok})
        if ok:
            return {'status': 'kept', 'chosen': strength, 'excerpt': excerpt, 'before': {'words': len(before_tokens), 'mean_p': before_p},
                    'tries': tries}
    return {'status': 'lost', 'chosen': candidates[-1], 'excerpt': excerpt, 'before': {'words': len(before_tokens), 'mean_p': before_p},
            'tries': tries}


def _why(clarity: dict | None, denoise: bool, floor: float) -> str:
    if not denoise:
        return f'noise floor {floor} dBFS after rumble and hum filters: already clean, no denoise'
    if clarity is None or clarity['status'] == 'skipped':
        return f'as requested; clarity check skipped: {clarity["reason"] if clarity else "not run"}'
    tries, before = clarity['tries'], clarity['before']

    def fault(t: dict) -> str:
        return (f'{t["strength"]} lost "{" ".join(t["lost"][:5])}"' if t['lost'] else
                f'{t["strength"]} dropped confidence {t["confidence_drop"]}')
    failed = '; '.join(fault(t) for t in tries if not t['ok'])
    if clarity['status'] == 'lost':
        return f'{failed}: words are lost even at light, kept light (judge it with --sample)'
    kept = tries[-1]
    return (f'{failed + "; " if failed else ""}{kept["strength"]} kept all {before["words"]} words '
            f'(confidence {before["mean_p"]} -> {kept["mean_p"]})')


# ---------------------------------------------------------------- the command

def clean_command(source: str, out: str, *, strength: str = 'medium', target: float = -14.0, eq: bool = True, sample: bool = False,
                  language: str | None = None, model: str = 'small', transcriber='auto') -> dict:
    """`transcriber`: 'auto' (faster-whisper when installed), None (skip the clarity check) or a callable (tests)."""
    if strength not in STRENGTHS:
        raise RnError(f'--strength: one of {", ".join(STRENGTHS)}')
    src, out_path = Path(source), Path(out)
    info = probe(src)
    if not info['audio']:
        raise RnError(f'{source}: no audio stream to clean')
    if out_path.resolve() == src.resolve():
        raise RnError('-o must be a new file, not the input')
    suffix = out_path.suffix.lower()
    video = info['video'] if info['video'] and not info['video']['still'] else None
    as_video = bool(video) and suffix not in AUDIO_CODECS
    if as_video and suffix not in VIDEO_EXT:
        raise RnError(f'-o {out}: a video goes to {", ".join(sorted(VIDEO_EXT))} (or {", ".join(sorted(AUDIO_CODECS))} for the sound only)')
    if not as_video and suffix not in AUDIO_CODECS and suffix not in VIDEO_EXT:
        raise RnError(f'-o {out}: one of {", ".join(sorted(AUDIO_CODECS))}')
    if transcriber == 'auto':
        transcriber = default_transcriber(language, model)
    channels = min(2, max(1, int(info['audio'].get('channels') or 1)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rn-clean-') as tmp:
        work = Path(tmp)
        # The first audio stream at the working rate, cut to the length its container declares: ffmpeg 6 decodes an MP4's
        # AAC with the encoder's end padding (16 ms more), which would make the new sound longer than the old.
        original = work / 'original.wav'
        declared = info['audio'].get('duration')
        trim = f',atrim=end_sample={round(declared * audio_mod.SAMPLE_RATE)}' if declared else ''
        run(['ffmpeg', '-i', str(src), '-map', '0:a:0', '-af', f'aresample={audio_mod.SAMPLE_RATE}{trim}', '-ac', str(channels),
             '-c:a', 'pcm_f32le', '-y', str(original)])
        samples = _pcm(original)
        before = {**levels(samples), 'hum': hum(samples), **clipping(original)}
        duration = len(samples) / RATE
        p = plan(before, levels(_pcm(original, af=','.join(_pre(before['hum']['notches'])))), eq=eq)
        latency = _latency(denoise_filter(p, 'strong')) if p['denoise'] else 0

        def render(inp: Path, outp: Path, level: str | None) -> None:
            run(['ffmpeg', '-i', str(inp), '-af', chain(p, level, latency), '-c:a', 'pcm_f32le', '-y', str(outp)])

        clarity = None
        chosen = strength if p['denoise'] else None
        if p['denoise']:
            if transcriber is None:
                clarity = {'status': 'skipped', 'reason': 'faster-whisper is not installed (extra "transcribe")'}
            else:
                length = min(CHECK_SECONDS, duration)
                start = busiest(samples, before['noise_floor_db'], before['speech_db'], length)
                candidates = list(reversed(STRENGTHS[:STRENGTHS.index(strength) + 1]))
                clarity = _clarity(transcriber, original, render, candidates, start, length, work)
                chosen = clarity.get('chosen', strength)
        cleaned = work / 'cleaned.wav'
        render(original, cleaned, chosen)
        # Sound only: straight to -o (an .mp4 from a sound file holds AAC). A video: AAC muxed with the copied picture.
        encoded = work / 'audio.m4a' if as_video else out_path
        loudness = audio_mod.finish_audio(cleaned, encoded, lufs=target, true_peak=TRUE_PEAK, codec=AUDIO_CODECS.get(suffix, audio_mod.AAC),
                                          channels=channels)
        if as_video:
            audio_mod.mux(src, encoded, out_path, frames=None)
        samples_out = None
        if sample:
            samples_out = _samples(original, cleaned, out_path, samples, before, target, channels)
    after_samples = _pcm(out_path)
    after = {**levels(after_samples), **clipping(out_path)}
    spectrum = _spectrum(after_samples) if before['hum']['found'] else None
    if spectrum:
        was = {h['hz']: h['prominence_db'] for h in before['hum']['harmonics']}
        after['hum'] = {'hz': before['hum']['hz'], 'notched': [{'hz': hz, 'before_db': was[hz], 'after_db': round(_prominence(spectrum, hz), 1)}
                                                                for hz in before['hum']['notches']]}
    result = {
        'path': str(out_path), 'source': source, 'video_copied': as_video,
        'before': before, 'after': after, 'gap_gain_db': round(after['gap_db'] - before['gap_db'], 1),
        'filters': {'chain': chain(p, chosen, latency), 'latency_samples': latency, 'denoise': p['denoise'],
                    'denoise_floor_db': p['floor_db'], 'compressor_threshold_db': p['compressor_threshold_db'], 'eq': eq},
        'strength': {'requested': strength, 'chosen': chosen, 'why': _why(clarity, p['denoise'], p['floor_db'])},
        'clarity': clarity,
        'loudness': {'target': target, 'lufs': loudness['lufs'], 'true_peak': loudness['true_peak'], 'gain_db': loudness['gain_db']},
    }
    if as_video:
        out_info = probe(out_path)
        result['duration'] = {'video_in': video['duration'], 'video_out': (out_info['video'] or {}).get('duration'),
                              # Matroska keeps no stream durations: the decoded length then
                              'audio_in': info['audio']['duration'] or round(len(samples) / RATE, 3),
                              'audio_out': (out_info['audio'] or {}).get('duration') or round(len(after_samples) / RATE, 3)}
    if samples_out:
        result['sample'] = samples_out
    return result


def _samples(original: Path, cleaned: Path, out_path: Path, samples: np.ndarray, before: dict, target: float, channels: int) -> dict:
    """10 s before / after from the busiest speech, each brought to the same loudness: compare quality, not volume."""
    length = min(SAMPLE_SECONDS, len(samples) / RATE)
    start = busiest(samples, before['noise_floor_db'], before['speech_db'], length)
    pair = {}
    for name, wav in (('before', original), ('after', cleaned)):
        cut = cleaned.with_name(f'sample-{name}-cut.wav')
        run(['ffmpeg', '-ss', f'{start:.3f}', '-t', f'{length:.3f}', '-i', str(wav), '-c:a', 'pcm_f32le', '-y', str(cut)])
        path = out_path.with_name(f'{out_path.stem}.sample-{name}.wav')
        level = audio_mod.finish_audio(cut, path, lufs=target, true_peak=TRUE_PEAK, codec=AUDIO_CODECS['.wav'], channels=channels)
        pair[name] = {'path': str(path), 'lufs': level['lufs']}
    return {'start': start, 'end': round(start + length, 3), **pair}


def summary_lines(result: dict) -> list[str]:
    before, after, hum_info = result['before'], result['after'], result['before']['hum']
    lines = [f"{result['path']}" + ('  (picture copied, sound replaced)' if result['video_copied'] else ''),
             f"gap speech vs background {before['gap_db']} -> {after['gap_db']} dB (floor {before['noise_floor_db']} -> "
             f"{after['noise_floor_db']} dBFS, speech {before['speech_db']} -> {after['speech_db']} dBFS)"]
    if hum_info['found']:
        fixed = ', '.join(f"{h['hz']} Hz {h['before_db']} -> {h['after_db']} dB" for h in (after.get('hum') or {}).get('notched', []))
        lines.append(f"hum {hum_info['hz']} Hz found (+{hum_info['prominence_db']} dB over neighbours), notched: {fixed}")
    else:
        lines.append(f"no mains hum (strongest 50/60 Hz harmonic +{hum_info['prominence_db']} dB, hum from +{HUM_DB:g})")
    if before['clipped']:
        lines.append(f"warning: the source is clipped ({before['clipped_samples']} samples in flat tops): cleaning cannot restore them")
    s = result['strength']
    lines.append(f"strength {s['chosen'] or 'none (no denoise)'} (requested {s['requested']}): {s['why']}")
    loud = result['loudness']
    lines.append(f"loudness {loud['lufs']} LUFS (target {loud['target']}), true peak {loud['true_peak']} dBTP")
    if result.get('duration'):
        d = result['duration']
        lines.append(f"video {d['video_in']} -> {d['video_out']} s, audio {d['audio_in']} -> {d['audio_out']} s")
    if result.get('sample'):
        sm = result['sample']
        lines.append(f"sample {sm['start']}-{sm['end']}s at the same loudness: {sm['before']['path']} | {sm['after']['path']}")
    return lines
