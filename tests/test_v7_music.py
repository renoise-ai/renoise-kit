"""v0.7.0: rn listen downbeats and rn music fit (a bed that starts on a downbeat and ends on a phrase)."""
import json
import wave

import numpy as np
import pytest

from rn.cli import main
from rn.ff import RnError

librosa = pytest.importorskip('librosa')
from rn.listen import beats_of  # noqa: E402
from rn.music import RATE, _decode, fit_command  # noqa: E402
from rn.agent import PROOF_KB, proof
from types import SimpleNamespace
from pathlib import Path

LEAD, BPM = 0.75, 120
BAR = 4 * 60 / BPM


def song(path, seconds, lead=LEAD, bpm=BPM, rate=44100):
    """A low kick on beat 1 of every bar, a high click on beats 2-4, a held tone underneath; lead seconds of quiet."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    y = 0.15 * np.sin(2 * np.pi * 220 * t) * (t >= lead)
    period, k = 60 / bpm, 0
    while lead + k * period < seconds:
        s = int((lead + k * period) * rate)
        m = min(n - s, int(0.12 * rate))
        tt = np.arange(m) / rate
        y[s:s + m] += (0.9 * np.sin(2 * np.pi * 55 * tt) * np.exp(-tt * 25) if k % 4 == 0
                       else 0.35 * np.sin(2 * np.pi * 2500 * tt) * np.exp(-tt * 60))
        k += 1
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(y, -1, 1) * 32000).astype('<i2').tobytes())
    return path


def on_a_bar(seconds: float, tol: float = 0.08) -> bool:
    phase = (seconds - LEAD) % BAR
    return min(phase, BAR - phase) < tol


@pytest.fixture(scope='module')
def music(tmp_path_factory):
    return song(tmp_path_factory.mktemp('music') / 'song.wav', 20)


def test_listen_finds_the_bars(music):
    grid = beats_of(str(music))
    assert grid['bpm'] == pytest.approx(120, abs=1) and grid['meter'] == 4
    downbeats = grid['downbeats']
    assert len(downbeats) >= 7 and all(on_a_bar(t) for t in downbeats)
    assert np.allclose(np.diff(downbeats), BAR, atol=0.05)


def test_fit_without_render_prints_an_item_on_the_grid(music):
    result = fit_command(str(music), duration=11.3, out=None)
    item = result['edl']
    assert item['out'] - item['in'] == pytest.approx(11.3, abs=1e-3)
    assert on_a_bar(item['in'] + 0.05)  # starts just before a downbeat, so its attack is kept
    assert on_a_bar(item['out'] - item['fade_out'] + 0.05)  # the fade begins on a bar
    assert result['end_on'] == 'phrase' and not result['loops'] and result['verdict'].startswith('clean')


def test_short_music_loops_on_phrases_and_keeps_the_beat(music, tmp_path):
    out = tmp_path / 'bed.wav'
    result = fit_command(str(music), duration=31, out=str(out))
    bed = _decode(str(out))
    assert len(bed) == round(31 * RATE)
    assert np.abs(bed[-480:]).max() < 1e-3  # faded to silence
    assert result['loops'] and all(on_a_bar(loop['to'] + 0.05) and on_a_bar(loop['from'] + 0.05) for loop in result['loops'])
    beats = np.array(beats_of(str(out))['times'])
    assert np.allclose(np.diff(beats), 0.5, atol=0.04)  # no hiccup at the loop point
    shown = proof(SimpleNamespace(command='music', file=str(music), no_proof=False), result)
    assert shown == str(tmp_path / 'bed.proof.jpg') and Path(shown).stat().st_size <= PROOF_KB * 1024


def test_no_render_when_a_loop_is_needed(music):
    with pytest.raises(RnError, match='looped'):
        fit_command(str(music), duration=40, out=None)


def test_music_without_a_pulse_gets_a_plain_fade(tmp_path):
    path = tmp_path / 'wash.wav'  # filtered noise: sound with no pulse (a pure tone fools the tracker's autocorrelation)
    rate = 44100
    noise = np.convolve(np.random.default_rng(1).standard_normal(8 * rate), np.ones(40) / 40, 'same') * 0.5
    tone = (np.clip(noise, -1, 1) * 32000).astype('<i2')
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone.tobytes())
    result = fit_command(str(path), duration=5, out=None)
    assert result['edl'] == {'src': str(path), 'role': 'music', 'in': 0.0, 'out': 5.0, 'fade_out': 1.5}
    assert result['verdict'].startswith('plain')


def test_cli_with_a_listen_result(music, tmp_path, capsys):
    listen = tmp_path / 'l.json'
    assert main(['listen', str(music), '--json', str(listen), '-o', str(tmp_path / 'l.jpg')]) == 0
    assert json.loads(listen.read_text())['beats']['downbeats']
    capsys.readouterr()
    assert main(['music', 'fit', str(music), '--duration', '9', '--listen', str(listen), '--end-on', 'downbeat',
                 '--json', str(tmp_path / 'fit.json')]) == 0
    out = capsys.readouterr().out
    assert 'clean: starts on a downbeat' in out and 'EDL audio item' in out
    fit = json.loads((tmp_path / 'fit.json').read_text())
    assert fit['edl']['out'] - fit['edl']['in'] == pytest.approx(9, abs=1e-3)
