"""rn qa --edl: cuts_in_speech, main-track hard cuts that land while a clip's own sound is still going (a clipped word)."""
import json

import pytest
from conftest import ffmpeg, write_edl

from rn.cli import main
from rn.edl import load
from rn.qa import cuts_in_speech

# "Words" of 0.3 s every 0.5 s: sound 0.0-0.3, 0.5-0.8, 1.0-1.3 ..., pauses 0.3-0.5, 0.8-1.0, ...
WORDS = "0.4*sin(2*PI*200*t)*lt(mod(t\\,0.5)\\,0.3)"


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def talk(tmp_path_factory):
    root = tmp_path_factory.mktemp('talk')
    ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=320x240:r=25:d=4', '-f', 'lavfi', '-i', f"aevalsrc='{WORDS}':s=48000:d=4",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(root / 'talk.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=320x240:r=25:d=4', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(root / 'tone.mp4'))
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.3*sin(2*PI*330*t)':s=48000:d=4", str(root / 'bed.wav'))
    return root


def edl(root, clips, name, **extra):
    return write_edl(root, {'version': 2, 'output': {'width': 320, 'height': 240, 'fps': 25}, 'tracks': [{'clips': clips}], **extra}, name)


# Cuts at 0.64 s (c1's out 0.64 is inside the word 0.5-0.8), 1.16 s (c2 out 1.92 and c3 in 2.4 both in pauses) and
# 1.64 s (c4's in 3.12 is inside the word 3.0-3.3).
CLIPS = [{'id': 'c1', 'src': 'talk.mp4', 'in': 0, 'out': 0.64}, {'id': 'c2', 'src': 'talk.mp4', 'in': 1.4, 'out': 1.92},
         {'id': 'c3', 'src': 'talk.mp4', 'in': 2.4, 'out': 2.88}, {'id': 'c4', 'src': 'talk.mp4', 'in': 3.12, 'out': 3.8}]


def qa(path, edit, *extra):
    report = path.parent / 'qa.json'
    rn('qa', path, '--edl', edit, '--json', report, *extra)
    result = json.loads(report.read_text(encoding='utf-8'))
    return {c['check']: c for c in result['checks']}, result


def test_flags_only_cuts_inside_a_word(talk, capsys):
    findings, checked, unjudged = cuts_in_speech(load(edl(talk, CLIPS, 'words.json')))
    assert (checked, unjudged) == (3, 0)
    assert [f['at'] for f in findings] == [0.64, 1.64]
    tail, head = findings[0], findings[1]
    assert set(tail) >= {'tail'} and 'head' not in tail and tail['tail']['clip'] == 'c1'
    assert 'tail' not in head and head['head']['clip'] == 'c4' and head['head']['edge'] == pytest.approx(3.12)
    # The nearer pause: c1's word began at 0.5 (pause before it ends at ~0.49), c4's word began at 3.0.
    assert tail['tail']['out'] == pytest.approx(0.49, abs=0.03)
    assert head['head']['in'] == pytest.approx(2.99, abs=0.03)

    edit = edl(talk, CLIPS, 'words.json', mix={'lufs': None, 'cut_fade': 0.01})  # a 10 ms fade does not hide the clip
    out = talk / 'words.mp4'
    assert rn('assemble', edit, '-o', out) == 0
    capsys.readouterr()
    checks, _ = qa(out, edit)
    speech = checks['cuts_in_speech']
    assert speech['status'] == 'WARN' and [c['at'] for c in speech['cuts']] == [0.64, 1.64]
    assert '0.64s c1|c2: c1 cut off mid-sound' in speech['detail'] and 'c4 starts mid-sound' in speech['detail']
    printed = capsys.readouterr().out
    assert 'next: rn edit' in printed and 'set c1 out=0.' in printed and '--unset duration' in printed

    accepted, _ = qa(out, edit, '--accept', 'cuts_in_speech@0.64', '--accept', 'cuts_in_speech@1.64')
    assert accepted['cuts_in_speech']['status'] == 'PASS' and '2 accepted' in accepted['cuts_in_speech']['detail']


def test_transition_muted_clips_and_music_bed_are_not_flagged(talk):
    dissolve = [dict(CLIPS[0]), {**CLIPS[3], 'transition': {'type': 'fade', 'duration': 0.2}}]
    findings, checked, _ = cuts_in_speech(load(edl(talk, dissolve, 'dissolve.json')))
    assert (findings, checked) == ([], 0)  # a transition crossfades the sound: not a hard cut

    # The same in-word edges with the clips' own sound off, under a music bed that sounds across every cut.
    muted = [{**c, 'audio': False} for c in CLIPS]
    edit = edl(talk, muted, 'bed.json', audio=[{'src': 'bed.wav', 'role': 'music'}], mix={'lufs': None})
    assert cuts_in_speech(load(edit)) == ([], 0, 0)
    out = talk / 'bed.mp4'
    assert rn('assemble', edit, '-o', out) == 0
    checks, _ = qa(out, edit)
    assert 'cuts_in_speech' not in checks  # no cut joins clips that play their own sound


def test_edge_threshold_follows_the_floor(tmp_path):
    from rn.listen import SPEECH_DB
    from rn.qa import _edge_in_sound
    clean, noisy = tmp_path / 'clean.wav', tmp_path / 'noisy.wav'
    ffmpeg('-f', 'lavfi', '-i', f"aevalsrc='{WORDS}':s=48000:d=4", str(clean))
    # Pink noise about 30 dB under the words (floor near -41 dBFS): above SPEECH_DB, so a fixed threshold would hear no pause at all.
    ffmpeg('-i', str(clean), '-f', 'lavfi', '-i', 'anoisesrc=a=0.06:c=pink:d=4:r=48000:s=7', '-filter_complex',
           '[0:a][1:a]amix=inputs=2:duration=first:normalize=0', str(noisy))
    for path in (clean, noisy):
        inside = {t: _edge_in_sound(path, t, 0.03)['inside'] for t in (0.25, 0.33, 0.65, 0.9, 2.15)}
        assert inside == {0.25: True, 0.33: False, 0.65: True, 0.9: False, 2.15: True}, path.name
    assert _edge_in_sound(clean, 1.65, 0.03)['threshold'] == SPEECH_DB
    assert _edge_in_sound(noisy, 1.65, 0.03)['threshold'] > SPEECH_DB
    ends_in_word = tmp_path / 'short.wav'
    ffmpeg('-f', 'lavfi', '-i', f"aevalsrc='{WORDS}':s=48000:d=3.7", str(ends_in_word))
    assert _edge_in_sound(ends_in_word, 3.6, 0.03)['inside']
    assert not _edge_in_sound(ends_in_word, 3.69, 0.03)['inside']  # the guard runs past the source's end: nothing cut away


def test_steady_sound_is_not_judged(talk):
    steady = [{'id': 't1', 'src': 'tone.mp4', 'in': 0, 'out': 1.32}, {'id': 't2', 'src': 'tone.mp4', 'in': 2.12, 'out': 3}]
    findings, checked, unjudged = cuts_in_speech(load(edl(talk, steady, 'tone.json')))
    assert (findings, checked, unjudged) == ([], 1, 2)
