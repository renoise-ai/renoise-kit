"""Edit effects for numpy frames: readable source to call, copy, change or ignore (not a closed vocabulary).

Defaults are engine defaults, not design defaults. Images are H x W x 3 or x 4 uint8 in RGB order
(OpenCV gives BGR: convert with img[..., ::-1] first). Alpha is preserved. Every function returns a new array.
Deterministic: randomness only from hash/seeded RNG keyed by frame index + seed, so a frame renders the same
in any order, shard or rerun. `i` is the frame index, `t` seconds. fx.js (RNFX) is the browser twin.
"""
from __future__ import annotations

import inspect
import math
from pathlib import Path

from rn.ff import RnError

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def _np():
    if np is None:
        raise RnError('rn.fx needs numpy (pip install numpy)')
    return np


def hash(a: int, b: int = 0, c: int = 0) -> float:  # noqa: A001
    """Integer mix -> [0,1); same inputs always give the same number."""
    m = 0xFFFFFFFF
    h = ((a ^ 0x9E3779B9) * 0x85EBCA6B ^ (b + 0x7F4A7C15) * 0xC2B2AAE3 ^ (c + 0x165667B1) * 0x27D4EB2F) & m
    h = (h ^ (h >> 15)) * 0x2C1B3C6D & m
    h = (h ^ (h >> 12)) * 0x297A2D39 & m
    return ((h ^ (h >> 15)) & m) / 4294967296


def rng(seed: int):
    """Generator of floats in [0,1) (mulberry32)."""
    m, s = 0xFFFFFFFF, seed & 0xFFFFFFFF
    while True:
        s = (s + 0x6D2B79F5) & m
        t = ((s ^ (s >> 15)) * (1 | s)) & m
        t = ((t + ((t ^ (t >> 7)) * (61 | t) & m)) ^ t) & m
        yield ((t ^ (t >> 14)) & m) / 4294967296


def _color(value) -> tuple[int, int, int]:
    if isinstance(value, str):
        v = value.lstrip('#')
        if len(v) == 3:
            v = ''.join(ch * 2 for ch in v)
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return tuple(value)[:3]


def _blend(base, layer, op: str, alpha):
    """base/layer float 0..255 (H,W,3); alpha scalar or (H,W,1) opacity of the layer."""
    if op == 'soft-light':
        b, s = base / 255, layer / 255
        d = np.where(b <= 0.25, ((16 * b - 12) * b + 4) * b, np.sqrt(b))
        mixed = 255 * np.where(s <= 0.5, b - (1 - 2 * s) * b * (1 - b), b + (2 * s - 1) * (d - b))
    elif op == 'screen':
        mixed = 255 - (255 - base) * (255 - layer) / 255
    elif op == 'add':
        mixed = base + layer
    elif op == 'multiply':
        mixed = base * layer / 255
    elif op == 'normal':
        mixed = layer
    else:
        raise RnError(f'unknown blend op {op!r} (soft-light, screen, add, multiply, normal)')
    return base + (mixed - base) * alpha


def _run(img, fn):
    """Applies fn to the float RGB part; passes alpha through untouched."""
    _np()
    if img.ndim != 3 or img.shape[2] not in (3, 4) or img.dtype != np.uint8:
        raise RnError('rn.fx expects an H x W x 3|4 uint8 array')
    rgb = fn(img[..., :3].astype(np.float32))
    out = img.copy()
    out[..., :3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    return out


def _fill(img, color, alpha):
    return _run(img, lambda rgb: rgb + (np.array(_color(color), np.float32) - rgb) * alpha)


def grain(img, i: int, *, amount=0.3, cell=2, mono=True, seed=7, op='soft-light'):
    """Film grain. amount=opacity, cell=grain size px, mono=gray noise, op=blend mode."""
    _np()
    h, w = img.shape[:2]
    gen = np.random.default_rng([seed, int(i)])
    shape = (-(-h // cell), -(-w // cell), 1 if mono else 3)
    noise = gen.random(shape, dtype=np.float32) * 255
    noise = np.repeat(np.repeat(noise, cell, 0), cell, 1)[:h, :w]
    return _run(img, lambda rgb: _blend(rgb, np.broadcast_to(noise, rgb.shape), op, amount))


def vignette(img, *, amount=0.35, inner=0.45, outer=0.95, color='#000'):
    """Darkens edges. inner/outer = start/full radius as a fraction of the half diagonal."""
    _np()
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.hypot(xx - w / 2, yy - h / 2) / (math.hypot(w, h) / 2)
    mask = np.clip((d - inner) / (outer - inner), 0, 1)[..., None] * amount
    return _fill(img, color, mask)


def leak(img, i: int, *, color='#ff7a30', color2='#ffd27a', amount=0.55, x=0.85, y=0.2, r=0.6, drift=0.1, seed=3, op='screen', fps=30):
    """Drifting light leak. x,y = centre (0..1), r = radius as a fraction of the long side, drift = wander as a fraction of size."""
    _np()
    h, w = img.shape[:2]
    ph = hash(seed, 0, 0) * 6.283
    cx = (x + math.sin(i / fps * 0.9 + ph) * drift) * w
    cy = (y + math.cos(i / fps * 0.7 + ph) * drift) * h
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = (np.hypot(xx - cx, yy - cy) / (r * max(w, h)))[..., None]
    fall = np.clip(1 - d, 0, 1) ** 2
    c1, c2 = np.array(_color(color), np.float32), np.array(_color(color2), np.float32)
    layer = c1 + (c2 - c1) * np.clip(1 - d / 0.35, 0, 1)
    return _run(img, lambda rgb: _blend(rgb, layer, op, fall * amount))


def flash(img, t: float, *, at=0.0, duration=0.1, color='#fff', amount=1.0):
    """Full-frame colour fading out over `duration` s from `at`. color '#000' gives a dip."""
    u = (t - at) / duration
    return img.copy() if u < 0 or u >= 1 else _fill(img, color, amount * (1 - u))


def letterbox(img, *, top=0.1, bottom=0.1, color='#000'):
    """Bars as fractions of the height."""
    _np()
    out = img.copy()
    h = img.shape[0]
    out[: round(h * top), ..., :3] = _color(color)
    out[h - round(h * bottom):, ..., :3] = _color(color)
    return out


def rgb_split(img, *, px=6):
    """Red shifted right, blue left by px (edge pixels clamp)."""
    _np()
    px = int(round(px))
    w = img.shape[1]
    out = img.copy()
    out[..., 0] = img[:, np.clip(np.arange(w) - px, 0, w - 1), 0]
    out[..., 2] = img[:, np.clip(np.arange(w) + px, 0, w - 1), 2]
    return out


def glitch(img, i: int, *, amount=0.5, slices=8, seed=5):
    """Horizontal slice displacement. amount = share of slices hit and how far they move."""
    _np()
    h, w = img.shape[:2]
    bh = -(-h // slices)
    out = img.copy()
    for k in range(slices):
        if hash(seed, i, k) >= amount:
            continue
        dx = int((hash(seed, i, k + 100) - 0.5) * amount * 0.3 * w)
        out[k * bh:(k + 1) * bh] = np.roll(img[k * bh:(k + 1) * bh], dx, axis=1)
    return out


def strobe(img, i: int, *, every=2, color='#fff', amount=1.0):
    """Colour overlay on every `every`-th frame."""
    return img.copy() if i % every else _fill(img, color, amount)


def punch(t: float, *, at=0.0, amount=0.07, decay=0.25) -> float:
    """Scale for a punch-in at `at` s that is exactly 1 again after `decay` s. Feed it to apply_transform."""
    e = max(0.0, 1 - (t - at) / decay)
    return 1.0 if t < at else 1 + amount * e * e


def shake(i: int, t: float, *, at=0.0, amp=(16, 12), decay=0.25, seed=9) -> tuple[float, float]:
    """(dx, dy) px offset, amp=(x,y) px, exactly 0 after `decay` s. Feed it to apply_transform."""
    e = 0.0 if t < at else max(0.0, 1 - (t - at) / decay)
    return (hash(seed, i, 1) * 2 - 1) * amp[0] * e, (hash(seed, i, 2) * 2 - 1) * amp[1] * e


def apply_transform(img, scale=1.0, dx=0.0, dy=0.0):
    """Centre scale + translate; always covers the frame (scale >= 1, offset clamped so no edge shows)."""
    _np()
    from PIL import Image
    h, w = img.shape[:2]
    s = max(1.0, float(scale))
    dx = max(-(s - 1) * w / 2, min((s - 1) * w / 2, dx))
    dy = max(-(s - 1) * h / 2, min((s - 1) * h / 2, dy))
    coeffs = (1 / s, 0, w / 2 - (w / 2 + dx) / s, 0, 1 / s, h / 2 - (h / 2 + dy) / s)
    out = Image.fromarray(img).transform((w, h), Image.AFFINE, coeffs, Image.BILINEAR)
    return np.asarray(out).copy()


_JS_NAME = {'rgb_split': 'rgbSplit'}
_WHAT = {
    'grain': 'film grain from per-frame seeded noise',
    'vignette': 'darkened edges',
    'leak': 'drifting radial light leak',
    'flash': 'fading full-frame flash (or dip with a dark colour)',
    'letterbox': 'top/bottom bars',
    'rgb_split': 'red/blue channel offset',
    'glitch': 'horizontal slice displacement',
    'strobe': 'colour overlay on every Nth frame',
    'punch': 'punch-in scale that settles back to 1 (number)',
    'shake': 'decaying camera shake offset (dx, dy)',
}


def catalog() -> list[dict]:
    """One entry per effect: {name, js, py, params (defaults), what}."""
    entries = []
    for name, what in _WHAT.items():
        params = {k: p.default for k, p in inspect.signature(globals()[name]).parameters.items() if p.default is not inspect.Parameter.empty}
        entries.append({'name': name, 'js': f'RNFX.{_JS_NAME.get(name, name)}', 'py': f'rn.fx.{name}', 'params': params, 'what': what})
    return entries


def js_path() -> Path:
    """Path of fx.js (copy or read it to adapt)."""
    return Path(__file__).with_name('fx.js')
