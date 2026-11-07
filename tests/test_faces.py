import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

cv2 = pytest.importorskip('cv2')
import numpy as np  # noqa: E402

from rn.faces import DEFAULT_MODEL, faces_command, summary_lines  # noqa: E402
from rn.ff import RnError  # noqa: E402
from rn.agent import PROOF_KB, proof
from types import SimpleNamespace

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


def test_default_model_is_fetched_once_and_checked(lena_clip, model, tmp_path, monkeypatch):
    import rn.faces as faces
    monkeypatch.delenv('RN_YUNET_MODEL', raising=False)
    monkeypatch.setattr(faces, 'DEFAULT_MODEL', str(tmp_path / 'not-baked' / 'face_detection_yunet_2023mar.onnx'))
    monkeypatch.setattr(faces, 'MODEL_URL', Path(model).resolve().as_uri())
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    monkeypatch.setattr(faces, 'MODEL_SHA256', '0' * 64)
    with pytest.raises(RnError, match='checksum mismatch'):
        faces_command(str(lena_clip), every=10)
    import hashlib
    monkeypatch.setattr(faces, 'MODEL_SHA256', hashlib.sha256(Path(model).read_bytes()).hexdigest())
    assert faces_command(str(lena_clip), every=10)['summary']['with_faces'] > 0
    assert (tmp_path / 'home' / '.cache' / 'rn' / 'face_detection_yunet_2023mar.onnx').is_file()


@pytest.fixture(scope='session')
def portrait_clip(request, tmp_path_factory) -> Path:
    """The portrait at the canvas's full height, held still for 2 s: its face covers several percent of the picture."""
    cache = Path(request.config.cache.mkdir('rn-faces'))
    lena = cv2.resize(cv2.imread(str(fetch(cache, LENA_URL, 'lena.jpg'))), (CANVAS_H, CANVAS_H))
    canvas = np.full((CANVAS_H, CANVAS_W, 3), 128, np.uint8)
    x = (CANVAS_W - CANVAS_H) // 2
    canvas[:, x:x + CANVAS_H] = lena
    folder = tmp_path_factory.mktemp('portrait')
    cv2.imwrite(str(folder / 'portrait.png'), canvas)
    clip = folder / 'portrait.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-loop', '1', '-framerate', '10', '-t', '2',
                    '-i', str(folder / 'portrait.png'), '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', '-y', str(clip)],
                   check=True)
    return clip


def test_shots_counts_frames_with_a_large_face(portrait_clip, lena_clip, model):
    """rn shots --faces: the same detector as rn faces, per shot. A face counts when it covers 2%+ of the picture: the
    full-height portrait's does in every checked frame; the small sliding one's (about 1.5%) does not."""
    from rn.shots import FACE_MIN_AREA, shots_command

    faces = shots_command(str(portrait_clip), jobs=1, faces=True, face_model=model)['shots'][0]['faces']
    assert faces['checked'] >= 2 and faces['frames_with_face'] == 1.0 and faces['largest_area'] >= FACE_MIN_AREA
    small = shots_command(str(lena_clip), jobs=1, faces=True, face_model=model)['shots'][0]['faces']
    assert small['frames_with_face'] == 0.0 and small['largest_area'] < FACE_MIN_AREA


def test_track_and_reframe_render(lena_clip, model, tmp_path):
    out = tmp_path / 'vertical.mp4'
    result = faces_command(str(lena_clip), model=model, reframe_size=(270, 480), out=str(out))
    assert result['track_summary']['tracked'] >= FRAMES * 0.8
    assert result['reframe']['render']['frames'] == FRAMES
    xs = [k[1] for k in result['reframe']['keys']]
    assert xs[-1] - xs[0] > 200  # the window follows the image across
    assert any('reframe 270x480' in line for line in summary_lines(result))
    shown = proof(SimpleNamespace(command='faces', file=str(lena_clip), no_proof=False), result)
    assert shown == str(tmp_path / 'vertical.proof.jpg') and Path(shown).stat().st_size <= PROOF_KB * 1024
