"""rn listen: audio as facts and one picture (waveform, spectrogram, loudness, onsets, beats, silences, words)."""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
from pathlib import Path

from .audio import measure
from .ff import RnError, probe, run
from .frames import DEFAULT_MAX_KB, _save_under

ANALYSIS_RATE = 22050
CURVE_STEP = 0.5
SILENCE_DB = -50.0  # frame RMS below this, for at least SILENCE_MIN seconds
SILENCE_MIN = 0.3
MAX_EVENTS = 400  # onsets / beats kept in the result; the strongest onsets win when thinned
BEAT_CLEAR = 0.15  # autocorrelation of the onset envelope at the beat period, 0..1


def _short_term_curve(wav: Path) -> list[dict]:
    """Short-term (3 s) loudness every CURVE_STEP seconds from ebur128's frame log (it logs every 0.1 s)."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', str(wav), '-af', 'ebur128',
                             '-f', 'null', '-'], capture_output=True, text=True)
    curve, next_t = [], 0.0
    for match in re.finditer(r't:\s*([\d.]+)\s.*?M:\s*(-?[\d.]+|-inf)\s+S:\s*(-?[\d.]+|-inf)', result.stderr):
        t = float(match.group(1))
        level = match.group(3) if t >= 3 else match.group(2)
        if t + 1e-6 >= next_t and level != '-inf':
            curve.append({'t': round(t, 2), 'lufs': max(float(level), -70.0)})
            next_t = t + CURVE_STEP
    return curve


def _silences(samples, rate: int) -> list[dict]:
    import librosa
    import numpy as np

    hop = 512
    rms = librosa.feature.rms(y=samples, frame_length=2048, hop_length=hop)[0]
    quiet = 20 * np.log10(np.maximum(rms, 1e-9)) < SILENCE_DB
    ranges, start = [], None
    for index, flag in enumerate(list(quiet) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            begin, end = start * hop / rate, min(index * hop / rate, len(samples) / rate)
            if end - begin >= SILENCE_MIN:
                ranges.append({'start': round(begin, 3), 'end': round(end, 3)})
            start = None
    return ranges


def _beats(samples, rate: int, onset_env, hop: int = 512) -> dict:
    import librosa
    import numpy as np

    tempo, frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=rate, hop_length=hop)
    bpm = float(np.atleast_1d(tempo)[0])
    if bpm <= 0 or len(frames) < 3:
        return {'bpm': None, 'alternatives': [], 'times': [], 'clear': False}
    # The tracker's tempo sits on a coarse frame grid (~2 bpm steps); the mean beat interval over the whole span is finer.
    beat_times = librosa.frames_to_time(frames, sr=rate, hop_length=hop)
    bpm = float(60 * (len(beat_times) - 1) / (beat_times[-1] - beat_times[0]))
    autocorr = librosa.autocorrelate(onset_env - onset_env.mean())
    autocorr = autocorr / autocorr[0] if autocorr[0] > 0 else autocorr

    def strength(candidate: float) -> float:
        lag = int(round(60 * rate / (hop * candidate)))
        return float(max(autocorr[max(lag - 1, 0):lag + 2], default=0)) if lag < len(autocorr) else 0.0

    # The BPM octave check: a beat tracker can report half or double the felt pulse, so give all three with
    # the periodicity strength measured at each; the main bpm is the tracker's.
    alternatives = [{'bpm': round(value, 1), 'strength': round(strength(value), 3)} for value in (bpm / 2, bpm * 2)]
    times = librosa.frames_to_time(frames, sr=rate, hop_length=hop)
    return {'bpm': round(bpm, 1), 'strength': round(strength(bpm), 3), 'alternatives': alternatives,
            'times': [round(float(t), 3) for t in times[:MAX_EVENTS]], 'clear': strength(bpm) >= BEAT_CLEAR}


def _transcribe(wav: Path, *, language: str | None, model: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RnError('faster-whisper is not installed; transcription needs it (baked into the sandbox image)')
    import numpy as np
    # Decode here and pass samples: faster-whisper's own file decoding goes through PyAV, whose API it does not pin
    # (av 16 dropped the `metadata_errors` argument it passes).
    pcm = subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-i', str(wav), '-f', 'f32le', '-ac', '1', '-ar', '16000', '-'],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, dtype=np.float32)
    segments, info = WhisperModel(model, device='cpu', compute_type='int8').transcribe(audio, language=language, word_timestamps=True)
    words, texts = [], []
    for segment in segments:
        texts.append({'start': round(segment.start, 3), 'end': round(segment.end, 3), 'text': segment.text.strip()})
        # Whisper's leading space marks a new word in space-delimited languages; rn captions relies on it.
        words.extend({'word': w.word, 'start': round(w.start, 3), 'end': round(w.end, 3), 'p': round(w.probability, 3)}
                     for w in segment.words or [])
    return {'words': words, 'segments': texts, 'language': info.language}


def _draw(result: dict, samples, rate: int, onset_times: list[float]):
    import librosa
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image

    duration = result['duration']
    fig, axes = plt.subplots(4, 1, figsize=(10, 6), sharex=True, gridspec_kw={'height_ratios': [2, 3, 1.5, 0.8]})
    wave, spec, loud, ticks = axes
    times = np.arange(len(samples)) / rate
    step = max(1, len(samples) // 4000)  # min/max envelope keeps spikes visible
    blocks = len(samples) // step
    shaped = samples[:blocks * step].reshape(blocks, step)
    block_t = times[:blocks * step:step]
    wave.fill_between(block_t, shaped.min(axis=1), shaped.max(axis=1), color='#3b6ea5', linewidth=0)
    wave.set_ylabel('amp')
    mel = librosa.power_to_db(librosa.feature.melspectrogram(y=samples, sr=rate, n_mels=96), ref=np.max)
    spec.imshow(mel, aspect='auto', origin='lower', cmap='magma', extent=[0, len(samples) / rate, 0, 96], vmin=-80, vmax=0)
    spec.set_ylabel('mel')
    spec.set_yticks([])
    curve = result['loudness']['curve']
    if curve:
        loud.plot([p['t'] for p in curve], [p['lufs'] for p in curve], color='#d95f02')
    loud.set_ylabel('LUFS')
    ticks.vlines(onset_times, 0.55, 1, color='#1b9e77', linewidth=0.8)
    ticks.vlines(result['beats']['times'], 0, 0.45, color='#e7298a', linewidth=1.2)
    ticks.set_yticks([0.225, 0.775])
    ticks.set_yticklabels(['beat', 'onset'], fontsize=6)
    ticks.set_xlabel('seconds')
    for silence in result['silences']:
        for axis in (wave, loud, ticks):
            axis.axvspan(silence['start'], silence['end'], color='grey', alpha=0.35, linewidth=0)
    ticks.set_xlim(0, duration)
    lufs, bpm = result['loudness']['integrated'], result['beats']['bpm']
    fig.suptitle(f"{Path(result['path']).name}  {duration:.2f}s  {lufs} LUFS  "
                 f"{str(bpm) + ' bpm' if bpm and result['beats']['clear'] else 'no clear pulse'}", fontsize=10)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png', dpi=100)
    plt.close(fig)
    buffer.seek(0)
    return Image.open(buffer).convert('RGB')


def listen_command(path: str, *, out: str | None, transcribe: bool, language: str | None, model: str, max_kb: int = DEFAULT_MAX_KB) -> dict:
    import librosa
    import numpy as np

    info = probe(path)
    if not info['audio']:
        raise RnError(f'{path}: no audio stream')
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'audio.wav'
        run(['ffmpeg', '-i', path, '-vn', '-ac', '1', '-ar', str(ANALYSIS_RATE), '-y', str(wav)])
        samples, rate = librosa.load(str(wav), sr=None, mono=True)
        loudness = measure(wav)
        hop = 512
        onset_env = librosa.onset.onset_strength(y=samples, sr=rate, hop_length=hop)
        onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=rate, hop_length=hop)
        if len(onset_frames) > MAX_EVENTS:  # keep the strongest, back in time order
            onset_frames = np.sort(onset_frames[np.argsort(onset_env[onset_frames])[-MAX_EVENTS:]])
        onset_times = [round(float(t), 3) for t in librosa.frames_to_time(onset_frames, sr=rate, hop_length=hop)]
        result: dict = {
            'path': path,
            'duration': round(len(samples) / rate, 3),
            'sample_rate': info['audio']['sample_rate'],
            'loudness': {'integrated': loudness['lufs'], 'true_peak': loudness['true_peak'], 'curve': _short_term_curve(wav)},
            'silences': _silences(samples, rate),
            'onsets': onset_times,
            'beats': _beats(samples, rate, onset_env),
        }
        if transcribe:
            result.update(_transcribe(wav, language=language, model=model))
        target = Path(out) if out else Path('listen') / f'{Path(path).stem}.jpg'
        target.parent.mkdir(parents=True, exist_ok=True)
        size = _save_under(_draw(result, samples, rate, onset_times), target, max_kb)
        result['image'] = {'path': str(target), 'bytes': size}
    return result


def summary_lines(result: dict) -> list[str]:
    loudness, beats = result['loudness'], result['beats']
    lines = [f"{result['path']}: {result['duration']:.2f}s  {result['sample_rate']} Hz",
             f"loudness {loudness['integrated']} LUFS  true peak {loudness['true_peak']} dBTP"]
    if beats['bpm'] and beats['clear']:
        alternatives = ', '.join(f"{a['bpm']} ({a['strength']})" for a in beats['alternatives'])
        lines.append(f"bpm {beats['bpm']} (strength {beats['strength']}; alternatives {alternatives})  {len(beats['times'])} beats")
    else:
        lines.append('no clear pulse')
    lines.append(f"{len(result['onsets'])} onsets  {len(result['silences'])} silences"
                 + ''.join(f"  {s['start']}-{s['end']}s" for s in result['silences'][:3]))
    if 'words' in result:
        first = ' '.join(w['word'] for w in result['words'][:8])
        lines.append(f"{len(result['words'])} words ({result['language']}): {first}")
    lines.append(f"image {result['image']['path']} ({result['image']['bytes']} bytes)")
    return lines
