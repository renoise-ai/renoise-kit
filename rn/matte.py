"""rn matte: the subject's alpha in every frame (rembg), as a numbered grey PNG sequence (white = subject) that an EDL
uses as an overlay "mask" (text behind a person, a graphic that stays off them) or, with --cutout, as the cut-out
subject itself. Measurement-grade, not roto: check the edges on a sheet.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .ff import RnError, probe, run, source_fps

MODELS = ('isnet-general-use', 'u2net_human_seg', 'u2net', 'u2netp', 'silueta', 'birefnet-general', 'birefnet-portrait')


def matte_command(path: str, *, out: str, model: str, every: int, cutout: bool) -> dict:
    from PIL import Image
    from rembg import new_session, remove

    if every < 1:
        raise RnError('--every must be >= 1')
    info = probe(path)
    video = info['video']
    if not video or video['still']:
        raise RnError(f'{path}: not a video (rembg itself handles a single image)')
    fps = source_fps(info)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('*.png'):
        stale.unlink()
    session = new_session(model)
    with tempfile.TemporaryDirectory() as tmp:
        # Every decoded frame in order (no timestamps involved), so output n is input frame n - 1.
        run(['ffmpeg', '-i', path, '-map', '0:v:0', '-fps_mode', 'passthrough', '-y', str(Path(tmp) / '%06d.png')])
        frames = sorted(Path(tmp).glob('*.png'))
        mask = None
        for k, png in enumerate(frames):
            image = Image.open(png).convert('RGB')
            if k % every == 0 or mask is None:
                mask = remove(image, session=session, only_mask=True).convert('L')  # held in between with --every
            target = out_dir / f'{k + 1:05d}.png'
            if cutout:
                rgba = image.convert('RGBA')
                rgba.putalpha(mask)
                rgba.save(target)
            else:
                mask.save(target)
    fps_text = str(fps.numerator) if fps.denominator == 1 else f'{fps.numerator}/{fps.denominator}'
    pattern = f'{out_dir.name}/%05d.png'
    use = {'frames': pattern, 'fps': fps_text}
    return {'dir': str(out_dir), 'frames': len(frames), 'model': model, 'every': every, 'cutout': cutout,
            'edl': use if cutout else {'mask': {**use, 'mode': 'luma'}}}


def summary_lines(result: dict) -> list[str]:
    kind = 'RGBA cut-outs' if result['cutout'] else 'grey mattes (white = subject)'
    return [f'{result["dir"]}/%05d.png  {result["frames"]} {kind}  model {result["model"]}, computed every {result["every"]} frame(s)',
            f'EDL: {json.dumps(result["edl"])}' + ('' if result['cutout'] else '  (on an overlay; "invert": true keeps it off the subject)')]
