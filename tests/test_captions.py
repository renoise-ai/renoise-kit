import json

import pytest

pysubs2 = pytest.importorskip('pysubs2')

from rn.captions import CLOSERS, SAFE_MARGINS, captions_command, summary_lines  # noqa: E402
from rn.ff import RnError  # noqa: E402

NO_LINE_START = set('、。，！？」』）')
NO_LINE_END = set('「『（')


def write_words(tmp_path, words, language):
    path = tmp_path / 'listen.json'
    path.write_text(json.dumps({'words': words, 'language': language}), encoding='utf-8')
    return str(path)


def timed(tokens, step=0.25, start=0.0, space=False):
    return [{'word': (' ' if space else '') + t, 'start': round(start + i * step, 3), 'end': round(start + (i + 1) * step, 3), 'p': 0.9} for i, t in enumerate(tokens)]


def run(tmp_path, words, language, **kw):
    kw.setdefault('width', 1080)
    kw.setdefault('height', 1920)
    result = captions_command(write_words(tmp_path, words, language), script=kw.pop('script', None), faces_json=kw.pop('faces_json', None),
                              out=str(tmp_path / 'cap.ass'), language=None, **kw)
    return result, json.loads((tmp_path / 'cap.json').read_text(encoding='utf-8'))['events']


ZH = '大家好，今天我们来聊一聊「短视频」怎么做才能火。首先，你需要一个好的开头！然后，保持节奏，不要拖沓。最后，记得关注我们（真的）。'
JA = '皆さんこんにちは、今日は動画の作り方についてお話しします。まず、最初の三秒が大切です！次に、テンポよく、無駄をなくしましょう。'
EN = 'Hello everyone, today we are going to talk about how to make short videos that people actually want to watch. First, you need a strong hook! Then keep the pace tight.'


def test_chinese_limits_and_kinsoku(tmp_path):
    result, events = run(tmp_path, timed(list(ZH.replace('。', '。 ').replace(' ', ''))), 'zh')
    assert result['event_count'] == len(events) > 2
    for e in events:
        assert len(e['lines']) <= 2
        for line in e['lines']:
            assert len(line) <= 14
            assert line[0] not in NO_LINE_START and line[-1] not in NO_LINE_END
        assert e['text'].replace(' ', '') == ''.join(e['lines'])
    assert ''.join(e['text'] for e in events) == ZH


def test_japanese_kinsoku_and_durations(tmp_path):
    _, events = run(tmp_path, timed(list(JA), step=0.3), 'ja')
    for e in events:
        assert all(l[0] not in NO_LINE_START and l[-1] not in NO_LINE_END and len(l) <= 14 for l in e['lines'])
        assert 0.6 - 1e-6 <= e['end'] - e['start'] <= 4.0 + 1e-6
    assert ''.join(e['text'] for e in events) == JA


def test_kinsoku_forced_at_line_break(tmp_path):
    text = '一二三四五六七八九十甲乙丙丁」戊'  # a closer would land at a line start
    _, events = run(tmp_path, timed(list(text)), 'zh', max_chars=14)
    for e in events:
        assert all(l[0] not in CLOSERS and l[0] not in NO_LINE_START for l in e['lines'])


def test_english_never_splits_words(tmp_path):
    tokens = EN.split()
    result, events = run(tmp_path, timed(tokens, step=0.3, space=True), 'en')
    assert [w for e in events for w in e['text'].split()] == tokens
    for e in events:
        assert len(e['lines']) <= 2
        assert all(len(l) <= 32 for l in e['lines'])
        assert all(t in tokens for l in e['lines'] for t in l.split())
        assert 0.6 - 1e-6 <= e['end'] - e['start'] <= 4.0 + 1e-6


def test_whisper_subword_pieces_merge(tmp_path):
    words = [{'word': ' Re', 'start': 0, 'end': .2}, {'word': 'noise', 'start': .2, 'end': .6}, {'word': ' rocks', 'start': .6, 'end': 1.0}]
    _, events = run(tmp_path, words, 'en')
    assert events[0]['text'] == 'Renoise rocks'


def test_script_replaces_misheard_brand_keeps_timing(tmp_path):
    heard = ' welcome to rainoise the best editor'.split()
    words = timed(heard, step=0.4, space=True)
    script = tmp_path / 'script.txt'
    script.write_text('Welcome to Renoise, the best editor.', encoding='utf-8')
    result, events = run(tmp_path, words, 'en', script=str(script))
    assert events[0]['text'] == 'Welcome to Renoise, the best editor.'
    renoise = next(w for e in events for w in e['words'] if w['text'].startswith('Renoise'))
    assert (renoise['start'], renoise['end']) == (0.8, 1.2)  # rainoise timing
    stats = result['script_alignment']
    assert stats['match_ratio'] < 1 and stats['unmatched_spans'][0]['script'] == 'Renoise,'
    assert stats['unmatched_spans'][0]['transcript'] == 'rainoise'
    assert any('script' in line for line in summary_lines(result))


def test_script_alignment_chinese_chars(tmp_path):
    heard = '我用瑞诺斯剪视频'
    script = tmp_path / 'script.txt'
    script.write_text('我用Renoise剪视频。', encoding='utf-8')
    result, events = run(tmp_path, timed(list(heard)), 'zh', script=str(script))
    assert events[0]['text'] == '我用Renoise剪视频。'
    word = next(w for w in events[0]['words'] if w['text'] == 'Renoise')
    assert (word['start'], word['end']) == (0.5, 1.25)  # spans the three misheard characters
    assert result['script_alignment']['unmatched_spans']


def faces_file(tmp_path, box, t0=0.0, t1=3.0):
    path = tmp_path / 'faces.json'
    frames = [{'f': i, 't': t, 'faces': [{'box': box}]} for i, t in enumerate([t0 + (t1 - t0) * k / 6 for k in range(7)])]
    path.write_text(json.dumps({'meta': {'width': 1080, 'height': 1920, 'fps': 30}, 'frames': frames}), encoding='utf-8')
    return str(path)


def test_face_in_bottom_band_moves_caption_to_top(tmp_path):
    words = timed('hello there my friend'.split(), step=0.5, space=True)
    _, still = run(tmp_path, words, 'en')
    assert not still[0]['moved_for_face'] and still[0]['position']['band'] == 'bottom'
    y = still[0]['position']['y']
    result, events = run(tmp_path, words, 'en', faces_json=faces_file(tmp_path, [300, y - 50, 400, 300]))
    assert events[0]['moved_for_face'] and events[0]['position']['band'] == 'top'
    assert result['moved_for_faces'][0]['to'] == 'top' and 'face' in result['moved_for_faces'][0]['reason']
    assert '\\an8' in pysubs2.load(result['ass'])[0].text


def test_face_elsewhere_does_not_move(tmp_path):
    words = timed('hello there my friend'.split(), step=0.5, space=True)
    _, events = run(tmp_path, words, 'en', faces_json=faces_file(tmp_path, [300, 400, 300, 300]))
    assert not events[0]['moved_for_face']


def test_safe_areas_differ_by_aspect(tmp_path):
    words = timed('hello there my friend'.split(), step=0.5, space=True)
    vertical, v_events = run(tmp_path, words, 'en', width=1080, height=1920)
    landscape, l_events = run(tmp_path, words, 'en', width=1920, height=1080)
    assert vertical['safe'] == 'tiktok' and landscape['safe'] == 'youtube'
    v_bottom = 1 - (v_events[0]['position']['y'] + v_events[0]['position']['h']) / 1920
    l_bottom = 1 - (l_events[0]['position']['y'] + l_events[0]['position']['h']) / 1080
    assert abs(v_bottom - SAFE_MARGINS['tiktok']['bottom']) < 0.01 and abs(l_bottom - SAFE_MARGINS['youtube']['bottom']) < 0.01
    assert v_bottom > l_bottom
    assert run(tmp_path, words, 'en', safe='reels')[0]['safe'] == 'reels'


def test_ass_has_single_style_and_srt_written(tmp_path):
    result, events = run(tmp_path, timed(list(ZH)), 'zh')
    subs = pysubs2.load(result['ass'])
    assert list(subs.styles) == ['Default'] and len(subs) == len(events)
    assert subs.styles['Default'].fontname == 'Noto Sans CJK SC'
    assert subs.info['PlayResY'] == '1920'
    srt = open(result['srt'], encoding='utf-8').read()
    assert '-->' in srt and srt.startswith('1\n00:00:00,000')
    assert summary_lines(result)[0].startswith('captions: ')


def test_errors(tmp_path):
    with pytest.raises(RnError):
        run(tmp_path, [], 'en')
    with pytest.raises(RnError):
        run(tmp_path, timed(['a']), 'en', safe='bogus')
