"""ffmpeg / ffprobe plumbing shared by every command."""
from __future__ import annotations

import json
import re
import struct
import subprocess
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


class RnError(Exception):
    """A failure the agent can act on; the CLI prints it as one line."""


def run(args: list[str], *, quiet: bool = True, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Runs ffmpeg (or ffprobe); raises RnError with the tail of stderr on failure."""
    flags = ['-nostdin', *(['-v', 'error'] if quiet else [])] if args[0] == 'ffmpeg' else []
    command = [args[0], '-hide_banner', *flags, *args[1:]]
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        # The cause is usually an earlier line than the generic 'Task finished with error' tail.
        causes = [line for line in lines if any(k in line for k in ('rror', 'nvalid', 'not match', 'No such', 'Unable', 'failed'))]
        tail = '\n'.join((causes[:3] + lines[-2:]) if causes else lines[-6:])
        raise RnError(f'{args[0]} failed: {tail or "exit " + str(result.returncode)}')
    return result


@lru_cache(maxsize=None)
def has_filter(name: str) -> bool:
    """Whether this ffmpeg build has a filter (Homebrew's plain ffmpeg, for one, has no libass `subtitles`)."""
    filters = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in filters.splitlines())


def ffprobe_json(path: str | Path, *extra: str) -> dict:
    result = run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', *extra, str(path)])
    return json.loads(result.stdout)


def rational(value: str | None) -> Fraction | None:
    if not value or value in ('0/0', 'N/A'):
        return None
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None


def fps_text(fps: Fraction) -> str:
    return str(fps.numerator) if fps.denominator == 1 else f'{fps.numerator}/{fps.denominator}'


def mp4_faststart(path: str | Path) -> bool | None:
    """True when the moov atom precedes mdat (playable before fully downloaded); None for non-MP4."""
    with open(path, 'rb') as file:
        while True:
            header = file.read(8)
            if len(header) < 8:
                return None
            size, kind = struct.unpack('>I4s', header)
            if size == 1:
                size = struct.unpack('>Q', file.read(8))[0]
                body = size - 16
            elif size == 0:
                return None
            else:
                body = size - 8
            if kind == b'moov':
                return True
            if kind == b'mdat':
                return False
            file.seek(body, 1)


def probe(path: str | Path, *, exact: bool = False) -> dict:
    """Facts an edit depends on. Frame count comes from counting packets (or decoding with exact=True),
    never from the container duration, which an audio track can stretch."""
    path = Path(path)
    if not path.exists():
        raise RnError(f'no such file: {path}')
    info = ffprobe_json(path, '-count_frames' if exact else '-count_packets', '-select_streams', 'v:0')
    data = ffprobe_json(path)
    video_stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
    audio_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    fmt = data.get('format', {})
    result: dict = {
        'path': str(path),
        'format': fmt.get('format_name'),
        'duration': _float(fmt.get('duration')),
        'faststart': mp4_faststart(path) if 'mp4' in (fmt.get('format_name') or '') or 'mov' in (fmt.get('format_name') or '') else None,
        'video': None,
        'audio': None,
    }
    if video_stream:
        fps = rational(video_stream.get('avg_frame_rate')) or rational(video_stream.get('r_frame_rate'))
        # Constant rate when the average rate equals the base rate; a VFR phone clip averages below it.
        cfr = rational(video_stream.get('avg_frame_rate')) == rational(video_stream.get('r_frame_rate'))
        frames = video_stream.get('nb_read_frames') if exact else video_stream.get('nb_read_packets')
        frames = int(frames) if frames not in (None, 'N/A') else None
        single_image = (video_stream.get('codec_name') in ('png', 'mjpeg', 'webp', 'bmp', 'tiff')) and (frames or 0) <= 1
        sar = video_stream.get('sample_aspect_ratio')
        # ffmpeg's native vp8/vp9 decoders drop the alpha side channel that WebM carries as alpha_mode=1.
        vpx_alpha = str((video_stream.get('tags') or {}).get('alpha_mode') or (video_stream.get('tags') or {}).get('ALPHA_MODE') or '') == '1'
        rotation = 0
        for side in video_stream.get('side_data_list', []) or []:
            if 'rotation' in side:
                rotation = int(side['rotation'])
        result['video'] = {
            'codec': video_stream.get('codec_name'),
            'profile': video_stream.get('profile'),
            'width': video_stream.get('width'),
            'height': video_stream.get('height'),
            'sar': sar if sar and sar != '0:1' else '1:1',
            'pix_fmt': video_stream.get('pix_fmt'),
            'fps': fps_text(fps) if fps and not single_image else None,
            'fps_float': round(float(fps), 6) if fps and not single_image else None,
            'frames': frames,
            'duration': None if single_image else (_float(video_stream.get('duration')) or (float(frames / fps) if frames and fps else None)),
            'color_space': video_stream.get('color_space'),
            'color_transfer': video_stream.get('color_transfer'),
            'color_primaries': video_stream.get('color_primaries'),
            'rotation': rotation,
            'alpha': vpx_alpha or (video_stream.get('pix_fmt') or '').startswith(('yuva', 'rgba', 'argb', 'bgra', 'abgr', 'gbrap', 'ya')),
            'alpha_decoder': ({'vp9': 'libvpx-vp9', 'vp8': 'libvpx'}.get(video_stream.get('codec_name')) if vpx_alpha else None),
            'still': single_image,
            'cfr': cfr and not single_image,
        }
    if audio_stream:
        result['audio'] = {
            'codec': audio_stream.get('codec_name'),
            'sample_rate': int(audio_stream['sample_rate']) if audio_stream.get('sample_rate') else None,
            'channels': audio_stream.get('channels'),
            'duration': _float(audio_stream.get('duration')),
        }
    return result


def _float(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def source_fps(info: dict) -> Fraction:
    video = info.get('video') or {}
    if not video.get('fps'):
        raise RnError(f'{info["path"]}: no video frame rate (is it an image? use "image" instead of "src")')
    return Fraction(video['fps'])


_MATRIX_BY_TAG = {'bt709': 'bt709', 'smpte170m': 'bt601', 'bt470bg': 'bt601', 'bt2020nc': 'bt2020', 'bt2020c': 'bt2020',
                  'smpte240m': 'smpte240m', 'fcc': 'fcc'}


def source_matrix(video: dict) -> str:
    """The YUV matrix to decode a source with. Tagged sources use their tag; untagged ones follow player convention
    (HD and up = BT.709, SD = BT.601; JPEG/JFIF is always BT.601). RGB sources ignore it. Both assemble and finish
    use this, so one clip never looks different depending on the command."""
    tagged = _MATRIX_BY_TAG.get(video.get('color_space') or '')
    if tagged:
        return tagged
    if (video.get('pix_fmt') or '').startswith('yuvj'):
        return 'bt601'
    return 'bt709' if (video.get('height') or 0) >= 720 else 'bt601'


def to_bt709(matrix: str) -> str:
    """scale options converting from `matrix` (RGB inputs: any) to limited-range BT.709 YUV."""
    return f'in_color_matrix={matrix}:out_color_matrix=bt709:out_range=tv'


HEX_COLOR = re.compile(r'^#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$')


def ff_color(value: str) -> str:
    """'#112233' -> '0x112233' for lavfi; named colors pass through."""
    if HEX_COLOR.match(value):
        return '0x' + value.lstrip('#')
    return value
