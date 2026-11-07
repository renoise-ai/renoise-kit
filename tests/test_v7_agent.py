"""v0.7.0: what rn adds for an agent that cannot watch or listen: rn --help by job, a proof image to look at, next: lines
with the exact next command, and a run-time warning before long work."""
import json
import re
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from conftest import ffmpeg, write_edl
from rn import agent
from rn.cli import _parser, main


def test_help_lists_every_command_once_under_a_job(capsys):
    with pytest.raises(SystemExit):
        main(['--help'])
    text = capsys.readouterr().out
    commands = set(next(a for a in _parser()._actions if a.dest == 'command').choices)
    listed = re.findall(r'^  ([a-z]+) {2,}\S', text, re.M)
    assert sorted(listed) == sorted(commands), 'a command is missing from GROUPS in rn/cli.py, or listed twice'
    assert 'recipes' in text and 'rn shots ref.mp4 --keyframes' in text


def test_estimate_scales_with_duration_pixels_and_every():
    assert agent.estimate_seconds('shots', 10) == pytest.approx(6.0)
    assert agent.estimate_seconds('shots', 20) == pytest.approx(12.0)
    assert agent.estimate_seconds('key', 10, 540, 960) == pytest.approx(7.5)        # a quarter of the pixels
    assert agent.estimate_seconds('key', 10, 160, 90) == agent.estimate_seconds('key', 10, 540, 960)  # floor at a quarter
    assert agent.estimate_seconds('matte', 10, every=5) == pytest.approx(30.0)
    assert agent.estimate_seconds('transcribe', 60, 4000, 4000) == pytest.approx(72.0)  # audio: size does not matter


def test_long_work_warns_on_stderr_and_reports_the_estimate(monkeypatch, capsys):
    monkeypatch.setattr(agent, '_media', lambda path: (120.0, 1080, 1920))
    seconds = agent.warn_if_long(SimpleNamespace(command='shots', file='ref.mp4'))
    assert seconds == 72.0
    assert 'rn: about 72 s for this (120.0 s of 1080x1920 video); in a sandbox run it with background: true' in capsys.readouterr().err
    monkeypatch.setattr(agent, '_media', lambda path: (5.0, 1080, 1920))
    assert agent.warn_if_long(SimpleNamespace(command='shots', file='short.mp4')) == 3.0
    assert capsys.readouterr().err == ''
    assert agent.warn_if_long(SimpleNamespace(command='probe', file='x.mp4')) is None


@pytest.fixture(scope='module')
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp('agent')
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:d=2:r=24', '-pix_fmt', 'yuv420p', str(d / 'src.mp4'))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:d=2:r=24', '-vf', 'eq=saturation=0.3:gamma=1.5', '-pix_fmt', 'yuv420p', str(d / 'ref.mp4'))
    return d


def test_look_far_points_at_grade_and_grade_proves_itself(clips, tmp_path, capsys):
    assert main(['look', str(clips / 'src.mp4'), '--ref', str(clips / 'ref.mp4')]) == 0
    out = capsys.readouterr().out
    assert f'next: rn grade {clips / "src.mp4"} --ref {clips / "ref.mp4"} -o src.cube' in out
    cube = tmp_path / 'src.cube'
    assert main(['grade', str(clips / 'src.mp4'), '--ref', str(clips / 'ref.mp4'), '-o', str(cube), '--json', str(tmp_path / 'g.json')]) == 0
    out = capsys.readouterr().out
    shown = tmp_path / 'src.proof.jpg'
    assert f'proof: {shown}' in out and shown.stat().st_size <= agent.PROOF_KB * 1024
    assert 'next: EDL clip: "vf": "lut3d=file=src.cube"' in out
    result = json.loads((tmp_path / 'g.json').read_text())
    assert result['proof'] == str(shown) and result['next'][0].startswith('EDL clip')
    assert main(['grade', str(clips / 'src.mp4'), '--ref', str(clips / 'ref.mp4'), '-o', str(tmp_path / 'b.cube'), '--no-proof']) == 0
    assert 'proof:' not in capsys.readouterr().out and not (tmp_path / 'b.proof.jpg').exists()


def test_skeleton_then_assemble_then_qa_each_say_what_comes_next(clips, tmp_path, capsys):
    shots = tmp_path / 'shots.json'
    assert main(['shots', str(clips / 'src.mp4'), '--jobs', '1', '--json', str(shots)]) == 0
    assert f'next: rn edit edit.json init --from-shots {shots}' in capsys.readouterr().out
    edl = tmp_path / 'edit.json'
    assert main(['edit', str(edl), 'init', '--from-shots', str(shots)]) == 0
    out = capsys.readouterr().out
    assert f'next: rn edit {edl} set s01 src=gen/s01.mp4' in out
    assert f'next: rn assemble {edl} -o draft.mp4 --placeholders --preview' in out
    draft = tmp_path / 'draft.mp4'
    assert main(['assemble', str(edl), '-o', str(draft), '--placeholders', '--preview']) == 0
    out = capsys.readouterr().out
    assert f'next: rn qa {draft} --edl {edl} --placeholders' in out and f'next: rn sheet {draft} --count 12' in out


def test_qa_names_the_fix(clips, tmp_path, capsys):
    loud = tmp_path / 'loud.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:d=2:r=24', '-f', 'lavfi', '-i', 'sine=f=440:d=2', '-af', 'volume=12dB',
           '-pix_fmt', 'yuv420p', '-shortest', str(loud))
    main(['qa', str(loud), '--lufs', '-14'])
    assert f'next: rn finish {loud} -o loud-fixed.mp4' in capsys.readouterr().out


def test_grid_proof_shows_every_tile(tmp_path, capsys):
    sheet = Image.new('RGB', (420, 210), 'white')
    draw = ImageDraw.Draw(sheet)
    for c in range(2):
        draw.rectangle([10 + c * 205, 10, 200 + c * 205, 200], fill=(60 + 120 * c, 90, 140))
    sheet.save(tmp_path / 'sheet.png')
    assert main(['grid', str(tmp_path / 'sheet.png'), '-o', str(tmp_path / 'tiles')]) == 0
    assert f'proof: {tmp_path / "tiles.proof.jpg"}' in capsys.readouterr().out
    assert (tmp_path / 'tiles.proof.jpg').stat().st_size <= agent.PROOF_KB * 1024


def test_edl_snippets_are_next_lines(tmp_path, capsys):
    item = write_edl(tmp_path, {'version': 2, 'output': {'width': 64, 'height': 64, 'fps': 10},
                                'tracks': [{'clips': [{'color': '#000000', 'duration': 1}]}]})
    assert main(['assemble', str(item), '-o', str(tmp_path / 'x.mp4'), '--dry-run']) == 0
    assert f'next: rn assemble {item} -o {tmp_path / "x.mp4"} --preview' in capsys.readouterr().out
