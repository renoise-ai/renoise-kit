import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

STEP = 25  # luma per digit in the ramp clip: left half Y = 16 + STEP*(N // 8), right half Y = 16 + STEP*(N % 8)


def ffmpeg(*args: str) -> None:
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', *args, '-y'], check=True)


def has_filter(name: str) -> bool:
    filters = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in filters.splitlines())


@pytest.fixture(scope='session')
def media(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp('media')
    # 60-frame 24 fps lossless grey ramp with a tone: the decoded brightness of any frame names its source index.
    ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s=320x240:r=24:d=2.5,format=yuv420p,geq=lum='16+{STEP}*if(lt(X\\,W/2)\\,trunc(N/8)\\,mod(N\\,8))':cb=128:cr=128",
           '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2.5',
           '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(root / 'ramp.mp4'))
    # 30 fps 1280x720 colour bars, 2 s, quieter tone.
    ffmpeg('-f', 'lavfi', '-i', 'smptehdbars=s=1280x720:r=30:d=2', '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=44100:duration=2',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(root / 'bars.mp4'))
    # No audio, 25 fps.
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=25:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(root / 'silent.mp4'))
    # Moov at the end (no faststart) and a hot full-scale square wave: both must be caught by qa / fixed by finish.
    ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=2', '-f', 'lavfi',
           '-i', "aevalsrc='0.999*sgn(sin(2*PI*220*t))':s=48000:d=2",
           '-c:v', 'libx264', '-pix_fmt', 'yuv444p', '-c:a', 'aac', '-shortest', str(root / 'hot.mp4'))
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.95*sin(2*PI*330*t)*(0.6+0.4*sin(2*PI*0.5*t))':s=44100:d=6", str(root / 'music.wav'))
    ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*180*t)*gt(mod(t\\,1)\\,0.4)':s=48000:d=3", str(root / 'voice.wav'))
    overlay = Image.new('RGBA', (80, 60), (255, 0, 0, 255))
    overlay.save(root / 'red.png')
    Image.new('RGB', (400, 300), (40, 170, 255)).save(root / 'card.png')
    return root


def write_edl(directory: Path, edl: dict, name: str = 'edit.json') -> Path:
    path = directory / name
    path.write_text(json.dumps(edl), encoding='utf-8')
    return path


def ramp_index(png: Path) -> int:
    """Source frame index of a decoded ramp frame. The left half encodes N // 8 and the right half N % 8, 25 luma
    levels apart, so no platform's yuv->rgb rounding can move a frame to its neighbour."""
    image = Image.open(png).convert('L')
    w, h = image.size

    def digit(x0: int) -> int:
        crop = image.crop((x0 + w // 16, h // 4, x0 + w // 2 - w // 16, 3 * h // 4))
        values = list(crop.tobytes())
        grey = sum(values) / len(values)
        return round(grey * 219 / 255 / STEP)

    return digit(0) * 8 + digit(w // 2)
