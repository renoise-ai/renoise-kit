import json
import re
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import has_filter, ramp_index, write_edl
from rn import __version__
from rn.cli import main
from rn.ff import probe


def rn(*args: str) -> int:
    return main([str(a) for a in args])


def qa_result(tmp_path: Path, *args: str) -> dict:
    report = tmp_path / 'qa.json'
    rn('qa', *args, '--json', report)
    return json.loads(report.read_text())


def checks(result: dict) -> dict:
    return {c['check']: c['status'] for c in result['checks']}


def frame_at(video: Path, index: int, tmp_path: Path) -> Path:
    out = tmp_path / f'f{index}.png'
    assert rn('frame', video, '--index', index, '-o', out) == 0
    return out


def test_version_matches_template_and_worker():
    template_path = Path(__file__).parents[2] / 'template.ts'
    if not template_path.exists():
        pytest.skip('kit checked out without the template')
    assert re.search(r"version: 'v([\d.]+)'", template_path.read_text()).group(1) == __version__
    # The Worker replaces sandboxes built from any other template, so all three must move together.
    worker = (Path(__file__).parents[3] / 'worker/src/services/mcp-sandbox.ts').read_text()
    assert re.search(r"MEDIA_SANDBOX_TEMPLATE = 'renoise-media:v([\d.]+)'", worker).group(1) == __version__


def test_probe_counts_frames_and_faststart(media):
    info = probe(media / 'ramp.mp4')
    assert info['video']['frames'] == 60
    assert info['video']['fps'] == '24'
    assert info['faststart'] is True
    assert probe(media / 'hot.mp4')['faststart'] is False
    assert probe(media / 'red.png')['video']['still'] is True


@pytest.mark.parametrize('index', [0, 1, 17, 59])
def test_frame_index_is_exact(media, tmp_path, index):
    assert ramp_index(frame_at(media / 'ramp.mp4', index, tmp_path)) == index


def test_frame_last_and_time(media, tmp_path):
    last = tmp_path / 'last.png'
    assert rn('frame', media / 'ramp.mp4', '--last', '-o', last) == 0
    assert ramp_index(last) == 59
    at = tmp_path / 'at.png'
    assert rn('frame', media / 'ramp.mp4', '--time', 1.0, '-o', at) == 0
    assert ramp_index(at) == 24
    assert rn('frame', media / 'ramp.mp4', '--index', 60, '-o', tmp_path / 'none.png') == 1


def test_sheet_pages_fit_budget(media, tmp_path):
    out = tmp_path / 'sheet.jpg'
    report = tmp_path / 'sheet.json'
    assert rn('sheet', media / 'ramp.mp4', '--every', 1, '-o', out, '--json', report) == 0
    pages = json.loads(report.read_text())['pages']
    assert len(pages) == 3  # 60 frames, 24 per page
    assert pages[0]['indices'][:3] == [0, 1, 2] and pages[-1]['indices'][-1] == 59
    assert all(Path(p['path']).stat().st_size <= 125 * 1024 for p in pages)
    assert rn('sheet', media / 'ramp.mp4', '--indices', '3,40', '-o', tmp_path / 'two.jpg', '--json', report) == 0
    assert json.loads(report.read_text())['pages'][0]['indices'] == [3, 40]


def test_assemble_cut_is_frame_exact(media, tmp_path):
    edl = write_edl(media, {
        'output': {'width': 320, 'height': 240, 'fps': 24, 'crf': 1},
        'tracks': [{'clips': [
            {'src': 'ramp.mp4', 'in': {'frame': 10}, 'out': {'frame': 20}},
            {'src': 'ramp.mp4', 'in': {'frame': 40}, 'duration': 0.5},
            {'src': 'ramp.mp4', 'in': {'frame': 2}, 'duration': 0.25, 'speed': 2},
        ]}],
    }, 'cut.json')
    out = tmp_path / 'cut.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    assert probe(out, exact=True)['video']['frames'] == 10 + 12 + 6
    # Cut points land on the requested source frames; 2x speed takes every other frame.
    assert ramp_index(frame_at(out, 0, tmp_path)) == 10
    assert ramp_index(frame_at(out, 9, tmp_path)) == 19
    assert ramp_index(frame_at(out, 10, tmp_path)) == 40
    assert ramp_index(frame_at(out, 22, tmp_path)) == 2
    assert ramp_index(frame_at(out, 23, tmp_path)) == 4
    result = qa_result(tmp_path, out, '--edl', edl)
    assert result['status'] == 'PASS', result


def test_assemble_mixed_sources_transitions_overlay_audio(media, tmp_path):
    edl = write_edl(media, {
        'output': {'width': 360, 'height': 640, 'fps': 30},
        'tracks': [
            {'clips': [
                {'src': 'ramp.mp4', 'in': {'frame': 12}, 'out': 2.5},
                {'src': 'bars.mp4', 'duration': 1.5, 'transition': {'type': 'fade', 'duration': 0.5}},
                {'src': 'silent.mp4', 'duration': 1, 'fit': 'contain'},
                {'image': 'card.png', 'duration': 1, 'transition': {'type': 'wipeleft', 'duration': 0.2}},
                {'color': '#000000', 'duration': 0.5},
            ]},
            {'clips': [{'image': 'red.png', 'at': 1.0, 'duration': 1.0, 'x': 20, 'y': 30, 'w': 100}]},
        ],
        'audio': [
            {'src': 'music.wav', 'role': 'music', 'gain_db': -3, 'fade_out': 1},
            {'src': 'voice.wav', 'role': 'voice', 'at': 0.5},
        ],
        'mix': {'lufs': -14, 'true_peak': -1.0, 'duck_db': 10},
    }, 'mixed.json')
    out = tmp_path / 'mixed.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    expected = 60 + 45 + 30 + 30 + 15 - 15 - 6
    info = probe(out, exact=True)
    assert info['video']['frames'] == expected
    assert (info['video']['width'], info['video']['height']) == (360, 640)
    assert info['faststart'] is True
    assert abs(info['audio']['duration'] - expected / 30) < 0.05
    # The overlay is red inside its window and absent before it.
    inside = Image.open(frame_at(out, 45, tmp_path)).convert('RGB').getpixel((40, 50))
    before = Image.open(frame_at(out, 15, tmp_path)).convert('RGB').getpixel((40, 50))
    assert inside[0] > 200 and inside[1] < 60, inside
    assert not (before[0] > 200 and before[1] < 60), before
    result = qa_result(tmp_path, out, '--edl', edl)
    assert result['status'] == 'PASS', result
    status = checks(result)
    assert status['true_peak'] == 'PASS' and status['loudness'] == 'PASS'
    assert status['black_frames'] == 'PASS', 'the closing colour clip is intended, not flagged'


def test_assemble_holds_short_source_and_warns(media, tmp_path, capsys):
    edl = write_edl(media, {
        'output': {'width': 320, 'height': 240, 'fps': 24, 'crf': 1},
        'tracks': [{'clips': [{'src': 'ramp.mp4', 'in': 2.0, 'duration': 1.0}]}],
    }, 'short.json')
    out = tmp_path / 'short.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    assert 'holding its last frame' in capsys.readouterr().out
    assert probe(out, exact=True)['video']['frames'] == 24
    assert ramp_index(frame_at(out, 23, tmp_path)) == 59


@pytest.mark.parametrize('clips, message', [
    ([{'src': 'missing.mp4'}], 'file not found'),
    ([{'src': 'ramp.mp4', 'transition': {'type': 'fade', 'duration': 0.2}}], 'nothing to transition from'),
    ([{'src': 'ramp.mp4', 'duration': 0.5}, {'src': 'ramp.mp4', 'duration': 0.5, 'transition': {'type': 'fade', 'duration': 1}}],
     'longer than the clips'),
    ([{'src': 'ramp.mp4', 'transition': {'type': 'spin', 'duration': 0.2}}], 'xfade names'),
    ([{'src': 'ramp.mp4', 'duration': 1}, {'src': 'ramp.mp4', 'duration': 0.5, 'transition': {'type': 'fade', 'duration': 0.4}},
      {'src': 'ramp.mp4', 'duration': 1, 'transition': {'type': 'fade', 'duration': 0.4}}], 'overlaps the transition'),
    ([{'src': 'ramp.mp4', 'at': 1}], 'main-track clips play back to back'),
    ([{'src': 'ramp.mp4', 'in': 9}], 'past the end'),
])
def test_assemble_rejects_bad_edls(media, tmp_path, capsys, clips, message):
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': clips}]}, 'bad.json')
    assert rn('assemble', edl, '-o', tmp_path / 'bad.mp4') == 1
    assert message in capsys.readouterr().err


def test_transition_pieces_stay_frame_exact(media, tmp_path):
    # The main track is joined from slot bodies and pre-rendered transitions (bounded memory for long timelines):
    # 24 + 24 + 24 frames with two 6-frame dissolves = 60 frames, and untouched frames keep their source index.
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'src': 'ramp.mp4', 'duration': 1},
        {'src': 'ramp.mp4', 'duration': 1, 'transition': {'type': 'dissolve', 'duration': 0.25}},
        {'src': 'ramp.mp4', 'in': 1, 'duration': 1, 'transition': {'type': 'dissolve', 'duration': 0.25}}]}]}, 'pieces.json')
    out = tmp_path / 'pieces.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    assert probe(out, exact=True)['video']['frames'] == 60
    assert ramp_index(frame_at(out, 10, tmp_path)) == 10, 'first body'
    assert ramp_index(frame_at(out, 24, tmp_path)) == 6, 'middle body starts after its incoming dissolve'
    assert ramp_index(frame_at(out, 59, tmp_path)) == 47, 'last clip runs to its own last frame'


def test_dry_run_prints_timeline(media, capsys):
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [
        {'src': 'ramp.mp4', 'duration': 1}, {'src': 'ramp.mp4', 'duration': 1, 'transition': {'type': 'dissolve', 'duration': 0.25}}]}]},
        'dry.json')
    assert rn('assemble', edl, '--dry-run') == 0
    out = capsys.readouterr().out
    assert '42 frames' in out and 'xfade 6f' in out


@pytest.mark.skipif(not has_filter('subtitles'), reason='ffmpeg built without libass')
def test_assemble_burns_subtitles(media, tmp_path):
    (media / 'caps.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHELLO\n')
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [{'color': '#000000', 'duration': 1}]}],
                            'subtitles': {'file': 'caps.srt', 'force_style': 'Fontsize=40'}}, 'subs.json')
    out = tmp_path / 'subs.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    pixels = Image.open(frame_at(out, 5, tmp_path)).convert('L').getdata()
    assert max(pixels) > 200, 'subtitle text is burned in'


def test_finish_fixes_format_peak_and_faststart(media, tmp_path):
    before = qa_result(tmp_path, media / 'hot.mp4')
    status = checks(before)
    assert before['status'] == 'FAIL'
    assert status['faststart'] == 'FAIL' and status['pix_fmt'] == 'FAIL' and status['true_peak'] == 'FAIL'
    out = tmp_path / 'fixed.mp4'
    assert rn('finish', media / 'hot.mp4', '-o', out) == 0
    after = qa_result(tmp_path, out, '--lufs', '-14', '--expect-frames', '48')
    assert after['status'] == 'PASS', after


def test_qa_flags_black_and_frozen_runs(media, tmp_path):
    clip = tmp_path / 'gaps.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=1', '-f', 'lavfi', '-i', 'color=black:s=320x240:r=24:d=1',
                    '-f', 'lavfi', '-i', 'color=gray:s=320x240:r=24:d=1', '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1[v]',
                    '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-y', str(clip)], check=True)
    result = qa_result(tmp_path, clip, '--expect-frames', '72')
    status = checks(result)
    assert status['frames'] == 'PASS'
    assert status['black_frames'] == 'WARN' and status['frozen_frames'] == 'WARN'
    assert status['audio'] == 'WARN'
    assert qa_result(tmp_path, clip, '--expect-frames', '71')['status'] == 'FAIL'


def luma(video: Path, index: int, crop: str = 'iw:ih:0:0') -> float:
    out = subprocess.run(['ffprobe', '-v', 'error', '-f', 'lavfi', f"movie='{video}',select='eq(n\\,{index})',crop={crop},signalstats",
                          '-show_entries', 'frame_tags=lavfi.signalstats.YAVG', '-of', 'csv=p=0'],
                         capture_output=True, text=True, check=True).stdout.split()
    return float(out[0])


def rms_db(video: Path, start: float, seconds: float) -> float:
    log = subprocess.run(['ffmpeg', '-hide_banner', '-ss', str(start), '-t', str(seconds), '-i', str(video), '-map', '0:a:0',
                          '-af', 'astats=measure_perchannel=none', '-f', 'null', '-'], capture_output=True, text=True).stderr
    value = re.findall(r'RMS level dB: (-?inf|-?[\d.]+)', log)[-1]
    return float('-inf') if 'inf' in value else float(value)


def test_ducked_music_continues_after_the_voice(media, tmp_path):
    edl = write_edl(media, {
        'output': {'width': 320, 'height': 240, 'fps': 24},
        'tracks': [{'clips': [{'color': '#202020', 'duration': 6}]}],
        'audio': [{'src': 'music.wav', 'role': 'music'}, {'src': 'voice.wav', 'role': 'voice', 'at': 0.5, 'duration': 1.5}],
        'mix': {'duck_db': 10},
    }, 'duck.json')
    out = tmp_path / 'duck.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    assert rms_db(out, 3.0, 2.0) > -40, 'the music keeps playing once the voice has ended'


@pytest.mark.parametrize('fps, at_frame, frames', [(24, 2, 12), (30, 7, 15), ('30000/1001', 14, 10)])
def test_overlay_lands_on_its_frames(media, tmp_path, fps, at_frame, frames):
    from fractions import Fraction
    rate = Fraction(str(fps))
    edl = write_edl(media, {
        'output': {'width': 320, 'height': 240, 'fps': fps},
        'tracks': [{'clips': [{'color': '#000000', 'duration': 2}]},
                   {'clips': [{'image': 'red.png', 'at': float(at_frame / rate), 'duration': float(frames / rate), 'x': 0, 'y': 0}]}],
    }, 'olay.json')
    out = tmp_path / 'olay.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    lit = [luma(out, i, '60:40:10:10') > 40 for i in range(at_frame - 1, at_frame + frames + 1)]
    assert lit == [False] + [True] * frames + [False], lit


def test_rgb_sources_are_encoded_bt709(media, tmp_path):
    from PIL import Image
    Image.new('RGB', (64, 64), (255, 0, 0)).save(media / 'red-card.png')
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24},
                            'tracks': [{'clips': [{'color': '#ff0000', 'duration': 0.5}, {'image': 'red-card.png', 'duration': 0.5}]}]},
                    'red.json')
    out = tmp_path / 'red.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    # Pure red is Y 63 in limited-range BT.709 (BT.601 would give 81, a visibly different red once tagged 709).
    assert abs(luma(out, 3) - 63) < 3 and abs(luma(out, 15) - 63) < 3, (luma(out, 3), luma(out, 15))


@pytest.mark.skipif('libvpx-vp9' not in subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True).stdout,
                    reason='ffmpeg built without libvpx')
def test_vp9_alpha_overlay_stays_transparent(media, tmp_path):
    clear = media / 'clear.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=80x60:r=24:d=1,format=yuva420p,colorchannelmixer=aa=0',
                    '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuva420p', '-y', str(clear)], check=True)
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24},
                            'tracks': [{'clips': [{'color': '#0000ff', 'duration': 1}]}, {'clips': [{'src': 'clear.webm', 'at': 0}]}]},
                    'alpha.json')
    out = tmp_path / 'alpha.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    pixel = Image.open(frame_at(out, 5, tmp_path)).convert('RGB').getpixel((20, 20))
    assert pixel[2] > 200 and pixel[0] < 60, f'a fully transparent overlay must not cover the frame: {pixel}'


def test_color_overlay_fills_missing_dimension(media, tmp_path):
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24},
                            'tracks': [{'clips': [{'color': '#000000', 'duration': 0.5}]},
                                       {'clips': [{'color': '#ffffff', 'at': 0, 'duration': 0.5, 'w': 50}]}]}, 'bar.json')
    out = tmp_path / 'bar.mp4'
    assert rn('assemble', edl, '-o', out) == 0
    image = Image.open(frame_at(out, 3, tmp_path)).convert('L')
    assert image.getpixel((20, 200)) > 200 and image.getpixel((100, 200)) < 40


@pytest.mark.parametrize('edl_patch, message', [
    ({'tracks': [{'clips': [{'color': '#000000', 'duration': 1}]}, {'clips': [{'src': 'ramp.mp4', 'audio': True}]}]}, 'overlay clips are silent'),
    ({'tracks': [{'clips': [{'color': 'rgb(1,2,3)', 'duration': 1}]}]}, 'color: expected'),
    ({'tracks': [{'clips': [{'color': '#000000', 'duration': 1}]}], 'subtitles': {'file': 'style.srt', 'force_style': "Fontname='x'"}},
     'force_style'),
])
def test_rejects_unsafe_fields(media, tmp_path, capsys, edl_patch, message):
    (media / 'style.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nx\n')
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, **edl_patch}, 'unsafe.json')
    assert rn('assemble', edl, '-o', tmp_path / 'x.mp4') == 1
    assert message in capsys.readouterr().err


def test_qa_handles_matroska(media, tmp_path):
    mkv = tmp_path / 'clip.mkv'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media / 'bars.mp4'), '-c', 'copy', '-y', str(mkv)], check=True)
    result = qa_result(tmp_path, mkv)
    assert 'av_duration' in checks(result)
