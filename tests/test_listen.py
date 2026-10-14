import pytest

pytest.importorskip('librosa')
pytest.importorskip('matplotlib')

from rn.listen import listen_command, summary_lines  # noqa: E402

from conftest import ffmpeg  # noqa: E402

CLICKS = "aevalsrc='sin(2*PI*1500*t)*lt(mod(t\\,0.5)\\,0.03)':s=44100:d=8"


@pytest.fixture(scope='module')
def clicks(tmp_path_factory):
    root = tmp_path_factory.mktemp('listen')
    ffmpeg('-f', 'lavfi', '-i', CLICKS, str(root / 'clicks.wav'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=10:d=8', '-i', str(root / 'clicks.wav'), '-c:v', 'libx264',
           '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(root / 'clicks.mp4'))
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*300*t)*(lt(t\\,3)+gt(t\\,5))':s=44100:d=8", str(root / 'gap.wav'))
    return root


def run_listen(clicks, name, **extra):
    return listen_command(str(clicks / name), out=str(clicks / f'{name}.jpg'), transcribe=False, language=None, model='small', **extra)


def test_click_track_tempo_and_onsets(clicks):
    result = run_listen(clicks, 'clicks.wav')
    beats = result['beats']
    assert beats['clear']
    candidates = [beats['bpm']] + [a['bpm'] for a in beats['alternatives']]
    assert any(abs(c - 120) / 120 < 0.02 for c in candidates)
    gaps = [b - a for a, b in zip(beats['times'], beats['times'][1:])]
    assert gaps and all(abs(g - 0.5 * round(g / 0.5)) < 0.06 for g in gaps)
    near_clicks = [t for t in result['onsets'] if abs(t / 0.5 - round(t / 0.5)) * 0.5 < 0.06]
    assert len(near_clicks) >= 14
    assert result['loudness']['curve'] and result['loudness']['integrated'] < 0
    assert 'bpm' in ' '.join(summary_lines(result))


def test_silence_gap(clicks):
    silences = run_listen(clicks, 'gap.wav')['silences']
    assert any(abs(s['start'] - 3) < 0.15 and abs(s['end'] - 5) < 0.15 for s in silences)


def test_video_input_and_image_budget(clicks):
    result = run_listen(clicks, 'clicks.mp4', max_kb=60)
    assert result['duration'] == pytest.approx(8, abs=0.3)
    assert 0 < result['image']['bytes'] <= 60 * 1024
    assert (clicks / 'clicks.mp4.jpg').stat().st_size == result['image']['bytes']


def test_transcribe_runs(clicks):
    pytest.importorskip('faster_whisper')
    result = listen_command(str(clicks / 'clicks.wav'), out=str(clicks / 't.jpg'), transcribe=True, language='en', model='tiny')
    assert 'words' in result and result['language'] == 'en'
