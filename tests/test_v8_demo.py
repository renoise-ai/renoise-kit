"""rn capture URL --demo: a scripted, frame-exact product demo of a live page (needs Playwright + Chromium)."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from rn.cli import main
from rn.demo import glide_ms, load_demo
from rn.ff import RnError, probe

# A small app: a text field, a small magenta "Add" button that appends a green row, a page long enough to scroll,
# and a blue "Bottom" section far down.
APP = '''<!doctype html><meta charset="utf-8"><style>
body{margin:0;font:18px sans-serif;background:#f4f4f6;height:1600px}
#task{position:absolute;left:40px;top:60px;width:240px;height:28px;font-size:18px}
#add{position:absolute;left:320px;top:60px;width:48px;height:30px;border:0;background:#ff2299;color:#fff}
#list{position:absolute;left:40px;top:120px;margin:0;padding:0;list-style:none}
#list li{width:300px;height:36px;margin-bottom:6px;background:#20b050;color:#fff}
#bottom{position:absolute;left:0;right:0;top:1200px;height:400px;background:#2040d0}
#go{margin:40px;font-size:18px}
</style>
<input id="task" placeholder="New task"><button id="add">Add</button><ul id="list"></ul>
<section id="bottom"><button id="go">Bottom</button></section>
<script>
document.getElementById('add').onclick = () => {
  const li = document.createElement('li');
  li.textContent = document.getElementById('task').value || 'item';
  document.getElementById('list').append(li);
};
</script>'''

SIZE = (960, 540)  # 640x360 CSS px at 1.5x: small and quick


def frames(video: Path) -> np.ndarray:
    import subprocess
    info = probe(str(video), exact=True)['video']
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, info['height'], info['width'], 3)


def share(frame: np.ndarray, rgb: tuple[int, int, int], tol: int = 40) -> np.ndarray:
    return np.all(np.abs(frame.astype(int) - rgb) <= tol, axis=-1)


def width_of(frame: np.ndarray, rgb) -> int:
    columns = np.nonzero(share(frame, rgb).sum(axis=0) > 3)[0]
    return int(columns.max() - columns.min() + 1) if len(columns) else 0


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding='utf-8')
    return path


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    pytest.importorskip('playwright')
    from rn.demo import demo_command
    root = tmp_path_factory.mktemp('demo')
    page = write(root / 'app.html', APP)
    url = page.resolve().as_uri()
    try:
        demo_command(url, demo=str(write(root / 'probe.json', {'steps': [{'pause': 40}]})), out=str(root / 'probe'), size=SIZE)
    except RnError as error:
        if 'Executable' in str(error) or 'install' in str(error):
            pytest.skip(f'no Chromium for Playwright: {error}')
        raise
    return url


def test_the_script_is_checked_before_a_browser_starts(tmp_path):
    good = load_demo(write(tmp_path / 'ok.json', {'setup': [{'localStorage': {'plan': 'pro'}}, {'reload': True}],
                                                   'steps': [{'click': {'role': 'button', 'name': 'Add'}},
                                                             {'type': 'hi', 'into': {'placeholder': 'Task'}, 'hold': 200},
                                                             {'wait': 300}, {'zoom': 'off'}, {'caption': 'x'}]}))
    assert len(good['steps']) == 5
    for steps, needle in (([{'pause': 10}, {'click': 'a', 'press': 'Enter'}], 'step 2: one action per step'),
                          ([{'clack': '#a'}], 'step 1: one action'),
                          ([{'pause': 10}, {'pause': 5}, {'click': {'role': 'button', 'text': 'x'}}], 'step 3: a target'),
                          ([{'scroll': 'down'}], 'step 1: "scroll" takes pixels'),
                          ([{'press': 'Enter', 'into': '#a'}], 'step 1: "press" does not take into')):
        with pytest.raises(RnError, match=needle):
            load_demo(write(tmp_path / 'bad.json', {'steps': steps}))
    with pytest.raises(RnError, match='setup step 1'):
        load_demo(write(tmp_path / 'bad.json', {'setup': [{'pause': 10}], 'steps': [{'pause': 10}]}))
    assert glide_ms(0, 1000) == 0 and glide_ms(50, 1000) >= 400 and glide_ms(5000, 1000) == 900


def test_frame_count_is_exact_for_the_step_timings(app, tmp_path):
    from rn.demo import demo_command
    script = write(tmp_path / 'd.json', {'steps': [{'pause': 1000}, {'caption': 'Hello'}, {'pause': 50}, {'wait': 400}]})
    result = demo_command(app, demo=str(script), out=str(tmp_path / 'out'), size=SIZE, fps=24)
    # 1050 ms at 24 fps: frames at 0, 41.7, ... the last one before 1050 ms is #25, so 26 frames; "wait" is off camera.
    assert result['video']['frames'] == 26 == probe(result['video']['path'], exact=True)['video']['frames']
    assert [(s['start'], s['end']) for s in result['steps']] == [(0, 1.0), (1.0, 1.0), (1.0, 1.05), (1.05, 1.05)]
    assert result['captions'] == [{'text': 'Hello', 'start': 1.0, 'end': 1.05, 'step': 2}]
    assert '00:00:01,000 --> 00:00:01,050' in Path(result['srt']).read_text(encoding='utf-8')
    assert json.loads(Path(result['timeline']).read_text(encoding='utf-8'))['video']['frames'] == 26


def test_typing_and_click_change_the_page_and_the_target_below_is_scrolled_to(app, tmp_path):
    from rn.demo import demo_command
    script = write(tmp_path / 'd.json', {'seed': 3, 'steps': [
        {'pause': 200}, {'type': 'Milk', 'into': {'placeholder': 'New task'}}, {'click': '#add'},
        {'zoom': 'off'}, {'click': {'role': 'button', 'name': 'Bottom'}, 'hold': 300}]})
    result = demo_command(app, demo=str(script), out=str(tmp_path / 'out'), size=SIZE)
    video = frames(Path(result['video']['path']))
    steps = result['steps']
    assert len(video) == result['video']['frames'] == steps[-1]['end_frame']
    assert all(a['end_frame'] == b['start_frame'] for a, b in zip(steps, steps[1:]))
    assert steps[1]['end'] - steps[1]['start'] > 4 * 0.045  # four keystrokes at human speed, after a glide
    green = (32, 176, 80)
    assert not share(video[0], green).any(), 'no list row before the click'
    after_click = video[steps[2]['end_frame'] - 1]
    assert share(after_click, green).sum() > 1000, 'the click added a row'
    assert share(video[-1], (32, 64, 208)).mean() > 0.3, 'the page scrolled down to the Bottom section'
    assert share(video[steps[3]['start_frame'] - 1], (32, 64, 208)).mean() < 0.01
    again = demo_command(app, demo=str(script), out=str(tmp_path / 'again'), size=SIZE)
    assert again['steps'] == result['steps'], 'same script, same layout: the same timeline'


def test_auto_and_manual_zoom_scale_the_target(app, tmp_path):
    from rn.demo import demo_command
    magenta = (255, 34, 153)
    script = write(tmp_path / 'd.json', {'steps': [{'pause': 100}, {'click': '#add'}, {'zoom': 'off'},
                                                   {'zoom': '#add', 'scale': 3}, {'pause': 100}]})
    result = demo_command(app, demo=str(script), out=str(tmp_path / 'out'), size=SIZE)
    video = frames(Path(result['video']['path']))
    steps = result['steps']
    base = width_of(video[0], magenta)
    assert base == pytest.approx(48 * 1.5, abs=4)
    auto = width_of(video[steps[1]['end_frame'] - 1], magenta)  # the small button: zoomed in by itself
    assert auto == pytest.approx(base * 2, rel=0.08)
    assert width_of(video[steps[2]['end_frame'] - 1], magenta) == pytest.approx(base, abs=4)  # zoomed back out
    assert width_of(video[-1], magenta) == pytest.approx(base * 3, rel=0.08)


def test_a_missing_target_names_the_step_and_saves_the_page(app, tmp_path, monkeypatch):
    import rn.demo
    monkeypatch.setattr(rn.demo, 'TARGET_TIMEOUT_S', 0.5)
    script = write(tmp_path / 'd.json', {'steps': [{'pause': 100}, {'click': {'role': 'button', 'name': 'Delete'}}]})
    out = tmp_path / 'out'
    with pytest.raises(RnError, match=r'demo step 2 .*Delete.*no visible element') as error:
        rn.demo.demo_command(app, demo=str(script), out=str(out), size=SIZE)
    assert (out / 'failed-step-2.png').is_file() and 'failed-step-2.png' in str(error.value)
    assert not (out / 'demo.mp4').exists()


def test_vertical_and_backdrop_from_the_cli(app, tmp_path, capsys):
    target = str(write(tmp_path / 'app.html', APP))  # a local file works as well as a URL
    script = write(tmp_path / 'd.json', {'steps': [{'pause': 100}, {'click': '#add'}]})
    out = tmp_path / 'out'
    assert main(['capture', target, '-o', str(out), '--demo', str(script), '--size', '960x540', '--vertical', '--backdrop',
                 '--json', str(tmp_path / 'r.json')]) == 0
    printed = capsys.readouterr().out
    assert 'demo.mp4' in printed and 'in a window' in printed and 'EDL main-track clip' in printed
    result = json.loads((tmp_path / 'r.json').read_text(encoding='utf-8'))
    wide, tall = probe(result['video']['path'], exact=True)['video'], probe(result['vertical']['path'], exact=True)['video']
    assert (wide['width'], wide['height']) == (960, 540) and (tall['width'], tall['height']) == (540, 960)
    assert math.isclose(tall['width'] / tall['height'], 9 / 16, rel_tol=0.01) and tall['frames'] == wide['frames']
    first = frames(Path(result['video']['path']))[0]
    assert tuple(first[4, 4]) != tuple(first[270, 480]) and first[4, 4].max() < 120, 'a dark gradient around the window'
    magenta = (255, 34, 153)
    vertical = frames(Path(result['vertical']['path']))
    assert share(vertical[-1], magenta).sum() > 500, 'the 9:16 cut follows the clicked button'
    assert (out.with_name('out.proof.jpg')).is_file()
    assert main(['capture', target, '-o', str(out), '--vertical']) == 1
    assert '--demo' in capsys.readouterr().err
