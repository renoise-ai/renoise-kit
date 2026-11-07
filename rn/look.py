"""rn look / rn grade: a picture's tone and colour as numbers, and a 3D LUT that moves one source toward references.

rn look measures; rn grade fits one per-pixel transform (tone curve on L*, zone casts and chroma in Lab) and bakes it
into a .cube LUT that ffmpeg's lut3d reads, so the EDL applies it with "vf": "lut3d=file=look.cube". Neither says
whether a look is right: generated shots from different calls drift apart in grade, and these numbers show by how much.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from .ff import RnError, probe, run, source_fps
from .frames import IMAGES, extract, pick_indices

SAMPLE_W = 320  # tone and colour statistics do not need full resolution
ZONES = (('shadows', 0, 33), ('mids', 33, 66), ('highlights', 66, 101))  # fixed L* bands, comparable between pictures
MIN_ZONE = 0.005  # a zone under 0.5% of the pixels has no cast to report
TOLERANCE = 4.0  # distance up to this reads "close"; up to 3x "noticeable"; beyond "far" (override with --tolerance)
CLIP_FLAG = 0.01  # share of crushed or blown pixels worth flagging
DISTANCE = ('distance = tone + cast + chroma / 2: tone = mean |dL*| of black (p2), mid (p50), white (p98); '
            'cast = mean a*b* distance of the zone means present in both; chroma = |d mean chroma|. Lower is closer. '
            'dE76 = Euclidean distance of the mean Lab.')

# --- colour maths (sRGB D65 <-> CIELAB), numpy only ---------------------------------------------------------------
_M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
_M_INV = np.linalg.inv(_M)
_WHITE = _M.sum(axis=1)  # D65 white for sRGB (1, 1, 1)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """(..., 3) sRGB in 0..1 -> (..., 3) L* a* b*."""
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ _M.T / _WHITE
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """(..., 3) L* a* b* -> (..., 3) sRGB clipped to 0..1."""
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], axis=-1)
    xyz = np.where(f > 6 / 29, f ** 3, 3 * (6 / 29) ** 2 * (f - 4 / 29)) * _WHITE
    linear = np.clip(xyz @ _M_INV.T, 0, 1)
    return np.clip(np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055), 0, 1)


# --- samples --------------------------------------------------------------------------------------------------------
def _image(path: Path) -> np.ndarray:
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert('RGB')
        if image.width > SAMPLE_W:
            image = image.resize((SAMPLE_W, max(1, round(image.height * SAMPLE_W / image.width))), Image.BILINEAR)
        return np.asarray(image, dtype=np.uint8)


def samples(path: str | Path, *, seconds: list[float] | None = None, count: int | None = None,
            indices: list[int] | None = None) -> list[tuple[str, np.ndarray]]:
    """[(label, HxWx3 uint8)] of an image, a folder of stills, or a video's frames (exact decoded indices)."""
    path = Path(path)
    if path.is_dir():
        files = sorted(f for f in path.iterdir() if f.suffix.lower() in IMAGES)
        if not files:
            raise RnError(f'{path}: no .png/.jpg/.webp images')
        return [(f.name, _image(f)) for f in files]
    if path.suffix.lower() in IMAGES:
        if not path.exists():
            raise RnError(f'no such file: {path}')
        return [(path.name, _image(path))]
    info = probe(path)
    video = info['video']
    if not video:
        raise RnError(f'{path}: no video stream')
    if video['still']:
        return [(path.name, _image(path))]
    fps = source_fps(info)
    chosen = indices if indices is not None else pick_indices(video['frames'], fps, every=None, rate=None, indices=None,
                                                              seconds=seconds, count=count or 8)
    if not chosen:
        raise RnError(f'{path}: no frames in range (the clip has {video["frames"]} frames)')
    with tempfile.TemporaryDirectory() as tmp:
        return [(f'#{i}', _image(p)) for i, p in extract(path, chosen, Path(tmp), width=SAMPLE_W)]


def _pixels(images: list[np.ndarray]) -> np.ndarray:
    return np.concatenate([image.reshape(-1, 3) for image in images])


# --- measurement ----------------------------------------------------------------------------------------------------
def measure(rgb8: np.ndarray) -> dict:
    """Tone and colour of a pool of pixels ((N, 3) uint8 sRGB)."""
    lab = rgb_to_lab(rgb8 / 255.0)
    lum = lab[:, 0]
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    black, mid, white = (float(np.percentile(lum, p)) for p in (2, 50, 98))
    zones = {}
    for name, low, high in ZONES:
        inside = (lum >= low) & (lum < high)
        share = float(inside.mean())
        zones[name] = {'share': round(share, 4),
                       'a': round(float(lab[inside, 1].mean()), 2) if share >= MIN_ZONE else None,
                       'b': round(float(lab[inside, 2].mean()), 2) if share >= MIN_ZONE else None}
    return {
        'black': round(black, 2), 'mid': round(mid, 2), 'white': round(white, 2), 'contrast': round(white - black, 2),
        'zones': zones,
        'chroma': round(float(chroma.mean()), 2), 'chroma_p90': round(float(np.percentile(chroma, 90)), 2),
        'clipped_black': round(float((rgb8.max(axis=1) <= 2).mean()), 4),  # every channel at the floor
        'clipped_white': round(float((rgb8.min(axis=1) >= 253).mean()), 4),  # every channel at the ceiling
        'mean_lab': [round(float(v), 2) for v in lab.mean(axis=0)],
    } | {'flags': _flags(rgb8)}


def _flags(rgb8: np.ndarray) -> list[str]:
    crushed, blown = float((rgb8.max(axis=1) <= 2).mean()), float((rgb8.min(axis=1) >= 253).mean())
    return ([f'crushed blacks {crushed:.1%}'] if crushed > CLIP_FLAG else []) + ([f'blown highlights {blown:.1%}'] if blown > CLIP_FLAG else [])


def match(distance: float, tolerance: float = TOLERANCE) -> str:
    return 'close' if distance <= tolerance else 'noticeable' if distance <= 3 * tolerance else 'far'


def versus(stats: dict, ref: dict, tolerance: float = TOLERANCE) -> dict:
    """Input minus reference, one distance (see DISTANCE) and a verdict on it: close / noticeable / far."""
    tone = {k: round(stats[k] - ref[k], 2) for k in ('black', 'mid', 'white', 'contrast')}
    casts, zones = [], {}
    for name, _, _ in ZONES:
        mine, theirs = stats['zones'][name], ref['zones'][name]
        if mine['a'] is None or theirs['a'] is None:
            zones[name] = None
            continue
        da, db = mine['a'] - theirs['a'], mine['b'] - theirs['b']
        zones[name] = {'a': round(da, 2), 'b': round(db, 2)}
        casts.append(float(np.hypot(da, db)))
    tone_d = float(np.mean([abs(tone[k]) for k in ('black', 'mid', 'white')]))
    cast_d = float(np.mean(casts)) if casts else 0.0
    chroma_d = stats['chroma'] - ref['chroma']
    distance = round(tone_d + cast_d + abs(chroma_d) / 2, 2)
    return {'tone': tone, 'zones': zones, 'chroma': round(chroma_d, 2), 'distance': distance, 'match': match(distance, tolerance),
            'de76': round(float(np.linalg.norm(np.subtract(stats['mean_lab'], ref['mean_lab']))), 2)}


def _shot_indices(shots_json: str) -> list[tuple[str, int]]:
    try:
        data = json.loads(Path(shots_json).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RnError(f'--shots {shots_json}: {error}') from None
    shots = data.get('shots') if isinstance(data, dict) else None
    if not shots:
        raise RnError(f'--shots {shots_json}: expected rn shots --json output (a "shots" list)')
    return [(f'shot {s.get("shot", k)}', (s['start_frame'] + s['end_frame'] - 1) // 2) for k, s in enumerate(shots)]


def look_command(paths: list[str], *, refs: list[str] | None = None, seconds: list[float] | None = None, count: int | None = None,
                 shots: str | None = None, tolerance: float = TOLERANCE) -> dict:
    entries = []
    if shots:
        if len(paths) != 1:
            raise RnError('--shots: give one video, the one rn shots measured')
        picks = _shot_indices(shots)
        frames = dict(samples(paths[0], indices=[i for _, i in picks]))
        for label, index in picks:
            if f'#{index}' in frames:
                entries.append({'path': paths[0], 'label': label, 'samples': [f'#{index}'], 'stats': measure(_pixels([frames[f'#{index}']]))})
    else:
        for path in paths:
            got = samples(path, seconds=seconds, count=count)
            entries.append({'path': path, 'label': Path(path).name, 'samples': [label for label, _ in got],
                            'stats': measure(_pixels([image for _, image in got]))})
    result: dict = {'inputs': entries}
    if refs:
        pooled = [image for ref in refs for _, image in samples(ref, seconds=seconds, count=count)]
        result['ref'] = {'paths': refs, 'stats': measure(_pixels(pooled))}
        result['distance_is'] = DISTANCE + f' match: close <= {tolerance:g}, noticeable <= {3 * tolerance:g}, else far (--tolerance).'
        for entry in entries:
            entry['vs_ref'] = versus(entry['stats'], result['ref']['stats'], tolerance)
    return result


def _cast(zone: dict | None) -> str:
    return '  --  ' if zone is None or zone.get('a') is None else f'{zone["a"]:+.1f}/{zone["b"]:+.1f}'


def _line(label: str, s: dict) -> str:
    z = s['zones']
    return (f'{label}  L* {s["black"]:.0f}/{s["mid"]:.0f}/{s["white"]:.0f} (contrast {s["contrast"]:.0f})  '
            f'a*/b* sh {_cast(z["shadows"])} mid {_cast(z["mids"])} hi {_cast(z["highlights"])}  '
            f'chroma {s["chroma"]:.1f}  clipped {s["clipped_black"]:.1%}/{s["clipped_white"]:.1%}'
            + ''.join(f'  [{flag}]' for flag in s['flags']))


def summary_lines(result: dict) -> list[str]:
    lines = [_line(e['label'], e['stats']) + (f'  -> ref: {e["vs_ref"]["match"].upper()} (distance {e["vs_ref"]["distance"]}, dE76 {e["vs_ref"]["de76"]})'
                                              if 'vs_ref' in e else '') for e in result['inputs']]
    if 'ref' in result:
        lines.append(_line('ref (pooled)', result['ref']['stats']))
        lines.append(result['distance_is'])
    lines.append('L* black/mid/white = p2/p50/p98; a*/b* = zone mean casts (+a red, -a green, +b yellow, -b blue); '
                 'clipped = share at black/white')
    return lines


# --- matching -------------------------------------------------------------------------------------------------------
_KNOTS = (0.5, 2, 5, 10, 25, 50, 75, 90, 95, 98, 99.5)
_CENTRES = np.array([16.5, 50.0, 83.5])  # zone centres in L*


def _zone_weights(lum: np.ndarray) -> np.ndarray:
    """(N, 3) soft shadow/mid/highlight weights summing to 1: no band edges in the graded picture."""
    w = np.clip(1 - np.abs(lum[:, None] - _CENTRES[None, :]) / 33.5, 0, None)
    w[:, 0] = np.where(lum < _CENTRES[0], 1, w[:, 0])
    w[:, 2] = np.where(lum > _CENTRES[2], 1, w[:, 2])
    return w / w.sum(axis=1, keepdims=True)


def _zone_means(lab: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """(3 zones, 2) weighted mean a*, b*; an empty zone falls back to the overall mean."""
    total = weights.sum(axis=0)
    overall = lab[:, 1:].mean(axis=0)
    means = (weights.T @ lab[:, 1:]) / np.maximum(total, 1e-9)[:, None]
    return np.where((total / len(lab) < MIN_ZONE)[:, None], overall, means)


def fit(src_rgb8: np.ndarray, ref_rgb8: np.ndarray, strength: float = 1.0):
    """A function (..., 3) sRGB 0..1 -> sRGB 0..1 moving src toward ref: 1) L* tone curve through matched percentiles,
    2) chroma scaled by the ratio of mean chroma (0.5..2), 3) each zone's mean a*/b* moved to the reference's, with soft
    zone weights. The result is blended with the input by strength."""
    src, ref = rgb_to_lab(src_rgb8 / 255.0), rgb_to_lab(ref_rgb8 / 255.0)
    xs = np.concatenate([[0], np.percentile(src[:, 0], _KNOTS), [100]])
    ys = np.concatenate([[0], np.percentile(ref[:, 0], _KNOTS), [100]])
    xs = np.maximum.accumulate(xs + np.arange(len(xs)) * 1e-6)  # strictly increasing for interp
    ys = np.maximum.accumulate(ys)
    k = float(np.clip(np.hypot(ref[:, 1], ref[:, 2]).mean() / max(np.hypot(src[:, 1], src[:, 2]).mean(), 1e-6), 0.5, 2.0))
    src_tone = np.interp(src[:, 0], xs, ys)
    src_means = _zone_means(np.column_stack([src_tone, src[:, 1:] * k]), _zone_weights(src_tone))
    ref_means = _zone_means(ref, _zone_weights(ref[:, 0]))
    shift = ref_means - src_means

    def apply(rgb: np.ndarray) -> np.ndarray:
        lab = rgb_to_lab(rgb.reshape(-1, 3))
        tone = np.interp(lab[:, 0], xs, ys)
        ab = lab[:, 1:] * k + _zone_weights(tone) @ shift
        graded = np.column_stack([tone, ab])
        return lab_to_rgb(lab + strength * (graded - lab)).reshape(rgb.shape)
    return apply, {'tone_knots': {'src': [round(float(v), 2) for v in xs], 'ref': [round(float(v), 2) for v in ys]},
                   'chroma_scale': round(k, 3), 'zone_shift_ab': [[round(float(v), 2) for v in row] for row in shift]}


def write_cube(path: Path, transform, size: int) -> None:
    """A .cube 3D LUT (sRGB in and out, red varying fastest) as ffmpeg lut3d and most editors read it."""
    axis = np.linspace(0, 1, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing='ij')
    grid = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    out = transform(grid)
    lines = ['TITLE "rn grade"', f'LUT_3D_SIZE {size}', 'DOMAIN_MIN 0 0 0', 'DOMAIN_MAX 1 1 1']
    lines += [f'{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}' for v in out]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def lut_filter(cube: Path) -> str:
    """lut3d on a file by name from its own folder (ffmpeg filter arguments cannot hold every path character)."""
    return f'lut3d=file={cube.name}:interp=tetrahedral'


def grade_command(src: str, refs: list[str], *, out: str, strength: float = 1.0, size: int = 33, count: int | None = None,
                  preview: str | None = None, proof: str | None = None) -> dict:
    if not 0 <= strength <= 1:
        raise RnError('--strength: between 0 and 1')
    if not 2 <= size <= 65:
        raise RnError('--size: between 2 and 65 (33 is usual)')
    if Path(out).suffix.lower() != '.cube':
        raise RnError('-o: a .cube file')
    src_samples = samples(src, count=count or 12)
    ref_samples = [image for ref in refs for _, image in samples(ref, count=count or 12)]
    transform, model = fit(_pixels([i for _, i in src_samples]), _pixels(ref_samples), strength)
    cube = Path(out)
    write_cube(cube, transform, size)
    result = {'path': str(cube), 'size': size, 'strength': strength, 'src': src, 'refs': refs, 'model': model}
    src_stats, ref_stats = measure(_pixels([i for _, i in src_samples])), measure(_pixels(ref_samples))
    graded = np.clip(np.round(transform(_pixels([i for _, i in src_samples]) / 255.0) * 255), 0, 255).astype(np.uint8)
    result['before'] = versus(src_stats, ref_stats)
    result['after'] = versus(measure(graded), ref_stats)  # the transform itself; the LUT interpolates it
    if preview or proof:
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            # through ffmpeg's lut3d, as the edit will apply it: the LUT itself, not the fitted transform
            picks = [src_samples[0], src_samples[len(src_samples) // 2]][:len(src_samples)]
            graded_images = []
            for k, (_, image) in enumerate(picks):
                first, after = Path(tmp) / f'src{k}.png', Path(tmp) / f'graded{k}.png'
                Image.fromarray(image).save(first)
                run(['ffmpeg', '-i', str(first.resolve()), '-vf', lut_filter(cube), '-y', str(after.resolve())], cwd=cube.resolve().parent)
                graded_images.append(Image.open(after).convert('RGB'))
            if preview:
                Path(preview).parent.mkdir(parents=True, exist_ok=True)
                graded_images[0].save(preview)
                result['preview'] = preview
            if proof:
                from .agent import write_proof
                images, labels = [], []
                for k, ((_, image), graded) in enumerate(zip(picks, graded_images)):
                    images += [Image.fromarray(image), graded, Image.fromarray(ref_samples[min(k, len(ref_samples) - 1)])]
                    labels += ['before', 'after (LUT)', 'reference']
                result['proof'] = write_proof(images, labels, f'rn grade {Path(src).name} -> {cube.name}: '
                                              f'distance {result["before"]["distance"]} -> {result["after"]["distance"]}', Path(proof), columns=3)
    return result


def grade_summary_lines(result: dict) -> list[str]:
    m = result['model']
    return [f'{result["path"]}  {result["size"]}^3 LUT, strength {result["strength"]}',
            f'distance to the references {result["before"]["distance"]} ({result["before"]["match"]}) -> '
            f'{result["after"]["distance"]} ({result["after"]["match"]})  '
            f'(dE76 {result["before"]["de76"]} -> {result["after"]["de76"]}; rn look on the graded clip shows the real result)',
            f'chroma x{m["chroma_scale"]}  zone a*/b* shifts sh/mid/hi {m["zone_shift_ab"]}',
            *([f'preview {result["preview"]}'] if result.get('preview') else [])]
