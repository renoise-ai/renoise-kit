"""rn key: a backdrop matte for footage or stills shot (or generated) on a real backdrop: a green / blue / any solid
colour, or white. No model: the backdrop colour is measured on the frame border and every pixel's distance from it
becomes alpha. Writes a numbered grey PNG sequence (white = subject) to use as an EDL overlay "mask", or RGBA cut-outs.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .ff import RnError, probe, source_fps

BORDER = 0.03          # share of the width / height read as the border band
SAMPLES = 5            # frames whose border sets the auto colour
WHITE_MIN = 238        # --white: min(R, G, B) of a near-white pixel ...
WHITE_SPREAD = 22      # ... and max - min of its channels (no tint)


def _ycc(rgb: np.ndarray) -> np.ndarray:
    """BT.709 luma and chroma (0..255 scale) of an (..., 3) RGB array; luma weighted 0.5 so shading on the backdrop
    (a creased green screen) stays nearer the key than a subject of a different hue."""
    rgb = rgb.astype(np.float32)
    y = rgb @ np.float32([0.2126, 0.7152, 0.0722])
    cb = (rgb[..., 2] - y) / 1.8556
    cr = (rgb[..., 0] - y) / 1.5748
    return np.stack([0.5 * y, cb, cr], axis=-1)


def _border(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    bh, bw = max(1, round(h * BORDER)), max(1, round(w * BORDER))
    return np.concatenate([rgb[:bh].reshape(-1, 3), rgb[-bh:].reshape(-1, 3), rgb[:, :bw].reshape(-1, 3), rgb[:, -bw:].reshape(-1, 3)])


def _runs_reached(cand: np.ndarray, reached: np.ndarray) -> np.ndarray:
    """Spread `reached` along each row's runs of candidate pixels: a run touching a reached pixel is reached entirely."""
    c = np.pad(cand, ((0, 0), (0, 1))).ravel()  # a False column so runs never wrap into the next row
    r = np.pad(reached & cand, ((0, 0), (0, 1))).ravel()
    run_id = np.cumsum(c & ~np.r_[False, c[:-1]]) * c
    hit = np.bincount(run_id[r], minlength=run_id.max() + 1) > 0
    hit[0] = False
    return hit[run_id].reshape(cand.shape[0], -1)[:, :-1]


def border_connected(cand: np.ndarray) -> np.ndarray:
    """Candidate pixels 4-connected to the image border (flood fill), by alternating row and column run sweeps until
    nothing changes: a few sweeps for ordinary shapes, no per-pixel loop."""
    reached = np.zeros_like(cand)
    reached[0], reached[-1], reached[:, 0], reached[:, -1] = cand[0], cand[-1], cand[:, 0], cand[:, -1]
    while True:
        grown = _runs_reached(cand, reached)
        grown = _runs_reached(cand.T, grown.T).T
        if (grown == reached).all():
            return grown
        reached = grown


def white_alpha(rgb: np.ndarray, softness: float) -> np.ndarray:
    lo, hi = rgb.min(axis=-1), rgb.max(axis=-1)
    background = border_connected((lo >= WHITE_MIN) & (hi.astype(np.int16) - lo <= WHITE_SPREAD))
    alpha = (~background).astype(np.float32)
    if softness > 0:
        from PIL import Image, ImageFilter
        alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(softness)), np.float32) / 255
    return alpha


def colour_alpha(rgb: np.ndarray, key: np.ndarray, tolerance: float, softness: float) -> np.ndarray:
    distance = np.linalg.norm(_ycc(rgb) - _ycc(key), axis=-1)
    return np.clip((distance - tolerance) / max(softness, 1e-6), 0, 1)


def despill(rgb: np.ndarray, key: np.ndarray) -> np.ndarray:
    """The key's strongest channel (green on a green screen) capped at the larger of the other two, so edges and
    reflections lose the backdrop's tint. A key with no dominant channel (grey, white) is left as is."""
    channel = int(np.argmax(key))
    others = [c for c in range(3) if c != channel]
    if key[channel] - key[others].max() < 30:
        return rgb
    out = rgb.copy()
    cap = rgb[..., others].max(axis=-1)
    out[..., channel] = np.minimum(rgb[..., channel], cap)
    return out


def _parse_colour(text: str) -> np.ndarray:
    value = text.lstrip('#')
    if len(value) != 6:
        raise RnError(f'--color "{text}": expected auto or #rrggbb')
    try:
        return np.array([int(value[i:i + 2], 16) for i in (0, 2, 4)], np.float32)
    except ValueError:
        raise RnError(f'--color "{text}": expected auto or #rrggbb') from None


def _hex(rgb) -> str:
    return '#' + ''.join(f'{int(round(v)):02x}' for v in rgb)


def _decode(path: str, width: int | None = None):
    """(RGB uint8 frames, frame count, fps or None for a still). width: decode smaller (aspect kept)."""
    from PIL import Image

    info = probe(path)
    video = info['video']
    if not video:
        raise RnError(f'{path}: no picture')
    if video['still']:
        image = Image.open(path).convert('RGB')
        if width and image.width > width:
            image = image.resize((width, max(1, round(image.height * width / image.width))))
        return iter([np.asarray(image)]), 1, None
    from .shots import _frames
    display = (video['width'], video['height'])
    if abs(int(video.get('rotation') or 0)) % 180 == 90:
        display = display[::-1]
    return (f[..., ::-1] for f in _frames(path, display, width or display[0])), video['frames'] or 0, source_fps(info)


def _border_samples(path: str) -> np.ndarray:
    """Border bands of a few evenly spaced frames, decoded small (a quick first pass)."""
    frames, count, _ = _decode(path, width=320)
    picks = {round(k * (max(count, 1) - 1) / max(1, SAMPLES - 1)) for k in range(SAMPLES)}
    bands = [_border(rgb) for k, rgb in enumerate(frames) if k in picks or count <= 1]
    if not bands:
        raise RnError(f'{path}: no decodable frames')
    return np.concatenate(bands)


def key_command(path: str, *, out: str, color: str = 'auto', white: bool = False, tolerance: float | None = None,
                softness: float | None = None, despill_edges: bool = False, cutout: bool = False, every: int = 1) -> dict:
    from PIL import Image

    if every < 1:
        raise RnError('--every must be >= 1')
    if white and color != 'auto':
        raise RnError('--white keys near-white connected to the border; it takes no --color')
    tolerance = (0.0 if white else 40.0) if tolerance is None else tolerance
    softness = (1.0 if white else 30.0) if softness is None else softness

    border = _border_samples(path)
    if white:
        key = np.median(border, axis=0).astype(np.float32)
        uniform = float(((border.min(axis=-1) >= WHITE_MIN) & (np.ptp(border, axis=-1) <= WHITE_SPREAD)).mean())
    else:
        key = np.median(border, axis=0).astype(np.float32) if color == 'auto' else _parse_colour(color)
        uniform = float((np.linalg.norm(_ycc(border) - _ycc(key), axis=-1) <= tolerance + softness / 2).mean())

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('*.png'):
        stale.unlink()
    frames, _, fps = _decode(path)
    per_frame: list[dict] = []
    alpha = None
    written = 0
    for n, rgb in enumerate(frames):
        if n % every == 0 or alpha is None:
            alpha = white_alpha(rgb, softness) if white else colour_alpha(rgb, key, tolerance, softness)
            per_frame.append({'f': n, 'coverage': round(float((alpha >= 0.5).mean()), 4),
                              'partial': round(float(((alpha > 0.02) & (alpha < 0.98)).mean()), 4)})
        grey = (alpha * 255 + 0.5).astype(np.uint8)
        target = out_dir / f'{n + 1:05d}.png'
        if cutout:
            picture = despill(rgb, key) if despill_edges and not white else rgb
            Image.fromarray(np.dstack([picture, grey])).save(target)
        else:
            Image.fromarray(grey).save(target)
        written += 1
    if written == 0:
        raise RnError(f'{path}: no decodable frames')

    coverage = [p['coverage'] for p in per_frame]
    if fps is None:
        use = {'image': f'{out_dir.name}/00001.png'}
    else:
        use = {'frames': f'{out_dir.name}/%05d.png', 'fps': str(fps.numerator) if fps.denominator == 1 else f'{fps.numerator}/{fps.denominator}'}
    return {
        'dir': str(out_dir), 'frames': written, 'mode': 'white' if white else 'colour', 'every': every, 'cutout': cutout,
        'backdrop': {'color': _hex(key), 'source': 'given' if color != 'auto' else 'border', 'border_uniform': round(uniform, 4)},
        'tolerance': tolerance, 'softness': softness, 'despill': bool(despill_edges and cutout and not white),
        'coverage': {'mean': round(float(np.mean(coverage)), 4), 'min': min(coverage), 'max': max(coverage)},
        'partial_mean': round(float(np.mean([p['partial'] for p in per_frame])), 4),
        'per_frame': per_frame,
        'edl': use if cutout else {'mask': {**use, 'mode': 'luma'}},
    }


def summary_lines(result: dict) -> list[str]:
    kind = 'RGBA cut-outs' if result['cutout'] else 'grey mattes (white = subject)'
    backdrop = result['backdrop']
    uniform = f', {backdrop["border_uniform"] * 100:.0f}% of the border matches it'
    coverage = result['coverage']
    lines = [f'{result["dir"]}/%05d.png  {result["frames"]} {kind}, keyed every {result["every"]} frame(s)',
             f'backdrop {backdrop["color"]} ({backdrop["source"]}, {result["mode"]}){uniform}',
             f'subject covers {coverage["mean"] * 100:.1f}% of the frame (min {coverage["min"] * 100:.1f}%, max {coverage["max"] * 100:.1f}%); '
             f'soft edge pixels {result["partial_mean"] * 100:.2f}%']  # the EDL mask: a next: line (rn/agent.py)
    if backdrop['border_uniform'] < 0.8:
        lines.append('check: the border is not one even backdrop (shading, a set, a subject touching the edge); look at the matte '
                     'on a sheet, give --color, or raise --tolerance')
    if coverage['max'] > 0.95 or coverage['min'] < 0.005:
        lines.append('check: some frames are almost all subject or almost all backdrop: wrong key colour or tolerance?')
    return lines
