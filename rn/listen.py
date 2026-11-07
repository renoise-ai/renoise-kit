"""rn listen: audio as facts and one picture (waveform, spectrogram, loudness, onsets, beats, silences, words)."""
from __future__ import annotations

import difflib
import io
import re
import subprocess
import tempfile
import unicodedata
import wave
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
METER = 4  # beats per bar assumed for downbeats


def _short_term_curve(wav: Path) -> list[dict]:
    """Short-term (3 s) loudness every CURVE_STEP seconds from ebur128's frame log (it logs every 0.1 s)."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', '-i', str(wav), '-af', 'ebur128',
                             '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
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


def _beats(samples, rate: int, onset_env, hop: int = 512, limit: int | None = MAX_EVENTS) -> dict:
    import librosa
    import numpy as np

    tempo, frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=rate, hop_length=hop)
    bpm = float(np.atleast_1d(tempo)[0])
    if bpm <= 0 or len(frames) < 3:
        return {'bpm': None, 'alternatives': [], 'times': [], 'clear': False, 'downbeats': []}
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
    phase, confidence = _downbeat_phase(samples, rate, onset_env, frames, hop)
    return {'bpm': round(bpm, 1), 'strength': round(strength(bpm), 3), 'alternatives': alternatives,
            'times': [round(float(t), 3) for t in times[:limit]], 'clear': strength(bpm) >= BEAT_CLEAR,
            'meter': METER, 'downbeat_phase': phase, 'downbeat_strength': confidence,
            'downbeats': [round(float(t), 3) for t in times[phase::METER][:limit]]}


def _downbeat_phase(samples, rate: int, onset_env, frames, hop: int) -> tuple[int, float]:
    """Which of every METER beats is the bar's first: the phase whose beats carry the most low-end attack (kick and
    bass land on the one) plus overall onset. ponytail: assumes 4/4 and a steady bar; a 3/4 waltz or a song that drops a
    beat reads with the wrong phase after the change; a real downbeat tracker (madmom) if that matters."""
    import librosa
    import numpy as np

    low = librosa.onset.onset_strength(y=samples, sr=rate, hop_length=hop, fmax=200, n_mels=32)
    at = np.clip(frames, 0, min(len(low), len(onset_env)) - 1)
    weight = low[at] / max(float(low[at].max()), 1e-9) + 0.5 * onset_env[at] / max(float(onset_env[at].max()), 1e-9)
    scores = [float(weight[p::METER].mean()) if len(weight[p::METER]) else 0.0 for p in range(METER)]
    best = int(np.argmax(scores))
    others = [s for p, s in enumerate(scores) if p != best]
    # 0: the four phases look alike (no audible bar); toward 1: the one stands out.
    confidence = (scores[best] - max(others)) / scores[best] if scores[best] > 0 else 0.0
    return best, round(confidence, 3)


def _transcribe(wav: Path, *, language: str | None, model: str) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RnError('--transcribe needs faster-whisper (extra "transcribe"): uv tool install "renoise-kit[all]"')
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


def beats_of(path: str) -> dict:
    """The beat grid of a file's sound, as rn listen tracks it, every beat kept: {bpm, strength, alternatives, times,
    clear, meter, downbeat_phase, downbeat_strength, downbeats}. For rn shots --beats and rn music fit."""
    import librosa

    if not probe(path)['audio']:
        raise RnError(f'{path}: no audio stream to take beats from')
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'audio.wav'
        run(['ffmpeg', '-i', path, '-vn', '-ac', '1', '-ar', str(ANALYSIS_RATE), '-y', str(wav)])
        samples, rate = librosa.load(str(wav), sr=None, mono=True)
        onset_env = librosa.onset.onset_strength(y=samples, sr=rate, hop_length=512)
        return _beats(samples, rate, onset_env, limit=None)


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


SPEECH_DB = -45.0  # --speech: a 20 ms window (10 ms hop) is quiet below this RMS in dBFS
SPEECH_RATE = 16000
SPEECH_HOP = SPEECH_RATE // 100
SPEECH_WIN = 2 * SPEECH_HOP
MAX_DIFFS = 30
MATCH_MIN = 0.85  # --expect with --transcribe: similarity at or above this is "match": "ok"


def _is_cjk(ch: str) -> bool:
    """Han and kana: written without spaces, so counted and compared by character."""
    return '぀' <= ch <= 'ヿ' or '㐀' <= ch <= '鿿' or '豈' <= ch <= '﫿'


def _tokens(text: str) -> list[str]:
    """Words lowercased without punctuation ("Don't!" -> dont); Han and kana one token per character.
    ponytail: Thai and other unspaced scripts read as one long word; split them by dictionary if takes in them matter."""
    out, word = [], ''
    for ch in unicodedata.normalize('NFKC', text).lower():
        if _is_cjk(ch):
            out += [word] if word else []
            out.append(ch)
            word = ''
        elif unicodedata.category(ch)[0] in 'LNM':
            word += ch
        elif ch in "'’":
            continue  # inside a word: don't = dont, as Whisper may spell either
        else:
            out += [word] if word else []
            word = ''
    return out + ([word] if word else [])


def _joined(tokens: list[str]) -> str:
    return ''.join(tokens) if tokens and all(_is_cjk(t) for t in tokens) else ' '.join(tokens)


def _compare(expected: list[str], words: list[dict], match_min: float) -> dict:
    """The transcript against the intended text, token by token: similarity 0..1 and every stretch that differs."""
    heard, times = [], []
    for w in words:
        for token in _tokens(w['word']):
            heard.append(token)
            times.append(w['start'])
    matcher = difflib.SequenceMatcher(None, expected, heard, autojunk=False)
    diffs = [{'expected': _joined(expected[i1:i2]), 'heard': _joined(heard[j1:j2]),
              'at': times[j1] if j1 < len(times) else (words[-1]['end'] if words else 0.0)}
             for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal']
    ratio = round(matcher.ratio(), 3)
    return {'match': 'ok' if ratio >= match_min else 'mismatch', 'match_min': match_min, 'similarity': ratio, 'heard': _joined(heard), 'differences': diffs[:MAX_DIFFS],
            'differences_total': len(diffs)}


def _speech_one(path: str, *, expect: str | None, pause: float, silence_db: float, transcribe: bool, language: str | None,
                model: str, match_min: float = MATCH_MIN) -> dict:
    import numpy as np

    if not probe(path)['audio']:
        raise RnError(f'{path}: no audio stream')
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'speech.wav'
        run(['ffmpeg', '-i', path, '-vn', '-ac', '1', '-ar', str(SPEECH_RATE), '-c:a', 'pcm_s16le', '-y', str(wav)])
        with wave.open(str(wav)) as handle:
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(np.float64) / 32768
    duration = len(samples) / SPEECH_RATE
    count = max(0, (len(samples) - SPEECH_WIN) // SPEECH_HOP + 1)
    energy = np.concatenate([[0.0], np.cumsum(samples ** 2)])
    starts = np.arange(count) * SPEECH_HOP
    rms = np.sqrt(np.maximum(energy[starts + SPEECH_WIN] - energy[starts], 0) / SPEECH_WIN)
    loud = 20 * np.log10(np.maximum(rms, 1e-9)) >= silence_db
    hop, win = SPEECH_HOP / SPEECH_RATE, SPEECH_WIN / SPEECH_RATE
    # A quiet run of windows a..b-1 is the time no loud window touches: from where window a-1 ends to where window b starts.
    runs, a = [], None
    for i, flag in enumerate([*loud, True]):
        if not flag and a is None:
            a = i
        elif flag and a is not None:
            runs.append((a, i, 0.0 if a == 0 else a * hop + win - hop, duration if i >= count else i * hop))
            a = None
    result: dict = {'path': path, 'duration': round(duration, 3), 'silence_db': silence_db}
    if not loud.any():
        result.update({'speech_start': None, 'speech_end': None, 'speech': 0.0, 'pauses': [], 'pause_count': 0, 'longest_pause': 0.0,
                       'silences': [{'start': 0.0, 'end': round(duration, 3)}] if duration >= SILENCE_MIN else []})
    else:
        first, last = int(np.argmax(loud)), count - 1 - int(np.argmax(loud[::-1]))
        speech_start, speech_end = first * hop, min(last * hop + win, duration)
        inner = [(s, e) for i0, i1, s, e in runs if i0 > 0 and i1 < count]
        pauses = [{'start': round(s, 3), 'end': round(e, 3), 'seconds': round(e - s, 3)} for s, e in inner if e - s >= pause]
        result.update({'speech_start': round(speech_start, 3), 'speech_end': round(speech_end, 3), 'speech': round(speech_end - speech_start, 3),
                       'pauses': pauses, 'pause_count': len(pauses), 'longest_pause': max((p['seconds'] for p in pauses), default=0.0),
                       'silences': [{'start': round(s, 3), 'end': round(e, 3)} for _, _, s, e in runs if e - s >= SILENCE_MIN]})
    if expect is not None:
        tokens = _tokens(expect)
        result['expect'] = {'text': expect, 'tokens': len(tokens),
                            'per_second': round(len(tokens) / result['speech'], 2) if result['speech'] else None}
    if transcribe:
        heard = _transcribe(Path(path), language=language, model=model)
        result.update(heard)
        if expect is not None:
            result['expect'].update(_compare(_tokens(expect), heard['words'], match_min))
    return result


def speech_command(paths: list[str], *, expect: str | None, pause: float, silence_db: float, transcribe: bool,
                   language: str | None, model: str, match_min: float = MATCH_MIN) -> dict:
    """rn listen --speech: per voice take, where the speech starts and ends inside the file (TTS pads both ends), its
    length, the pauses inside it, and with --expect the delivery rate and (with --transcribe) how far the take's words
    are from the intended ones. No librosa: numpy windows over 16 kHz samples."""
    if expect is not None and len(paths) > 1:
        raise RnError('--expect is the text of one take: give one file with it')
    if expect is not None and expect.startswith('@'):
        try:
            expect = Path(expect[1:]).read_text(encoding='utf-8-sig')
        except FileNotFoundError:
            raise RnError(f'--expect {expect}: no such file') from None
    if pause <= 0:
        raise RnError('--pause: seconds > 0')
    takes = [_speech_one(p, expect=expect, pause=pause, silence_db=silence_db, transcribe=transcribe, language=language, model=model,
                         match_min=match_min) for p in paths]
    return takes[0] if len(takes) == 1 else {'takes': takes}


def speech_summary_lines(result: dict) -> list[str]:
    lines = []
    for take in result.get('takes', [result]):
        if take['speech_start'] is None:
            lines.append(f"{take['path']}: {take['duration']:.2f}s  no speech above {take['silence_db']} dBFS")
            continue
        lines.append(f"{take['path']}: {take['duration']:.2f}s  speech {take['speech_start']}-{take['speech_end']}s "
                     f"({take['speech']}s)  {take['pause_count']} pauses, longest {take['longest_pause']}s"
                     + ''.join(f"  {p['start']}-{p['end']}s" for p in take['pauses'][:3]))
        expect = take.get('expect')
        if expect:
            lines.append(f"  expected {expect['tokens']} words/characters: {expect['per_second']} per second of speech")
            if 'similarity' not in expect:
                lines.append('  words not checked: add --transcribe for match ok | mismatch')
            else:
                lines.append(f"  heard: {expect['heard'][:120]}")
                lines.append(f"  match {expect['match']} (similarity {expect['similarity']}, ok from {expect['match_min']})  {expect['differences_total']} differences"
                             + ''.join(f"; at {d['at']}s expected \"{d['expected']}\" heard \"{d['heard']}\"" for d in expect['differences'][:4]))
    return lines


def summary_lines(result: dict) -> list[str]:
    if 'takes' in result or 'speech' in result:
        return speech_summary_lines(result)
    loudness, beats = result['loudness'], result['beats']
    lines = [f"{result['path']}: {result['duration']:.2f}s  {result['sample_rate']} Hz",
             f"loudness {loudness['integrated']} LUFS  true peak {loudness['true_peak']} dBTP"]
    if beats['bpm'] and beats['clear']:
        alternatives = ', '.join(f"{a['bpm']} ({a['strength']})" for a in beats['alternatives'])
        lines.append(f"bpm {beats['bpm']} (strength {beats['strength']}; alternatives {alternatives})  {len(beats['times'])} beats, "
                     f"{len(beats['downbeats'])} bars of {beats['meter']} (downbeat on beat {beats['downbeat_phase']}, strength {beats['downbeat_strength']})")
    else:
        lines.append('no clear pulse')
    lines.append(f"{len(result['onsets'])} onsets  {len(result['silences'])} silences"
                 + ''.join(f"  {s['start']}-{s['end']}s" for s in result['silences'][:3]))
    if 'words' in result:
        first = ' '.join(w['word'] for w in result['words'][:8])
        lines.append(f"{len(result['words'])} words ({result['language']}): {first}")
    lines.append(f"image {result['image']['path']} ({result['image']['bytes']} bytes)")
    return lines
