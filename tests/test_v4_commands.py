"""v0.4.0: compare, assemble --preview, qa sound and frame-rate checks, and the install hint for a missing extra."""
import builtins
import json
import subprocess

from PIL import Image

from conftest import ffmpeg, write_edl
from rn.cli import main
from rn.compare import match_cuts
from rn.ff import probe


def rn(*args) -> int:
    return main([str(a) for a in args])


def test_match_cuts_pairs_nearest_within_tolerance():
    result = match_cuts([1.0, 2.0, 3.0], [1.04, 2.9, 3.1, 5.0])
    assert [p['b'] for p in result['pairs']] == [1.04, None, 2.9]
    assert result['only_b'] == [3.1, 5.0]
    assert result['matched'] == 2 and result['mean_abs_offset'] == 0.07


def test_compare_aligns_times_and_cuts(media, tmp_path, capsys):
    # B is A with the cut 0.25 s later: bars then a grey ramp, against the same pair with a longer first shot.
    a, b = tmp_path / 'a.mp4', tmp_path / 'b.mp4'
    for path, first in ((a, 1.0), (b, 1.25)):
        ffmpeg('-f', 'lavfi', '-i', f'smptehdbars=s=320x240:r=24:d={first}', '-f', 'lavfi', '-i', 'color=c=0x2040c0:s=320x240:r=24:d=1',
               '-filter_complex', '[0:v][1:v]concat=n=2:v=1[v]', '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path))
    out, report, video = tmp_path / 'cmp.jpg', tmp_path / 'cmp.json', tmp_path / 'sbs.mp4'
    assert rn('compare', a, b, '--count', '8', '--cuts', '--video', video, '-o', out, '--json', report) == 0
    result = json.loads(report.read_text())
    assert [p['path'] for p in result['pages']] == [str(tmp_path / 'cmp-p1.jpg'), str(tmp_path / 'cmp-p2.jpg')]
    assert all(p['bytes'] <= 125 * 1024 for p in result['pages'])
    pair = result['cuts']['pairs'][0]
    assert abs(pair['a'] - 1.0) < 0.05 and abs(pair['offset'] - 0.25) < 0.05
    side = probe(video)['video']
    assert (side['width'], side['height']) == (640, 240)
    assert '1 matched' in capsys.readouterr().out


def test_preview_is_half_size_same_frames(media, tmp_path):
    edl = write_edl(media, {
        'output': {'width': 320, 'height': 240, 'fps': 24},
        'tracks': [{'clips': [{'src': 'ramp.mp4', 'in': 0, 'out': 1}]},
                   {'clips': [{'image': 'red.png', 'at': 0, 'duration': 1, 'x': 200, 'y': 100}]}],
    }, 'preview.json')
    out = tmp_path / 'draft.mp4'
    assert rn('assemble', edl, '-o', out, '--preview') == 0
    video = probe(out, exact=True)['video']
    assert (video['width'], video['height'], video['frames']) == (160, 120, 24)
    # The 80x60 red overlay at (200, 100) lands at half size and half position: (100, 50) to (140, 80).
    frame = tmp_path / 'f.png'
    assert rn('frame', out, '--index', 12, '-o', frame) == 0
    image = Image.open(frame).convert('RGB')
    assert image.getpixel((120, 65))[0] > 200 and image.getpixel((120, 65))[1] < 60
    assert image.getpixel((95, 65))[0] < 200 or image.getpixel((95, 65))[1] > 60


def test_qa_flags_dead_air_and_level_jumps(tmp_path):
    clip = tmp_path / 'gap.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=4', '-f', 'lavfi',
           '-i', "aevalsrc='0.3*sin(2*PI*440*t)*(lt(t\\,1.5)+gt(t\\,2))':s=48000:d=4",
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(clip))
    report = tmp_path / 'qa.json'
    rn('qa', clip, '--json', report)
    status = {c['check']: c for c in json.loads(report.read_text())['checks']}
    assert status['dead_air']['status'] == 'WARN' and '1.50-2.00s' in status['dead_air']['detail']
    assert status['level_jumps']['status'] == 'WARN'
    steady = tmp_path / 'steady.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(steady))
    rn('qa', steady, '--json', report)
    status = {c['check']: c['status'] for c in json.loads(report.read_text())['checks']}
    assert status['dead_air'] == 'PASS' and status['level_jumps'] == 'PASS'


def test_missing_extra_names_the_install(media, monkeypatch, capsys):
    real_import = builtins.__import__

    def no_cv2(name, *args, **kwargs):
        if name == 'cv2':
            raise ModuleNotFoundError("No module named 'cv2'", name='cv2')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_cv2)
    assert rn('faces', media / 'ramp.mp4') == 1
    err = capsys.readouterr().err.strip().splitlines()[-1]
    assert err == "rn: error: rn faces needs the Python module cv2 (extra \"faces\"): uv tool install 'renoise-kit[all]'"


def test_package_builds_with_readme_and_extras(tmp_path):
    from pathlib import Path
    kit = Path(__file__).parents[1]
    subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(tmp_path), str(kit)], check=True, capture_output=True)
    wheel = next(tmp_path.glob('renoise_kit-*.whl'))
    import zipfile
    metadata = next(n for n in zipfile.ZipFile(wheel).namelist() if n.endswith('METADATA'))
    text = zipfile.ZipFile(wheel).read(metadata).decode()
    assert 'Provides-Extra: all' in text and 'Provides-Extra: faces' in text and '# renoise-kit' in text


def test_outputs_declare_a_fixed_frame_rate(media, tmp_path):
    # A plain x264 encode leaves fixed_frame_rate_flag=0: qa warns, finish fixes it, assemble never produces it.
    report = tmp_path / 'qa.json'

    def fixed(path) -> str:
        rn('qa', path, '--json', report)
        return {c['check']: c['status'] for c in json.loads(report.read_text())['checks']}['fixed_frame_rate']

    assert fixed(media / 'bars.mp4') == 'WARN'
    finished = tmp_path / 'finished.mp4'
    assert rn('finish', media / 'bars.mp4', '-o', finished) == 0
    assert fixed(finished) == 'PASS'
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': '30000/1001'},
                            'tracks': [{'clips': [{'src': 'ramp.mp4', 'in': 0, 'out': 1}]}]}, 'cfr.json')
    assembled = tmp_path / 'assembled.mp4'
    assert rn('assemble', edl, '-o', assembled) == 0
    assert fixed(assembled) == 'PASS'
