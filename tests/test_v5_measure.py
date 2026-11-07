"""v0.5.0 batch 4, measurement and exchange: signal, align, qa clicks and accept, finish --height, text, matte, export."""
import json
import shutil

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from conftest import ffmpeg, write_edl
from rn.cli import main
from rn.ff import probe


def rn(*args) -> int:
    return main([str(a) for a in args])


@pytest.fixture(scope='module')
def flash_click(tmp_path_factory):
    """Grey 3 s at 24 fps with white frames 30-31 and a single-sample impulse at 1.5 s over a steady tone."""
    path = tmp_path_factory.mktemp('signal') / 'flash.mov'
    ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=320x240:r=24:d=3', '-f', 'lavfi',
           '-i', "aevalsrc='0.3*sin(2*PI*220*t)+0.6*eq(trunc(t*48000)\\,72000)':s=48000:d=3",
           '-filter_complex', "[0:v]geq=lum='if(between(N\\,30\\,31)\\,235\\,128)':cb=128:cr=128[v]", '-map', '[v]', '-map', '1:a',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(path))
    return path


def test_signal_series_flashes_and_clicks(flash_click, tmp_path):
    report, image = tmp_path / 'signal.json', tmp_path / 'signal.png'
    assert rn('signal', flash_click, '--json', report, '-o', image) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert result['frames'] == 72 and len(result['video']['luma']) == 72 and len(result['audio']['onset_db']) == 72
    assert result['flashes'] == [30]
    assert result['clicks'] == [pytest.approx(1.5, abs=0.001)]
    onset = result['audio']['onset_db']
    assert max(onset) > 6 and sorted(onset)[36] < 1, 'a steady tone has no onsets; the impulse has one'
    assert image.stat().st_size > 10_000


def test_align_finds_an_excerpt(tmp_path):
    song = tmp_path / 'song.wav'
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*(200+150*sin(2*PI*0.3*t))*t)*(0.5+0.5*sin(2*PI*1.7*t))"
           "+0.3*sin(2*PI*90*t)*exp(-6*mod(t\\,0.61))':s=48000:d=20", str(song))
    excerpt, other = tmp_path / 'cut.wav', tmp_path / 'noise.wav'
    ffmpeg('-ss', '7.315', '-t', '8', '-i', str(song), '-af', 'volume=-6dB', str(excerpt))
    ffmpeg('-f', 'lavfi', '-i', 'anoisesrc=a=0.3:d=8:c=pink', str(other))
    report = tmp_path / 'align.json'
    assert rn('align', song, excerpt, '--json', report) == 0
    found = json.loads(report.read_text(encoding='utf-8'))
    assert found['offset'] == pytest.approx(7.315, abs=0.002) and found['match'] > 0.9
    assert rn('align', song, other, '--json', report) == 0
    assert json.loads(report.read_text(encoding='utf-8'))['match'] < 0.2


def test_qa_clicks_and_accept(flash_click, media, tmp_path):
    clip = tmp_path / 'click.mp4'
    ffmpeg('-i', str(flash_click), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', str(clip))
    report = tmp_path / 'qa.json'

    def checks(*extra):
        rn('qa', clip, '--json', report, *extra)
        return {c['check']: c for c in json.loads(report.read_text(encoding='utf-8'))['checks']}

    assert checks()['clicks']['status'] == 'WARN'
    accepted = checks('--accept', 'clicks@1.5', '--accept', 'color')
    assert accepted['clicks']['status'] == 'PASS' and '1 accepted' in accepted['clicks']['detail']
    assert accepted['color']['status'] == 'PASS' and accepted['color']['accepted'] == 'command line'
    assert checks('--accept', 'clicks@0.5')['clicks']['status'] == 'WARN'
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [{'color': 'gray', 'duration': 3}]}],
                            'accept': [{'check': 'clicks', 'at': 1.5, 'note': 'the snap'}]}, 'accept.json')
    rn('qa', clip, '--edl', edl, '--json', report)
    assert {c['check']: c['status'] for c in json.loads(report.read_text(encoding='utf-8'))['checks']}['clicks'] == 'PASS'


def test_finish_height(media, tmp_path):
    out = tmp_path / 'small.mp4'
    assert rn('finish', media / 'bars.mp4', '-o', out, '--height', 360) == 0
    video = probe(out)['video']
    assert (video['width'], video['height']) == (640, 360)


@pytest.mark.skipif(not shutil.which('tesseract'), reason='needs tesseract')
def test_text_reads_a_title(tmp_path):
    from rn.edl import _font
    card = Image.new('RGB', (640, 360), (20, 20, 20))
    ImageDraw.Draw(card).text((60, 140), 'RENOISE 2026', fill=(255, 255, 255), font=_font(64))
    card.save(tmp_path / 'title.png')
    clip = tmp_path / 'title.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=black:s=640x360:r=24:d=2', '-loop', '1', '-t', '1', '-i', str(tmp_path / 'title.png'),
           '-filter_complex', '[0:v][1:v]overlay=enable=\'between(t,0.5,1.4)\'', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))
    report = tmp_path / 'text.json'
    assert rn('text', clip, '--lang', 'eng', '--json', report) == 0
    spans = json.loads(report.read_text(encoding='utf-8'))['spans']
    title = next(s for s in spans if 'RENOISE' in s['text'])
    # Read twice a second: on screen 0.5-1.4 s is seen at 0.5 and 1.0, so the span ends at the 1.5 s read that misses it.
    assert title['start'] == pytest.approx(0.5, abs=0.05) and title['end'] == pytest.approx(1.5, abs=0.05)
    style = title['style']
    assert _near(style['color'], (255, 255, 255)) and _near(style['background'], (20, 20, 20)) and style['outline'] is None
    assert style['height_px'] == title['box'][3] and style['height_share'] == round(title['box'][3] / 360, 4)


def _near(colour: str | None, rgb: tuple[int, int, int], tolerance: float = 40) -> bool:
    return colour is not None and float(np.linalg.norm([int(colour[i:i + 2], 16) - c for i, c in zip((1, 3, 5), rgb)])) <= tolerance


def _title_card(fill, background, outline=None, size=(640, 200)) -> tuple[Image.Image, list[int]]:
    """One line of type on a flat colour and its tight box [x, y, w, h] (glyphs and outline), as tesseract gives one."""
    font = ImageFont.load_default(size=64)
    card = Image.new('RGB', size, background)
    draw = ImageDraw.Draw(card)
    stroke = 5 if outline else 0
    draw.text((40, 50), 'RENOISE 26', fill=fill, font=font, stroke_width=stroke, stroke_fill=outline)
    x0, y0, x1, y1 = draw.textbbox((40, 50), 'RENOISE 26', font=font, stroke_width=stroke)
    return card, [x0, y0, x1 - x0, y1 - y0]


STYLE_CASES = {  # fill, background, outline
    'white on black': ((255, 255, 255), (0, 0, 0), None),
    'black on yellow': ((0, 0, 0), (255, 220, 0), None),
    'yellow on blue': ((255, 230, 0), (20, 40, 220), None),  # 4:2:0 greys the edges: still no outline
    'white, black outline, grey': ((255, 255, 255), (128, 128, 128), (0, 0, 0)),
    'white, black outline, blue': ((255, 255, 255), (30, 90, 200), (0, 0, 0)),
    'black, white outline, orange': ((0, 0, 0), (230, 120, 30), (255, 255, 255)),
}


@pytest.mark.parametrize('codec', ['png', 'h264'])
@pytest.mark.parametrize('case', list(STYLE_CASES))
def test_text_style_colours(case, codec, tmp_path):
    from rn.text import _style
    fill, background, outline = STYLE_CASES[case]
    card, _ = _title_card(fill, background, outline)
    if codec == 'h264':
        card.save(tmp_path / 'card.png')
        ffmpeg('-i', str(tmp_path / 'card.png'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(tmp_path / 'card.mp4'))
        ffmpeg('-i', str(tmp_path / 'card.mp4'), '-frames:v', '1', str(tmp_path / 'decoded.png'))
        card = Image.open(tmp_path / 'decoded.png').convert('RGB')
    style = _style(np.asarray(card))
    assert _near(style['color'], fill), style
    assert _near(style['background'], background), style
    assert style['outline'] is None if outline is None else _near(style['outline'], outline), style


def test_text_style_box_and_nothing_to_read():
    from rn.text import _span_style, _style
    card, box = _title_card((255, 255, 255), (0, 0, 0), size=(640, 400))
    style = _span_style(np.asarray(card), box)
    assert style['height_px'] == box[3] and style['height_share'] == round(box[3] / 400, 4)
    assert _near(style['color'], (255, 255, 255)) and _near(style['background'], (0, 0, 0))
    assert _style(np.full((40, 200, 3), 90, np.uint8)) == {'color': None, 'background': '#5a5a5a', 'outline': None}


def test_text_style_is_from_the_first_read(tmp_path, monkeypatch):
    """No tesseract needed: a stand-in OCR reads one line wherever there is light type. The title is white for the first
    second and yellow for the next; its one span keeps the style of the frame where it was first read."""
    import rn.text as text
    for name, fill in (('white', (255, 255, 255)), ('yellow', (255, 220, 0))):
        _title_card(fill, (20, 20, 20), size=(640, 360))[0].save(tmp_path / f'{name}.png')
    clip = tmp_path / 'title.mp4'
    ffmpeg('-loop', '1', '-t', '1', '-r', '24', '-i', str(tmp_path / 'white.png'), '-loop', '1', '-t', '1', '-r', '24',
           '-i', str(tmp_path / 'yellow.png'), '-filter_complex', '[0:v][1:v]concat=n=2:v=1[v]', '-map', '[v]',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))

    def read(png, lang, min_conf):
        ys, xs = np.nonzero(np.asarray(Image.open(png).convert('L')) > 100)
        return [{'text': 'RENOISE 26', 'box': [int(xs.min()), int(ys.min()), int(np.ptp(xs)) + 1, int(np.ptp(ys)) + 1], 'conf': 90.0}]

    monkeypatch.setattr(text, 'languages', lambda: ['eng'])
    monkeypatch.setattr(text, '_read', read)
    result = text.text_command(str(clip), every=None, lang=None, min_conf=50)
    assert [s['f'] for s in result['samples']] == [0, 12, 24, 36]
    [span] = result['spans']
    assert (span['first_frame'], span['last_frame']) == (0, 36)
    style = span['style']
    assert _near(style['color'], (255, 255, 255)) and not _near(style['color'], (255, 220, 0)), style
    assert _near(style['background'], (20, 20, 20)) and style['outline'] is None
    assert style['height_px'] == span['box'][3] and style['height_share'] == round(span['box'][3] / 360, 4)
    assert all('style' not in line for sample in result['samples'] for line in sample['lines'])
    assert style['color'] in text.summary_lines(result)[1]


def test_matte_masks_the_subject(tmp_path):
    pytest.importorskip('rembg')
    frame = Image.new('RGB', (320, 240), (200, 210, 220))
    ImageDraw.Draw(frame).ellipse((110, 60, 210, 180), fill=(180, 40, 30))
    frame.save(tmp_path / 'disk.png')
    clip = tmp_path / 'disk.mp4'
    ffmpeg('-loop', '1', '-t', '0.5', '-r', '24', '-i', str(tmp_path / 'disk.png'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))
    assert rn('matte', clip, '-o', tmp_path / 'matte', '--every', 6) == 0
    masks = sorted((tmp_path / 'matte').glob('*.png'))
    assert len(masks) == 12
    mask = Image.open(masks[0]).convert('L')
    assert mask.getpixel((160, 120)) > 200 and mask.getpixel((10, 10)) < 50


def test_export_otio_fcpxml_xml(media, tmp_path):
    otio = pytest.importorskip('opentimelineio')
    write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [{'clips': [{'src': 'ramp.mp4', 'out': 1}]}]}, 'sub.json')
    edl = write_edl(media, {'output': {'width': 320, 'height': 240, 'fps': 24}, 'tracks': [
        {'clips': [{'id': 'a', 'src': 'ramp.mp4', 'out': 1, 'meta': {'take': 2}},
                   {'id': 'b', 'src': 'ramp.mp4', 'in': 0.5, 'out': 2, 'speed': 2, 'transition': {'type': 'fade', 'duration': 0.25}},
                   {'id': 'n', 'src': 'sub.json', 'in': 0.25}]},
        {'clips': [{'id': 't', 'image': 'red.png', 'at': 0.5, 'duration': 1, 'x': 10, 'y': 20, 'blend': 'screen'},
                   {'color': 'white', 'w': 10, 'h': 10, 'at': 1.0, 'duration': 1}]}],
        'audio': [{'id': 'm', 'src': 'music.wav', 'role': 'music', 'gain_db': -6}]}, 'export.json')
    out = tmp_path / 'cut.otio'
    report = tmp_path / 'export.json'
    assert rn('export', edl, '-o', out, '--json', report) == 0
    timeline = otio.adapters.read_from_file(str(out))
    from rn.edl import load
    total = load(edl).total_frames
    assert round(timeline.tracks[0].duration().value) == total
    names = [t.name for t in timeline.tracks]
    assert names == ['V1', 'V2', 'V3', 'A1']  # the overlays overlap in time: two lanes
    clips = list(timeline.tracks[0].find_clips())
    assert clips[0].metadata['rn']['meta'] == {'take': 2}
    assert any(isinstance(e, otio.schema.LinearTimeWarp) for e in clips[1].effects)
    assert any(isinstance(item, otio.schema.Stack) for item in timeline.tracks[0])
    assert any(l['clip'] == 't' for l in json.loads(report.read_text(encoding='utf-8'))['lost'])
    for suffix in ('.fcpxml', '.xml'):
        target = tmp_path / f'cut{suffix}'
        assert rn('export', edl, '-o', target) == 0 and target.stat().st_size > 500


def test_finish_takes_a_separate_mix_and_an_offset(media, tmp_path):
    # A renderer's MP4 whose sound runs 0.25 s late, plus its separate WAV mix: --audio uses the WAV, --audio-offset fixes the MP4.
    late = tmp_path / 'late.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2', '-f', 'lavfi',
           '-i', "aevalsrc='0.5*sin(2*PI*440*t)*gte(t\\,0.25)':s=48000:d=2", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(late))
    mix = tmp_path / 'mix.wav'
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*440*t)':s=48000:d=2", str(mix))
    from rn.signal import pcm
    import numpy as np

    def onset(path) -> float:
        samples = np.abs(pcm(path))
        return float(np.argmax(samples > 0.05) / 48000)

    for n, args in enumerate((['--audio', mix], ['--audio-offset', '-0.25'])):
        out = tmp_path / f'fixed{n}.mp4'
        assert rn('finish', late, '-o', out, '--lufs', 'keep', *args) == 0
        assert onset(out) < 0.03
        assert probe(out, exact=True)['video']['frames'] == 48


def test_sheet_of_a_folder_of_stills(tmp_path):
    stills = tmp_path / 'stills'
    stills.mkdir()
    for n in (792, 1062, 1500):
        Image.new('RGB', (640, 360), (n % 255, 80, 160)).save(stills / f'f{n}.png')
    report = tmp_path / 'sheet.json'
    assert rn('sheet', stills, '-o', tmp_path / 'stills.jpg', '--json', report) == 0
    page = json.loads(report.read_text(encoding='utf-8'))['pages'][0]
    assert page['files'] == ['f792.png', 'f1062.png', 'f1500.png'] and page['bytes'] <= 125 * 1024
