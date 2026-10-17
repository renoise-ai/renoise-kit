import re
import shutil
import subprocess

import pytest

np = pytest.importorskip('numpy')
from rn import fx  # noqa: E402


def frame(channels=3):
    img = np.full((48, 64, channels), 120, np.uint8)
    img[..., 0] = np.arange(64, dtype=np.uint8) * 3
    if channels == 4:
        img[..., 3] = np.arange(48, dtype=np.uint8)[:, None] * 5
    return img


EFFECTS = {
    'grain': lambda im, i: fx.grain(im, i),
    'vignette': lambda im, i: fx.vignette(im),
    'leak': lambda im, i: fx.leak(im, i),
    'flash': lambda im, i: fx.flash(im, 0.05, at=0.0),
    'letterbox': lambda im, i: fx.letterbox(im),
    'rgb_split': lambda im, i: fx.rgb_split(im, px=3),
    'glitch': lambda im, i: fx.glitch(im, i, amount=1.0),
    'strobe': lambda im, i: fx.strobe(im, i),
    'transform': lambda im, i: fx.apply_transform(im, 1.2, 5, -3),
}


@pytest.mark.parametrize('name', EFFECTS)
@pytest.mark.parametrize('channels', [3, 4])
def test_shape_dtype_alpha_deterministic(name, channels):
    img, before = frame(channels), frame(channels).copy()
    out = EFFECTS[name](img, 4)
    assert out.shape == img.shape and out.dtype == np.uint8
    assert (img == before).all()  # input untouched
    assert (out == EFFECTS[name](img, 4)).all()
    if channels == 4 and name != 'transform':  # transform moves alpha with the pixels
        assert (out[..., 3] == img[..., 3]).all()
    assert not (out == img).all()


def test_grain_varies_by_frame_and_seed():
    img = frame()
    assert not (fx.grain(img, 1) == fx.grain(img, 2)).all()
    assert not (fx.grain(img, 1, seed=1) == fx.grain(img, 1, seed=2)).all()


def test_hash_and_rng_stable():
    assert fx.hash(1, 2, 3) == fx.hash(1, 2, 3) and 0 <= fx.hash(1, 2, 3) < 1
    assert fx.hash(1, 2, 3) != fx.hash(1, 2, 4)
    a, b = fx.rng(5), fx.rng(5)
    assert [next(a) for _ in range(3)] == [next(b) for _ in range(3)]


def test_punch_and_shake_settle():
    assert fx.punch(0.0, at=0.5) == 1.0
    assert fx.punch(0.5, at=0.5, amount=0.1) == pytest.approx(1.1)
    assert fx.punch(0.9, at=0.5, decay=0.25) == 1.0
    assert fx.shake(10, 0.9, at=0.5, decay=0.25) == (0.0, 0.0)
    assert fx.shake(10, 0.5, at=0.5) != (0.0, 0.0)
    assert fx.shake(10, 0.55, at=0.5) == fx.shake(10, 0.55, at=0.5)


def test_flash_window_and_transform_cover():
    img = frame()
    assert (fx.flash(img, 1.0, at=0.0) == img).all()
    assert (fx.apply_transform(img, 1.0) == img).all()
    solid = np.full((40, 40, 3), 200, np.uint8)
    assert (fx.apply_transform(solid, 1.1, 999, 999) == 200).all()  # clamped: no black edges


def test_catalog_resolves_and_js_exists():
    js = fx.js_path()
    assert js.is_file()
    source = js.read_text(encoding='utf-8')
    for entry in fx.catalog():
        assert callable(getattr(fx, entry['py'].split('.')[-1]))
        assert re.search(r'\b' + entry['js'].split('.')[-1] + r'\b', source.split('window.RNFX = ')[-1])
        assert isinstance(entry['params'], dict) and entry['what']


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_fx_js_syntax():
    subprocess.run(['node', '--check', str(fx.js_path())], check=True)
