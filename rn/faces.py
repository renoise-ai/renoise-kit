"""rn faces: per-frame face boxes and landmarks (YuNet), in source pixels."""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from fractions import Fraction
from pathlib import Path

from .ff import RnError
from .frames import _compose, _save_under

DEFAULT_MODEL = '/opt/media-sandbox/models/face_detection_yunet_2023mar.onnx'
DETECT_LONG_SIDE = 960  # larger frames are detected at this size, then mapped back to source pixels
SHEET_FRAMES = 12


def _face(row, scale: float) -> dict:
    """YuNet row: x y w h, right eye, left eye, nose, right mouth corner, left mouth corner (all as seen in the image), score."""
    x, y, w, h, *marks, score = (float(v) for v in row[:15])
    point = lambda i: [round(marks[2 * i] / scale, 1), round(marks[2 * i + 1] / scale, 1)]  # noqa: E731
    return {'box': [round(v / scale, 1) for v in (x, y, w, h)], 'score': round(score, 3),
            'eyes': [point(0), point(1)], 'nose': point(2), 'mouth': [point(3), point(4)]}


def faces_command(path: str, *, every: int = 1, model: str | None = None, min_score: float = 0.6,
                  out_sheet: str | None = None, max_kb: int = 125) -> dict:
    """Measures where faces are in every `every`-th decoded frame. Measurement only: it never alters, blurs or crops a face.
    Use the boxes to keep text/captions off faces, or to place separate graphics that follow a subject.
    Coordinates are source pixels; `f` is the exact decoded frame index. `eyes` and `mouth` are ordered left-to-right as seen in the image."""
    import cv2

    model_path = model or os.environ.get('RN_YUNET_MODEL') or DEFAULT_MODEL
    if not Path(model_path).is_file():
        raise RnError(f'face model not found: {model_path} (set RN_YUNET_MODEL or pass --model)')
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
    if result.get('sheet'):
        lines.append(f'sheet: {result["sheet"]["path"]} ({result["sheet"]["bytes"] // 1024} KB)')
    return lines
