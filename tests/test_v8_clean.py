"""rn clean: voice cleanup that measures first (floor, speech, gap, clipping, hum) and keeps every word."""
import json
import subprocess

import numpy as np
import pytest
from conftest import ffmpeg

from rn import clean
from rn.audio import measure
from rn.cli import main
from rn.ff import probe

# A 'voice' of bursts (230 + 1170 + 2830 Hz and a broadband breath, on ~60 % of the time) over white noise near -45 dBFS.
# The breath is what lines the sound up sample for sample: tones alone repeat every few ms.
VOICE = ('(0.3*(sin(2*PI*230*t)+0.5*sin(2*PI*1170*t)+0.3*sin(2*PI*2830*t))+0.1*(2*random(1)-1))*gt(sin(2*PI*1.3*t)\\,-0.2)'
         '+0.01*(2*random(0)-1)')
HUM = '+0.03*sin(2*PI*50*t)+0.01*sin(2*PI*150*t)'  # 50 Hz mains and its 3rd harmonic


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def takes(tmp_path_factory):
    root = tmp_path_factory.mktemp('clean')
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=25:d=6', '-f', 'lavfi', '-i', f"aevalsrc='{VOICE}{HUM}':s=48000:d=6",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(root / 'talk.mp4'))
    ffmpeg('-f', 'lavfi', '-i', f"aevalsrc='{VOICE}':s=48000:d=6", str(root / 'noisy.wav'))
    # Gated: exact silence between the bursts, nothing to denoise.
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.3*sin(2*PI*230*t)*gt(sin(2*PI*1.3*t)\\,0.2)':s=48000:d=4", str(root / 'gated.wav'))
    return root


def _pcm(path, rate=16000):
    return clean._pcm(path, rate)


def _band_db(samples, hz, rate=16000):
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    return 10 * np.log10(spectrum[np.abs(freqs - hz) <= 1.5].max())


def test_measure_reports_floor_gap_hum_and_clipping(takes):
    hummed = clean.measure_voice(takes / 'talk.mp4')
    assert hummed['hum']['found'] and hummed['hum']['hz'] == 50
    assert 50 in hummed['hum']['notches'] and 150 in hummed['hum']['notches']
    assert hummed['speech_db'] == pytest.approx(-12, abs=1.5)
    assert hummed['gap_db'] < 25  # the hum (-33 dBFS) is the floor
    assert hummed['clipped'] is False
    noisy = clean.measure_voice(takes / 'noisy.wav')
    assert not noisy['hum']['found'] and noisy['hum']['prominence_db'] < clean.HUM_DB
    assert noisy['noise_floor_db'] == pytest.approx(-50, abs=2) and noisy['gap_db'] == pytest.approx(38, abs=2)
    hot = takes / 'hot.wav'
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='1.4*sin(2*PI*230*t)':s=48000:d=1", '-c:a', 'pcm_s16le', str(hot))
    assert clean.clipping(hot)['clipped'] and clean.clipping(hot)['clipped_share'] > 0.1


@pytest.fixture(scope='module')
def video_run(takes, tmp_path_factory):
    out = tmp_path_factory.mktemp('clean-out') / 'talk-clean.mp4'
    report = out.with_suffix('.json')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(clean, 'default_transcriber', lambda language, model: None)  # as without faster-whisper
        assert rn('clean', takes / 'talk.mp4', '-o', out, '--strength', 'strong', '--sample', '--json', report) == 0
    return out, json.loads(report.read_text(encoding='utf-8'))


def test_hum_goes_and_the_gap_grows(takes, video_run):
    out, result = video_run
    before, after = _pcm(takes / 'talk.mp4'), _pcm(out)
    # 50 Hz against the voice's 230 Hz partial, so the loudness change does not count.
    hum_before = _band_db(before, 50) - _band_db(before, 230)
    hum_after = _band_db(after, 50) - _band_db(after, 230)
    assert hum_after < hum_before - 25
    assert result['before']['hum']['found'] and all(h['after_db'] < 6 for h in result['after']['hum']['notched'])
    assert result['after']['gap_db'] > result['before']['gap_db'] + 20
    assert result['gap_gain_db'] == pytest.approx(result['after']['gap_db'] - result['before']['gap_db'], abs=0.11)


def test_loudness_hits_the_target(video_run):
    out, result = video_run
    measured = measure(out)
    assert measured['lufs'] == pytest.approx(-14, abs=1.0)
    assert measured['true_peak'] <= -1.0 + 0.05
    assert result['loudness']['lufs'] == pytest.approx(-14, abs=1.0)


def _video_md5(path):
    lines = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-c', 'copy', '-f', 'framemd5', '-'],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    return [line for line in lines if not line.startswith('#')]


def test_video_is_copied_and_the_sound_stays_in_sync(takes, video_run):
    out, result = video_run
    assert result['video_copied']
    assert _video_md5(out) == _video_md5(takes / 'talk.mp4')  # every video packet byte-identical
    src, made = probe(takes / 'talk.mp4'), probe(out)
    assert made['video']['frames'] == src['video']['frames'] and made['video']['duration'] == src['video']['duration']
    assert made['audio']['duration'] == pytest.approx(src['audio']['duration'], abs=0.001)
    a, b = _pcm(takes / 'talk.mp4', 48000), _pcm(out, 48000)
    n = min(len(a), len(b))
    corr = np.fft.irfft(np.fft.rfft(b[:n], 2 * n) * np.conj(np.fft.rfft(a[:n], 2 * n)))
    lag = int(np.argmax(corr))
    assert min(lag, 2 * n - lag) <= 2  # samples at 48 kHz


def test_sample_pair_at_the_same_loudness_and_skipped_check_reported(video_run, capsys):
    out, result = video_run
    pair = result['sample']
    for name in ('before', 'after'):
        assert measure(pair[name]['path'])['lufs'] == pytest.approx(-14, abs=1.0)
        assert probe(pair[name]['path'])['audio']['duration'] == pytest.approx(pair['end'] - pair['start'], abs=0.01)
    assert result['clarity']['status'] == 'skipped' and 'faster-whisper' in result['clarity']['reason']
    assert result['strength'] == {'requested': 'strong', 'chosen': 'strong', 'why': result['strength']['why']}
    assert 'clarity check skipped' in result['strength']['why']
    assert result['next'][0].startswith('rn qa ') and '--lufs -14' in result['next'][0]


def test_clarity_check_steps_down_until_every_word_survives(takes, tmp_path, capsys):
    heard = []
    words = {'original': (['hello', 'world', 'again'], 0.90),
             'strong': (['hello', 'world'], 0.90),          # loses a word
             'medium': (['hello', 'world', 'again'], 0.87),  # keeps them, but confidence drops 0.03
             'light': (['hello', 'world', 'again'], 0.895)}

    def transcriber(wav):
        variant = wav.stem.removeprefix('check-')
        heard.append(variant)
        tokens, p = words[variant]
        return {'words': [{'word': f' {t}', 'p': p} for t in tokens]}

    out = tmp_path / 'clean.wav'
    result = clean.clean_command(str(takes / 'noisy.wav'), str(out), strength='strong', transcriber=transcriber)
    assert heard == ['original', 'strong', 'medium', 'light']
    assert result['strength']['chosen'] == 'light' and result['clarity']['status'] == 'kept'
    tries = {t['strength']: t for t in result['clarity']['tries']}
    assert tries['strong']['lost'] == ['again'] and not tries['strong']['ok']
    assert tries['medium']['confidence_drop'] == pytest.approx(0.03) and not tries['medium']['ok']
    assert tries['light']['ok']
    assert 'strong lost "again"' in result['strength']['why'] and 'light kept all 3 words' in result['strength']['why']
    assert 'afftdn=nr=10' in result['filters']['chain']
    # Audio in, audio out.
    made = probe(out)
    assert made['video'] is None and made['audio']['codec'] == 'pcm_s24le'
    assert made['audio']['duration'] == pytest.approx(6.0, abs=0.001)
    # The requested strength wins when nothing is lost; light losing words keeps light and says so.
    words['medium'] = (['hello', 'world', 'again'], 0.9)
    assert clean.clean_command(str(takes / 'noisy.wav'), str(out), strength='medium', transcriber=transcriber)['strength']['chosen'] == 'medium'
    words['light'] = words['strong']
    words['medium'] = words['strong']
    lost = clean.clean_command(str(takes / 'noisy.wav'), str(out), strength='medium', transcriber=transcriber)
    assert lost['clarity']['status'] == 'lost' and lost['strength']['chosen'] == 'light'
    assert 'lost even at light' in lost['strength']['why']


def test_gated_sound_is_not_denoised_and_bad_outputs_are_refused(takes, tmp_path):
    result = clean.clean_command(str(takes / 'gated.wav'), str(tmp_path / 'g.flac'), transcriber=None)
    assert result['strength']['chosen'] is None and 'already clean' in result['strength']['why']
    assert 'afftdn' not in result['filters']['chain'] and result['clarity'] is None
    assert probe(tmp_path / 'g.flac')['audio']['codec'] == 'flac'
    assert rn('clean', takes / 'noisy.wav', '-o', takes / 'noisy.wav') == 1  # never over the input
    assert rn('clean', takes / 'talk.mp4', '-o', tmp_path / 'x.gif') == 1


def test_latency_is_measured_and_compensated():
    # afftdn delays its output; the chain must put every sample back where it was.
    assert clean._latency('afftdn=nr=24:nf=-40') > 0
    p = {'pre': ['highpass=f=80:p=2'], 'post': [], 'floor_db': -50.0, 'speech_db': -12.0}
    graph = clean.chain(p, 'medium', 1200)
    assert graph.startswith('apad=pad_len=1200,') and graph.endswith('atrim=start_sample=1200,asetpts=PTS-STARTPTS')
