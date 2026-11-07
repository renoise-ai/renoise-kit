"""rn captions --style: animated ASS styles built from the per-word timing."""
import json
import re

import numpy as np
import pytest

from conftest import ffmpeg, has_filter, write_edl
from rn.ff import RnError

pysubs2 = pytest.importorskip('pysubs2')
from rn.captions import captions_command  # noqa: E402

WORDS = [{'word': f' {w}', 'start': round(0.2 + 0.4 * i, 2), 'end': round(0.5 + 0.4 * i, 2)} for i, w in enumerate('make every frame count today'.split())]


def _caps(tmp_path, **kw):
    words = tmp_path / 'words.json'
    words.write_text(json.dumps({'words': WORDS, 'language': 'en'}), encoding='utf-8')
    result = captions_command(str(words), out=str(tmp_path / 'caps.ass'), width=1080, height=1920, **kw)
    return result, pysubs2.load(str(tmp_path / 'caps.ass'))


def test_plain_stays_as_before(tmp_path):
    result, subs = _caps(tmp_path)
    assert result['style'] == 'plain' and '{' not in ''.join(e.text for e in subs)


def test_karaoke_k_tags_follow_the_words(tmp_path):
    result, subs = _caps(tmp_path, style='karaoke', accent='#00ff00')
    text = ''.join(e.text for e in subs)
    ks = [int(k) for k in re.findall(r'\\k(\d+)', text)]
    assert len(ks) == len(WORDS)  # the caption starts on its first word: no lead-in
    assert ks[:-1] == [40] * (len(WORDS) - 1) and ks[-1] == 30
    style = subs.styles['Default']
    assert (style.primarycolor.r, style.primarycolor.g) == (0, 255) and style.secondarycolor.r == 255


def test_highlight_switches_each_word_on_and_off(tmp_path):
    _, subs = _caps(tmp_path, style='highlight', accent='#ff0000')
    text = subs[0].text
    ons = re.findall(r'\\t\((\d+),\d+,\\1c&H0000FF&\)', text)
    assert len(ons) == len(WORDS) and [int(o) for o in ons][:2] == [0, 400]


@pytest.mark.parametrize('style,tag', [('pop', r'\fscx70'), ('clean', None), ('box', None)])
def test_other_styles(tmp_path, style, tag):
    result, subs = _caps(tmp_path, style=style, upper=True, position='top')
    assert result['font_size'] > 57  # animated styles use larger type than plain (57 px at 1920 high)
    assert 'MAKE' in subs[0].text and r'\an8' in subs[0].text
    if tag:
        assert tag in subs[0].text
    if style == 'box':
        assert subs.styles['Default'].borderstyle == 3


def test_bad_colour_and_style_are_errors(tmp_path):
    with pytest.raises(RnError, match='#rrggbb'):
        _caps(tmp_path, style='karaoke', accent='yellow')
    with pytest.raises(RnError, match='--style'):
        _caps(tmp_path, style='neon')


@pytest.mark.skipif(not has_filter('subtitles'), reason='ffmpeg built without libass')
def test_assemble_burns_the_styled_captions(tmp_path):
    from rn.assemble import assemble_command
    _caps(tmp_path, style='karaoke')
    edl = write_edl(tmp_path, {'output': {'width': 540, 'height': 960, 'fps': 10},
                               'tracks': [{'clips': [{'color': '#202020', 'duration': 2.4}]}], 'subtitles': {'file': 'caps.ass'}})
    assemble_command(str(edl), str(tmp_path / 'out.mp4'), dry_run=False, keep=False)
    ffmpeg('-i', str(tmp_path / 'out.mp4'), '-vf', 'select=eq(n\\,15)', '-frames:v', '1', str(tmp_path / 'f.png'))
    from PIL import Image
    frame = np.asarray(Image.open(tmp_path / 'f.png').convert('RGB')).astype(int)
    lower = frame[frame.shape[0] // 2:]
    assert (lower.max(axis=2) > 200).sum() > 100  # caption pixels over the grey clip
    yellow = (lower[..., 0] > 180) & (lower[..., 1] > 160) & (lower[..., 2] < 90)
    assert yellow.sum() > 20  # words already spoken are in the accent colour
