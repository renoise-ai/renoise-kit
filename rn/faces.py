"""rn faces: per-frame face boxes and landmarks (YuNet), in source pixels."""
from __future__ import annotations

import math
import os
import tempfile
from collections import Counter
from fractions import Fraction
from pathlib import Path

from .ff import RnError
from .frames import _compose, _save_under

DEFAULT_MODEL = '/opt/media-sandbox/models/face_detection_yunet_2023mar.onnx'  # baked into the sandbox
# Outside the sandbox: fetched once into ~/.cache/rn (OpenCV zoo, MIT).
MODEL_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
MODEL_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
DETECT_LONG_SIDE = 960  # larger frames are detected at this size, then mapped back to source pixels
SHEET_FRAMES = 12


def _face(row, scale: float) -> dict:
    """YuNet row: x y w h, right eye, left eye, nose, right mouth corner, left mouth corner (all as seen in the image), score."""
    x, y, w, h, *marks, score = (float(v) for v in row[:15])
    point = lambda i: [round(marks[2 * i] / scale, 1), round(marks[2 * i + 1] / scale, 1)]  # noqa: E731
    return {'box': [round(v / scale, 1) for v in (x, y, w, h)], 'score': round(score, 3),
            'eyes': [point(0), point(1)], 'nose': point(2), 'mouth': [point(3), point(4)]}


def face_model_path(model: str | None = None) -> str:
    """The YuNet model: --model, $RN_YUNET_MODEL, the sandbox's baked copy, else fetched once into ~/.cache/rn."""
    model_path = model or os.environ.get('RN_YUNET_MODEL')
    if not model_path:
        from .assets import fetch_checked
        model_path = DEFAULT_MODEL if Path(DEFAULT_MODEL).is_file() else str(
            fetch_checked(MODEL_URL, MODEL_SHA256, Path.home() / '.cache' / 'rn' / Path(DEFAULT_MODEL).name, name='face model'))
    if not Path(model_path).is_file():
        raise RnError(f'face model not found: {model_path} (set RN_YUNET_MODEL or pass --model)')
    return model_path


def faces_command(path: str, *, every: int = 1, model: str | None = None, min_score: float = 0.6,
                  out_sheet: str | None = None, max_kb: int = 125, track: bool = False, pick: str = 'largest',
                  smooth: float = 0.25, max_gap: int = 12, reframe_size: tuple[int, int] | None = None, zoom: float = 1.0,
                  out: str | None = None) -> dict:
    """Measures where faces are in every `every`-th decoded frame. Measurement only: it never alters, blurs or crops a face.
    Use the boxes to keep text/captions off faces, or to place separate graphics that follow a subject.
    Coordinates are source pixels; `f` is the exact decoded frame index. `eyes` and `mouth` are ordered left-to-right as seen in the image."""
    import cv2

    model_path = face_model_path(model)
    if every < 1:
        raise RnError('--every must be >= 1')
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RnError(f'{path}: cannot open video')
    fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
    detector = cv2.FaceDetectorYN.create(model_path, '', (320, 320), min_score)
    width = height = 0
    scale = 1.0
    sampled: list[dict] = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index == 0:
            height, width = frame.shape[:2]
            scale = min(1.0, DETECT_LONG_SIDE / max(width, height))
        if index % every == 0:
            small = frame if scale == 1.0 else cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            detector.setInputSize((small.shape[1], small.shape[0]))
            _, rows = detector.detect(small)
            sampled.append({'f': index, 't': round(index / fps, 4) if fps else None,
                            'faces': [_face(row, scale) for row in (rows if rows is not None else [])]})
        index += 1
    capture.release()
    if index == 0:
        raise RnError(f'{path}: no decodable video frames')

    with_faces = [s for s in sampled if s['faces']]
    boxes = [f['box'] for s in with_faces for f in s['faces']]
    union = None
    if boxes:
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        union = [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]
    result = {
        'meta': {'path': str(path), 'width': width, 'height': height, 'fps': round(fps, 4), 'frames': index, 'every': every, 'model': model_path},
        'frames': sampled,
        'summary': {
            'sampled': len(sampled), 'with_faces': len(with_faces),
            'max_faces': max((len(s['faces']) for s in sampled), default=0),
            'union_box': union,
            'union_box_fraction': [round(union[0] / width, 4), round(union[1] / height, 4), round(union[2] / width, 4), round(union[3] / height, 4)] if union else None,
            'face_count_histogram': {str(k): v for k, v in sorted(Counter(len(s['faces']) for s in sampled).items())},
        },
    }
    if track or reframe_size:
        result['track'] = track_faces(sampled, width=width, height=height, fps=fps, pick=pick, max_gap=max_gap, smooth=smooth)
        tracked = [t for t in result['track'] if t['box']]
        result['track_summary'] = {'pick': pick, 'smooth': smooth, 'max_gap': max_gap, 'samples': len(sampled),
                                   'tracked': len(tracked), 'filled': sum(t['filled'] for t in tracked),
                                   'missing': len(sampled) - len(tracked)}
    if reframe_size:
        plan = reframe(result['track'], width=width, height=height, out_w=reframe_size[0], out_h=reframe_size[1], fps=fps, zoom=zoom)
        if out:
            plan['render'] = render_reframe(path, plan, out)
        result['reframe'] = plan
    elif out:
        raise RnError('-o renders a reframed video: give --reframe WxH too')
    if out_sheet:
        result['sheet'] = _write_sheet(path, sampled, Fraction(fps).limit_denominator(1001) or Fraction(1), out_sheet, max_kb)
    return result


def _write_sheet(path: str, sampled: list[dict], fps: Fraction, out_sheet: str, max_kb: int) -> dict:
    import cv2

    count = min(SHEET_FRAMES, len(sampled))
    picks = {sampled[round(k * (len(sampled) - 1) / max(1, count - 1))]['f']: sampled[round(k * (len(sampled) - 1) / max(1, count - 1))] for k in range(count)}
    capture = cv2.VideoCapture(str(path))
    tiles: list[tuple[int, Path]] = []
    with tempfile.TemporaryDirectory() as tmp:
        index = 0
        while len(tiles) < len(picks):
            ok, frame = capture.read()
            if not ok:
                break
            if index in picks:
                shrink = min(1.0, 480 / frame.shape[1])
                frame = cv2.resize(frame, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
                for face in picks[index]['faces']:
                    x, y, w, h = (int(v * shrink) for v in face['box'])
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                tile = Path(tmp) / f'{index:08d}.png'
                cv2.imwrite(str(tile), frame)
                tiles.append((index, tile))
            index += 1
        capture.release()
        target = Path(out_sheet)
        target.parent.mkdir(parents=True, exist_ok=True)
        size = _save_under(_compose(tiles, fps, 4, f'{Path(path).name}  faces  {len(tiles)} frames'), target, max_kb)
    return {'path': str(target), 'indices': [i for i, _ in tiles], 'bytes': size}


def summary_lines(result: dict) -> list[str]:
    meta, summary = result['meta'], result['summary']
    lines = [f'{meta["path"]}: {meta["width"]}x{meta["height"]} {meta["fps"]}fps {meta["frames"]} frames, sampled every {meta["every"]} ({summary["sampled"]} sampled)',
             f'faces in {summary["with_faces"]}/{summary["sampled"]} sampled frames, max {summary["max_faces"]} at once; histogram {summary["face_count_histogram"]}']
    if summary['union_box']:
        x, y, w, h = summary['union_box']
        fx, fy, fw, fh = summary['union_box_fraction']
        lines.append(f'union box [x={x} y={y} w={w} h={h}] px = [x={fx} y={fy} w={fw} h={fh}] of frame')
    if result.get('track_summary'):
        ts = result['track_summary']
        lines.append(f'track ({ts["pick"]}): {ts["tracked"]}/{ts["samples"]} samples, {ts["filled"]} filled across gaps, '
                     f'{ts["missing"]} without the subject; smoothed over {ts["smooth"]} s')
        if ts['missing'] > ts['samples'] * 0.2:
            lines.append('check: the subject is missing from over 20% of samples (out of frame, turned away, cut to another shot?); '
                         'the reframe holds the last position there')
    if result.get('reframe'):
        plan = result['reframe']
        xs = [k[1] for k in plan['keys']]
        lines.append(f'reframe {plan["size"][0]}x{plan["size"][1]}: crop {plan["crop"][0]}x{plan["crop"][1]} of the source, '
                     f'{len(plan["keys"])} keyframes, x {min(xs)}..{max(xs)} px')
        if plan.get('render'):
            lines.append(f'{plan["render"]["path"]}  {plan["render"]["frames"]} frames: use it as the clip\'s "src"')
        else:
            lines.append('-o FILE renders it; or ffmpeg -vf "<reframe.filter in --json>"')
    if result.get('sheet'):
        lines.append(f'sheet: {result["sheet"]["path"]} ({result["sheet"]["bytes"] // 1024} KB)')
    return lines


# ---------------------------------------------------------------- one subject over time
PICKS = ('largest', 'center', 'left', 'right')
HEADROOM = 0.4  # --reframe puts the face centre this far down the crop (and in the middle across)


def _centre(box) -> tuple[float, float]:
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def _first_pick(faces: list[dict], pick: str, width: float, height: float) -> dict:
    if pick == 'largest':
        return max(faces, key=lambda f: f['box'][2] * f['box'][3])
    if pick == 'center':
        return min(faces, key=lambda f: (_centre(f['box'])[0] - width / 2) ** 2 + (_centre(f['box'])[1] - height / 2) ** 2)
    return min(faces, key=lambda f: _centre(f['box'])[0] * (1 if pick == 'left' else -1))


def track_faces(sampled: list[dict], *, width: float, height: float, fps: float, pick: str = 'largest',
                max_gap: int = 12, smooth: float = 0.25) -> list[dict]:
    """One subject's face per sampled frame. The subject is picked by `pick` on the first frame with faces; each next
    sample takes the face nearest the last one (within one face size). A gap of up to `max_gap` frames is filled by
    straight-line interpolation (`filled`: true); after a longer gap the subject is picked again by the same rule.
    Then the box centre and size are smoothed by a Gaussian of sigma `smooth` seconds, within each tracked run; near a
    run's ends the window shrinks symmetrically (the first and last boxes stay as found, a steady move stays straight).
    Pure: no model."""
    if pick not in PICKS:
        raise RnError(f'--pick: one of {", ".join(PICKS)}')
    raw: list[list[float] | None] = []
    last, last_f = None, None
    for sample in sampled:
        faces = sample['faces']
        chosen = None
        if faces:
            if last is None or sample['f'] - last_f > max_gap:
                chosen = _first_pick(faces, pick, width, height)
            else:
                cx, cy = _centre(last)
                near = min(faces, key=lambda f: (_centre(f['box'])[0] - cx) ** 2 + (_centre(f['box'])[1] - cy) ** 2)
                nx, ny = _centre(near['box'])
                if math.hypot(nx - cx, ny - cy) <= max(last[2], last[3]):
                    chosen = near
        raw.append(list(chosen['box']) if chosen else None)
        if chosen:
            last, last_f = chosen['box'], sample['f']

    frames = [s['f'] for s in sampled]
    boxes: list[list[float] | None] = list(raw)
    known = [i for i, b in enumerate(raw) if b is not None]
    for a, b in zip(known, known[1:]):
        if b - a > 1 and frames[b] - frames[a] <= max_gap + 1:
            for i in range(a + 1, b):
                u = (frames[i] - frames[a]) / (frames[b] - frames[a])
                boxes[i] = [raw[a][k] + (raw[b][k] - raw[a][k]) * u for k in range(4)]

    step = (frames[1] - frames[0]) if len(frames) > 1 else 1
    sigma = smooth * fps / step if fps else 0
    smoothed = list(boxes)
    if sigma > 0:
        radius = max(1, int(math.ceil(3 * sigma)))
        weights = [math.exp(-0.5 * (d / sigma) ** 2) for d in range(-radius, radius + 1)]
        runs, start = [], None
        for i, box in enumerate([*boxes, None]):
            if box is not None and start is None:
                start = i
            elif box is None and start is not None:
                runs.append((start, i))
                start = None
        for lo, hi in runs:
            # centre x, centre y, width, height are smoothed, then turned back into a box
            series = [[*_centre(boxes[i]), boxes[i][2], boxes[i][3]] for i in range(lo, hi)]
            for i in range(lo, hi):
                reach = min(radius, i - lo, hi - 1 - i)  # a symmetric window: a steady move is not dragged at the run's ends
                acc, total = [0.0] * 4, 0.0
                for d in range(-reach, reach + 1):
                    w = weights[d + radius]
                    total += w
                    acc = [a + w * v for a, v in zip(acc, series[i - lo + d])]
                cx, cy, bw, bh = (a / total for a in acc)
                smoothed[i] = [cx - bw / 2, cy - bh / 2, bw, bh]
    track = []
    for sample, box, found in zip(sampled, smoothed, raw):
        track.append({'f': sample['f'], 't': sample['t'], 'box': [round(v, 1) for v in box] if box else None,
                      'center': [round(v, 1) for v in _centre(box)] if box else None,
                      'filled': box is not None and found is None})
    return track


def _hold(track: list[dict]) -> list[list[float]]:
    """Every sample's box, a missing one held from the nearest tracked sample (the last before, else the first after)."""
    boxes = [t['box'] for t in track]
    first = next((b for b in boxes if b), None)
    if first is None:
        raise RnError('no face was tracked: nothing to reframe on')
    held, current = [], first
    for box in boxes:
        current = box or current
        held.append(current)
    return held


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def reframe(track: list[dict], *, width: int, height: int, out_w: int, out_h: int, fps: float, zoom: float = 1.0,
            key_every: float = 0.5) -> dict:
    """A moving crop of the source with the output's aspect that keeps the tracked face in it: the face centre in the
    middle across and HEADROOM down, the window clamped inside the picture. `zoom` > 1 crops tighter. Keyframes every
    `key_every` seconds (and the last sample), straight lines between them. Pure: no model."""
    if zoom < 1:
        raise RnError('--zoom must be >= 1 (the crop cannot be larger than the source)')
    aspect = out_w / out_h
    cw, ch = (height * aspect, height) if width / height >= aspect else (width, width / aspect)
    cw, ch = _even(min(width, cw / zoom)), _even(min(height, ch / zoom))
    held = _hold(track)
    spacing = (track[1]['f'] - track[0]['f']) if len(track) > 1 else 1  # frames between samples (--every)
    stride = max(1, round(key_every * fps / spacing)) if fps else 1
    picks = sorted({*range(0, len(track), stride), len(track) - 1})
    keys = []
    for i in picks:
        cx, cy = _centre(held[i])
        x = min(max(cx - cw / 2, 0), width - cw)
        y = min(max(cy - ch * HEADROOM, 0), height - ch)
        keys.append([track[i]['f'], round(x, 1), round(y, 1)])
    return {'size': [out_w, out_h], 'crop': [cw, ch], 'zoom': zoom, 'keys': keys,
            'filter': f"crop={cw}:{ch}:x='{_piecewise([(f, x) for f, x, _ in keys])}':y='{_piecewise([(f, y) for f, _, y in keys])}'"
                      f',scale={out_w}:{out_h}:flags=lanczos,setsar=1'}


def _piecewise(points: list[tuple[int, float]]) -> str:
    """ffmpeg expression in n (frame number): straight lines through (frame, value), held before and after."""
    if all(v == points[0][1] for _, v in points):
        return f'{points[0][1]}'
    expr = f'{points[-1][1]}'
    for (n0, v0), (n1, v1) in reversed(list(zip(points, points[1:]))):
        expr = f'if(lt(n,{n1}),{v0}+({v1 - v0:.3f})*(n-{n0})/{n1 - n0},{expr})'
    return expr


def render_reframe(path: str, plan: dict, out: str) -> dict:
    from .ff import probe, run
    run(['ffmpeg', '-i', str(path), '-map', '0:v:0', '-map', '0:a?', '-vf', plan['filter'], '-c:v', 'libx264', '-crf', '14',
         '-preset', 'medium', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', '-y', str(out)])
    return {'path': str(out), 'frames': probe(out)['video']['frames']}
