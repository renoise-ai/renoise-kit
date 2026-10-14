"""rn capture: renderFrame(i) pages -> RGBA PNG sequences (needs Playwright + Chromium, as in the media sandbox)."""
from pathlib import Path

import pytest
from PIL import Image

from conftest import write_edl
from rn.ff import RnError

pytest.importorskip('playwright')
from rn.capture import capture_command  # noqa: E402

PAGE = '''<!doctype html><meta charset="utf-8"><style>html,body{margin:0;background:transparent}</style>
<canvas id="c"></canvas><script>
const c = document.getElementById('c'); c.width = RN.width; c.height = RN.height; const ctx = c.getContext('2d');
window.renderFrame = (i) => { ctx.clearRect(0, 0, RN.width, RN.height); ctx.fillStyle = '#ff0000'; ctx.fillRect(i * 10, 20, 20, 20);
  if (!window.RNFX || typeof RNFX.grain !== 'function') throw new Error('RNFX missing'); };
</script>'''


def _chromium_or_skip(tmp_path: Path) -> None:
    probe = tmp_path / 'probe.html'
    probe.write_text(PAGE)
    try:
        capture_command(str(probe), out=str(tmp_path / 'probe'), frames=1, width=64, height=64, fps=30, shards=1)
    except RnError as error:
        if 'Executable' in str(error) or 'install' in str(error):
            pytest.skip(f'no Chromium for Playwright: {error}')
        raise


def test_frames_are_exact_transparent_and_shard_independent(tmp_path):
    _chromium_or_skip(tmp_path)
    page = tmp_path / 'page.html'
    page.write_text(PAGE)
    one = capture_command(str(page), out=str(tmp_path / 'one'), frames=12, width=160, height=60, fps=30, shards=1)
    three = capture_command(str(page), out=str(tmp_path / 'three'), frames=12, width=160, height=60, fps=30, shards=3)
    assert one['frames'] == three['frames'] == 12 and len(three['shards']) == 3
    for k in (0, 5, 11):
        image = Image.open(tmp_path / 'one' / f'{k + 1:05d}.png')
        assert image.mode == 'RGBA'
        assert image.getpixel((150, 5))[3] == 0  # untouched canvas stays transparent
        assert image.getpixel((k * 10 + 10, 30))[:3] == (255, 0, 0)
        assert image.tobytes() == Image.open(tmp_path / 'three' / f'{k + 1:05d}.png').tobytes()


def test_capture_feeds_the_edl(tmp_path):
    _chromium_or_skip(tmp_path)
    from rn.assemble import assemble_command
    page = tmp_path / 'page.html'
    page.write_text(PAGE)
    capture_command(str(page), out=str(tmp_path / 'layer'), frames=10, width=160, height=60, fps=30, shards=1)
    edl = write_edl(tmp_path, {'output': {'width': 320, 'height': 240, 'fps': 30},
                               'tracks': [{'clips': [{'color': '#0000ff', 'duration': 1}]},
                                          {'clips': [{'frames': 'layer/%05d.png', 'at': 0.1}]}]})
    result = assemble_command(str(edl), str(tmp_path / 'out.mp4'), dry_run=False, keep=False)
    assert result['overlays'][0]['start_frame'] == 3 and result['overlays'][0]['frames'] == 10


def test_page_errors_are_reported(tmp_path):
    _chromium_or_skip(tmp_path)
    missing = tmp_path / 'missing.html'
    missing.write_text('<!doctype html><p>no renderFrame</p>')
    with pytest.raises(RnError, match='renderFrame'):
        capture_command(str(missing), out=str(tmp_path / 'm'), frames=2, width=64, height=64, fps=30, shards=1)
    broken = tmp_path / 'broken.html'
    broken.write_text('<!doctype html><script>window.renderFrame = (i) => { if (i === 1) throw new Error("boom"); };</script>')
    with pytest.raises(RnError, match='boom'):
        capture_command(str(broken), out=str(tmp_path / 'b'), frames=3, width=64, height=64, fps=30, shards=1)


def test_grain_keeps_an_overlay_transparent(tmp_path):
    # The rn capture --help example: grain over a cleared canvas with a title. Grain textures the title only; the
    # empty overlay stays transparent instead of turning into a grey noise layer (staging2 F5).
    _chromium_or_skip(tmp_path)
    page = tmp_path / 'grain.html'
    page.write_text(PAGE.replace("throw new Error('RNFX missing'); };", "throw new Error('RNFX missing'); RNFX.grain(ctx, i, {}); };"))
    capture_command(str(page), out=str(tmp_path / 'grain'), frames=2, width=160, height=60, fps=30, shards=1)
    image = Image.open(tmp_path / 'grain' / '00001.png')
    assert image.getpixel((150, 5))[3] == 0, 'empty overlay pixels stay transparent'
    assert image.getpixel((10, 30))[3] == 255, 'the drawn title keeps its alpha'
    alphas = image.getchannel('A').getdata()
    assert sum(1 for a in alphas if a == 0) / len(alphas) > 0.9
