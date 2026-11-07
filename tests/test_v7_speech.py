"""v0.7.0: rn listen --speech (voice-take facts, --expect), rn edit EDL silences, and a bitrate ceiling (finish --max-mbps,
EDL output.max_mbps)."""
import json
import os

import pytest
from conftest import ffmpeg, write_edl

from rn.cli import main
from rn.edl import load
from rn.ff import probe
from rn.listen import _compare, _tokens


def rn(*args) -> int:
    return main([str(a) for a in args])


TONE = "0.4*sin(2*PI*200*t)*(between(t\\,0.5\\,1.5)+between(t\\,2.6\\,3.4))"  # speech 0.5-1.5 and 2.6-3.4 in 4 s


@pytest.fixture
def take(tmp_path):
    path = tmp_path / 'take.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=320x240:r=24:d=4', '-f', 'lavfi', '-i', f"aevalsrc='{TONE}':s=48000:d=4",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path))
    return path


def test_speech_span_pauses_and_rate(take, tmp_path, capsys):
    out = tmp_path / 's.json'
    assert rn('listen', take, '--speech', '--expect', "Hello there, world! Don't", '--json', out) == 0
    result = json.loads(out.read_text())
    assert result['speech_start'] == pytest.approx(0.5, abs=0.03)
    assert result['speech_end'] == pytest.approx(3.4, abs=0.03)
    assert result['pause_count'] == 1 and result['pauses'][0]['start'] == pytest.approx(1.5, abs=0.03)
    assert result['longest_pause'] == pytest.approx(1.1, abs=0.05)
    assert result['expect']['tokens'] == 4
    assert result['expect']['per_second'] == pytest.approx(4 / result['speech'], abs=0.01)
    assert [s['start'] for s in result['silences']][0] == 0.0  # the head pad is a silence too
    assert 'add --transcribe' in capsys.readouterr().out


def test_speech_takes_several_files_and_reports_none(take, tmp_path, capsys):
    quiet = tmp_path / 'quiet.wav'
    ffmpeg('-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono', '-t', '1', str(quiet))
    out = tmp_path / 's.json'
    assert rn('listen', take, quiet, '--speech', '--json', out) == 0
    takes = json.loads(out.read_text())['takes']
    assert takes[1]['speech'] == 0 and takes[1]['speech_start'] is None
    assert 'no speech' in capsys.readouterr().out
    assert rn('listen', take, quiet) == 1  # several files only with --speech
    assert rn('listen', take, quiet, '--speech', '--expect', 'hi') == 1  # --expect is one take's text


def test_tokens_and_match_verdict():
    assert _tokens('Hello, WORLD! don’t 你好。') == ['hello', 'world', 'dont', '你', '好']
    words = [{'word': ' Hello', 'start': 0.0, 'end': 0.4}, {'word': ' word', 'start': 0.5, 'end': 0.9}]
    near = _compare(_tokens('hello world'), words, 0.85)
    assert near['match'] == 'mismatch' and near['differences'][0] == {'expected': 'world', 'heard': 'word', 'at': 0.5}
    assert _compare(_tokens('hello world'), words, 0.4)['match'] == 'ok'
    cut_short = _compare(_tokens('一二三四五六'), [{'word': '一二三', 'start': 0, 'end': 1}], 0.85)
    assert cut_short['match'] == 'mismatch' and cut_short['differences'][0]['expected'] == '四五六'


def test_speech_transcript_against_expected(take):
    pytest.importorskip('faster_whisper')
    assert rn('listen', take, '--speech', '--expect', 'hello', '--transcribe') == 0


def talking_edit(directory, take, clip=None):
    clip = {'id': 'c1', 'src': take.name, **(clip or {})}
    return write_edl(directory, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 24},
                                 'tracks': [{'clips': [clip, {'id': 'c2', 'color': 'red', 'duration': 1}]}]}, 'e.json')


def test_silences_become_ranges(take, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rn('listen', take, '--speech', '--json', 'take.json') == 0
    edl = talking_edit(tmp_path, take)
    assert rn('edit', edl, 'silences', 'c1', '--listen', 'take.json') == 0
    ranges = json.loads(edl.read_text())['tracks'][0]['clips'][0]['ranges']
    # head dead air goes at any length (pad 0.1 stays), the 1.1 s pause goes, the tail after the last word goes
    assert ranges == [[pytest.approx(0.39, abs=0.03), pytest.approx(1.61, abs=0.03)], [pytest.approx(2.49, abs=0.03), pytest.approx(3.51, abs=0.03)]]
    assert 'cut' in capsys.readouterr().out
    assert load(edl).duration == pytest.approx(1 + sum(b - a for a, b in ranges), abs=0.1)


def test_silences_keep_gap_min_and_clip_bounds(take, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rn('listen', take, '--speech', '--json', 'take.json')
    edl = talking_edit(tmp_path, take, {'in': 1.0, 'out': 3.0})
    assert rn('edit', edl, 'silences', 'c1', '--listen', 'take.json', '--keep-gap', '0.4') == 0
    ranges = json.loads(edl.read_text())['tracks'][0]['clips'][0]['ranges']
    assert ranges[0][0] == 1.0 and ranges[-1][1] == pytest.approx(3.0)  # never past the clip's own in/out
    assert ranges[0][1] == pytest.approx(1.71, abs=0.03) and ranges[1][0] == pytest.approx(2.39, abs=0.03)
    edl = talking_edit(tmp_path, take)
    assert rn('edit', edl, 'silences', 'c1', '--listen', 'take.json', '--min', '2') == 0
    assert len(json.loads(edl.read_text())['tracks'][0]['clips'][0]['ranges']) == 1  # the pause is shorter than --min


def test_silences_refuse_and_write_nothing(take, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rn('listen', take, '--speech', '--json', 'take.json')
    edl = talking_edit(tmp_path, take)
    before = edl.read_text()
    assert rn('edit', edl, 'silences', 'c2', '--listen', 'take.json') == 1  # a colour has no sound
    (tmp_path / 'bad.json').write_text('{"beats": {}}')
    assert rn('edit', edl, 'silences', 'c1', '--listen', 'bad.json') == 1
    assert edl.read_text() == before
    assert rn('edit', edl, 'batch', 'silences c1 --listen take.json', 'silences c1 --listen take.json --min 0.3 --pad 0') == 0


def grainy(path, seconds=2):
    ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s=640x360:r=30:d={seconds},noise=alls=60:allf=t+u', '-c:v', 'libx264', '-crf', '10',
           '-pix_fmt', 'yuv420p', str(path))


def test_finish_max_mbps_caps_grain(tmp_path, capsys):
    source = tmp_path / 'grain.mp4'
    grainy(source)
    free, capped = tmp_path / 'free.mp4', tmp_path / 'capped.mp4'
    assert rn('finish', source, '-o', free) == 0
    assert rn('finish', source, '-o', capped, '--max-mbps', '2') == 0
    assert 'cap 2.0' in capsys.readouterr().out
    assert os.path.getsize(capped) < os.path.getsize(free) * 0.6
    assert os.path.getsize(capped) * 8 / 2 / 1e6 < 2 * 1.6  # cap x (1 + 1/T), plus the container
    assert rn('finish', source, '-o', capped, '--max-mbps', 'zero') == 1
    assert rn('finish', source, '-o', capped, '--max-mbps', '2', '--copy-video') == 1


def test_edl_output_max_mbps(tmp_path):
    grainy(tmp_path / 'grain.mp4')
    edl = write_edl(tmp_path, {'version': 2, 'output': {'width': 640, 'height': 360, 'fps': 30, 'max_mbps': 1},
                               'tracks': [{'clips': [{'src': 'grain.mp4'}]}]})
    assert rn('assemble', edl, '-o', tmp_path / 'out.mp4') == 0
    assert os.path.getsize(tmp_path / 'out.mp4') * 8 / 2 / 1e6 < 1 * 1.6
    assert probe(str(tmp_path / 'out.mp4'))['video']['frames'] == 60
    bad = write_edl(tmp_path, {'version': 2, 'output': {'width': 640, 'height': 360, 'max_mbps': -1},
                               'tracks': [{'clips': [{'src': 'grain.mp4'}]}]}, 'bad.json')
    assert rn('assemble', bad, '--dry-run') == 1
