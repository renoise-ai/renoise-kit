import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

cv2 = pytest.importorskip('cv2')
import numpy as np  # noqa: E402

from rn.faces import DEFAULT_MODEL, faces_command, summary_lines  # noqa: E402
from rn.ff import RnError  # noqa: E402

MODEL_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
LENA_URL = 'https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg'
FRAMES, CANVAS_W, CANVAS_H, FACE = 20, 960, 540, 256


def fetch(cache: Path, url: str, name: str) -> Path:
    target = cache / name
    if not target.exists():
        try:
            target.write_bytes(urllib.request.urlopen(url, timeout=30).read())
        except Exception as error:
            pytest.skip(f'cannot download {name}: {error}')
    return target


@pytest.fixture(scope='session')
def model(request) -> str:
    local = os.environ.get('RN_YUNET_MODEL') or DEFAULT_MODEL
    if Path(local).is_file():
        return local
    cache = Path(request.config.cache.mkdir('rn-faces'))
    return str(fetch(cache, MODEL_URL, 'yunet.onnx'))


@pytest.fixture(scope='session')
def lena_clip(request, tmp_path_factory) -> Path:
    cache = Path(request.config.cache.mkdir('rn-faces'))
    lena = cv2.imread(str(fetch(cache, LENA_URL, 'lena.jpg')))
    lena = cv2.resize(lena, (FACE, FACE))
    clip = tmp_path_factory.mktemp('faces') / 'lena.mp4'
    frames_dir = clip.parent / 'png'
    frames_dir.mkdir()
    for n in range(FRAMES):  # image slides left to right on a grey canvas
        canvas = np.full((CANVAS_H, CANVAS_W, 3), 128, np.uint8)
        x = 40 + n * 30
        canvas[100:100 + FACE, x:x + FACE] = lena
        cv2.imwrite(str(frames_dir / f'{n:04d}.png'), canvas)
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-framerate', '10', '-i', str(frames_dir / '%04d.png'),
                    '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', '-y', str(clip)], check=True)
    return clip


def test_missing_model(lena_clip):
    with pytest.raises(RnError, match='model not found'):
        faces_command(str(lena_clip), model='/nonexistent.onnx')


def test_faces_follow_image(lena_clip, model):
    result = faces_command(str(lena_clip), model=model)
    assert result['meta']['width'] == CANVAS_W and result['meta']['frames'] == FRAMES
    assert result['summary']['with_faces'] >= FRAMES * 0.8
    detected = [(s['f'], max(s['faces'], key=lambda f: f['score'])) for s in result['frames'] if s['faces']]
    for f, face in detected:
        x, y, w, h = face['box']
        left, top = 40 + f * 30, 100
        assert left - 20 <= x + w / 2 <= left + FACE + 20, (f, face)  # source-pixel coordinates, inside the pasted image
        assert top - 20 <= y + h / 2 <= top + FACE + 20
        assert len(face['eyes']) == 2 and len(face['mouth']) == 2 and len(face['nose']) == 2
    centers = [face['box'][0] + face['box'][2] / 2 for _, face in detected]
    assert centers[-1] - centers[0] > 200  # boxes travel with the image
    assert result['summary']['union_box_fraction'][2] > 0.3


def test_every_and_sheet(lena_clip, model, tmp_path):
    sheet = tmp_path / 'sheet.jpg'
    result = faces_command(str(lena_clip), model=model, every=5, out_sheet=str(sheet), max_kb=100)
    assert [s['f'] for s in result['frames']] == [0, 5, 10, 15]
    assert result['summary']['sampled'] == 4
    assert sheet.exists() and sheet.stat().st_size <= 100 * 1024
    assert result['sheet']['indices'] == [0, 5, 10, 15]
    assert any('faces in' in line for line in summary_lines(result))


def test_no_face(tmp_path, model):
    clip = tmp_path / 'nofaces.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=25:d=1',
                    '-pix_fmt', 'yuv420p', '-y', str(clip)], check=True)
    result = faces_command(str(clip), model=model)
    assert result['summary']['with_faces'] == 0 and result['summary']['union_box'] is None
    assert result['summary']['face_count_histogram'] == {'0': 25}
