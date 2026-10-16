"""rn assemble: render an EDL to a finished MP4, frame-exact. rn finish: the same output standard for any video.

Pipeline (spec/mcp/03-media-kit.md §4-5):
1. every main-track clip -> its own intermediate at the output size/fps with exactly `frames` frames, plus a WAV
   of exactly the same length (a slot engine: each piece is checkable on its own);
2. each transition is rendered from the two slot ends it joins, then the pieces are read back in order through the concat
   demuxer (one decoder at a time, so memory does not grow with the clip count), overlay tracks are stacked, subtitles
   burned and the video encoded;
3. audio is mixed separately (clip audio with matching crossfades + audio items + optional ducking), gained to the
   LUFS target and limited until the ENCODED true peak fits; then both streams are copied into the MP4.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from . import __version__
from . import audio as audio_mod
from .edl import Clip, Edit, cache_dir, load, source_offsets
from .ff import RnError, ff_color, has_filter, probe, run, source_matrix, to_bt709

SR = audio_mod.SAMPLE_RATE
# Lossless slots: the only lossy step is the final encode.
INTERMEDIATE = ['-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0', '-pix_fmt', 'yuv420p', '-an']


def _fit(clip: Clip, width: int, height: int, background: str) -> str:
    # Every source is converted to BT.709 here, once; RGB images and solids would otherwise get swscale's BT.601.
    scale = f'scale={width}:{height}:{to_bt709(clip.matrix)}'
    if clip.fit == 'cover':
        return f'{scale}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}'
    if clip.fit == 'contain':
        return (f'{scale}:force_original_aspect_ratio=decrease:flags=lanczos,'
                f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={ff_color(background)}')
    return f'{scale}:flags=lanczos'


REMAP_SEGMENTS = 64  # a speed ramp's time map as at most this many straight pieces (well under a frame of error)


def _remap(points: list, frames: int, fps: Fraction) -> str:
    """setpts expression in T (source seconds from the clip's in) -> output seconds: the inverse of the ramp's integral,
    piecewise linear through its value at every few output frames."""
    offsets = source_offsets(points, frames, fps)
    step = max(1, math.ceil(frames / REMAP_SEGMENTS))
    marks = [(offsets[n], n / float(fps)) for n in [*range(0, frames, step), frames]]
    tau_end, t_end = marks[-1]
    expr = f'({t_end:.6f}+(T-{tau_end:.6f})/{points[-1][1]:.6f})'  # past the last mark: the final speed
    for (tau0, t0), (tau1, t1) in reversed(list(zip(marks, marks[1:]))):
        if tau1 > tau0:
            expr = f'if(lt(T,{tau1:.6f}),{t0:.6f}+(T-{tau0:.6f})*{(t1 - t0) / (tau1 - tau0):.6f},{expr})'
    return expr


def _timing(clip: Clip, fps: Fraction) -> str:
    """Source range -> output frame grid. Starts half a source frame early so float rounding never drops the first frame."""
    start = max(0.0, clip.start_s - 0.5 / float(clip.src_fps)) if clip.src_fps else clip.start_s
    chain = f'setpts=PTS-STARTPTS,trim=start={start:.6f},setpts=PTS-STARTPTS'
    ramp = (clip.keys or {}).get('speed')
    if ramp:
        chain += f",setpts='{_remap(ramp, clip.frames, fps)}'/TB"
    elif clip.speed != 1:
        chain += f',setpts=PTS/{clip.speed:.6f}'
    # Slow motion blends neighbouring frames instead of repeating them (a visible stutter at 0.3-1x).
    slow = min(v for _, v, _ in ramp) < 1 if ramp else clip.speed < 1
    chain += f',framerate=fps={fps}' if slow else f',fps={fps}'
    return chain


_EASE = {
    'linear': 'U', 'hold': '0', 'in': 'U*U', 'out': '(1-(1-U)*(1-U))', 'in3': 'U*U*U', 'out3': '(1-pow(1-U,3))',
    'inout': 'if(lt(U,0.5),2*U*U,1-2*(1-U)*(1-U))', 'inout3': 'if(lt(U,0.5),4*U*U*U,1-4*pow(1-U,3))',
}


def _curve(points: list[tuple[float, float, str]] | None, var: str, default: float) -> str:
    """Keyframes -> an ffmpeg expression in `var` (seconds): held before the first key and after the last."""
    if not points:
        return repr(float(default))
    expr = repr(float(points[-1][1]))
    for (t0, v0, _), (t1, v1, ease) in reversed(list(zip(points, points[1:]))):
        u = f'clip(({var}-{t0})/{t1 - t0},0,1)'
        segment = f'({v0}+({v1 - v0})*({_EASE[ease].replace("U", u)}))'
        expr = f'if(lt({var},{t1}),{segment},{expr})'
    return f'if(lt({var},{points[0][0]}),{points[0][1]},{expr})'


def _perspective(keys: dict, fps: Fraction, *, centre: tuple[str, str], scale_default: float = 1.0) -> str:
    """Scale (about `centre`), rotate (degrees) and move via perspective's source corners: smooth, sub-pixel, alpha kept.
    `centre` are expressions in t for where the layer's centre lands; the layer's own centre is (W/2, H/2) of its input
    for the main track, (cx0, cy0) passed in through the same centre pair for overlays."""
    # perspective's `on` counts output frames from 1 (measured on ffmpeg 9), so frame 0 is on-1 = 0.
    t = f'((on-1)/{float(fps)})'
    scale = _curve(keys.get('scale'), t, scale_default)
    angle = f'(({_curve(keys.get("rotate"), t, 0)})*PI/180)'
    cx, cy = centre
    corners = []
    for k, (ox, oy) in enumerate((('0', '0'), ('W', '0'), ('0', 'H'), ('W', 'H'))):
        dx, dy = f'(({ox})-({cx}))/({scale})', f'(({oy})-({cy}))/({scale})'
        corners.append(f"x{k}='{{px}}+{dx}*cos({angle})+{dy}*sin({angle})':y{k}='{{py}}-{dx}*sin({angle})+{dy}*cos({angle})'")
    return ':'.join(corners)


def _video_input(clip: Clip, fps: Fraction, size: tuple[int, int]) -> list[str]:
    if clip.kind == 'color':
        # Generated as RGB so the BT.709 conversion downstream gets the colour right.
        return ['-f', 'lavfi', '-i', f'color=c={ff_color(clip.color or "black")}:s={size[0]}x{size[1]}:r={fps},format=rgb24']
    if clip.kind == 'image':
        return ['-loop', '1', '-framerate', str(fps), '-i', str(clip.source)]
    if clip.kind == 'sequence':
        return ['-framerate', str(clip.src_fps), '-start_number', str(clip.seq_start), '-i', str(clip.source)]
    return [*(['-c:v', clip.decoder] if clip.decoder else []), '-i', str(clip.source)]


def _render_slot(clip: Clip, edit: Edit, out: Path) -> None:
    chain = _timing(clip, edit.fps) + ',' if clip.kind in ('video', 'sequence') else ''
    chain += _fit(clip, edit.width, edit.height, edit.background)
    if clip.keys:
        # Main-track move: the plate scales/rotates about the frame centre and shifts by x/y (fractions of the frame).
        t = f'((on-1)/{float(edit.fps)})'
        cx = f'(W/2+({_curve(clip.keys.get("x"), t, 0)})*W)'
        cy = f'(H/2+({_curve(clip.keys.get("y"), t, 0)})*H)'
        chain += ',perspective=' + _perspective(clip.keys, edit.fps, centre=(cx, cy)).replace('{px}', 'W/2').replace('{py}', 'H/2') \
            + ':eval=frame:interpolation=cubic'
    if clip.vf:
        chain += f',{clip.vf}'
    chain += f',setsar=1,format=yuv420p,{audio_mod.BT709}'
    # Hold the last frame if the source runs short, then cut to the exact frame count.
    chain += f',tpad=stop_mode=clone:stop={clip.frames},trim=end_frame={clip.frames},setpts=N/({edit.fps}*TB)'
    run(['ffmpeg', *_video_input(clip, edit.fps, (edit.width, edit.height)), '-filter_complex', f'[0:v]{chain}[v]', '-map', '[v]',
         *INTERMEDIATE, '-r', str(edit.fps), '-y', str(out)])
    frames = probe(out)['video']['frames']
    if frames != clip.frames:
        raise RnError(f'{clip.where}: slot rendered {frames} frames, expected {clip.frames}')


def _atempo(speed: float) -> str:
    factors = []
    while speed > 2.0:
        factors.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ','.join(f'atempo={f:.6f}' for f in factors if abs(f - 1) > 1e-9)


def _gain(keys: dict | None, af: str | None) -> str:
    """keys.gain_db (dB over t, the stream's own seconds) and a raw "af" chain, as filters to append."""
    chain = ''
    if keys and 'gain_db' in keys:
        chain += f",volume=volume='pow(10,({_curve(keys['gain_db'], 't', 0)})/20)':eval=frame"
    return chain + (f',{af}' if af else '')


def _render_slot_audio(clip: Clip, edit: Edit, out: Path, fades: tuple[float, float] = (0.0, 0.0)) -> None:
    """The clip's own sound, exactly as long as its slot; `fades` (in, out) soften hard cuts (mix.cut_fade)."""
    samples = round(clip.frames / edit.fps * SR)
    if clip.audio and clip.has_audio:
        chain = f'asetpts=PTS-STARTPTS,atrim=start={clip.start_s:.6f},asetpts=PTS-STARTPTS'
        tempo = _atempo(clip.speed) if clip.speed != 1 else ''
        chain += (',' + tempo if tempo else '') + f',aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo'
        if clip.gain_db:
            chain += f',volume={clip.gain_db}dB'
        chain += _gain(clip.keys, clip.af)
        chain += f',apad,atrim=end_sample={samples}'
        if fades[0]:
            chain += f',afade=t=in:d={fades[0]:.4f}'
        if fades[1]:
            chain += f',afade=t=out:st={max(0.0, samples / SR - fades[1]):.6f}:d={fades[1]:.4f}'
        run(['ffmpeg', '-i', str(clip.source), '-filter_complex', f'[0:a]{chain}[a]', '-map', '[a]', '-c:a', 'pcm_f32le', '-y', str(out)])
    else:
        run(['ffmpeg', '-f', 'lavfi', '-i', f'anullsrc=r={SR}:cl=stereo', '-af', f'atrim=end_sample={samples}',
             '-c:a', 'pcm_f32le', '-y', str(out)])


def _join(edit: Edit, slots: list[Path], work: Path) -> Path:
    """The main track as a concat-demuxer list: slot bodies and pre-rendered transitions, in timeline order.

    Opening every slot in one graph keeps a decoder and its frame queues alive per clip: 61 1080p clips ran an 8 GB
    sandbox out of memory. A transition only needs the last frames of one slot and the first of the next."""
    grid = _grid(edit)
    pieces: list[Path] = []
    for i, (clip, slot) in enumerate(zip(edit.base, slots)):
        head = clip.transition_frames
        tail = edit.base[i + 1].transition_frames if i + 1 < len(edit.base) else 0
        if head:
            previous = edit.base[i - 1]
            piece = work / f'trans{i:03d}.mkv'
            duration = head / edit.fps
            run(['ffmpeg', '-i', str(slots[i - 1]), '-i', str(slot), '-filter_complex',
                 f'[0:v]trim=start_frame={previous.frames - head},{grid}[a];[1:v]trim=end_frame={head},{grid}[b];'
                 f'[a][b]xfade=transition={clip.transition}:duration={float(duration):.6f}:offset=0,{grid}[v]',
                 '-map', '[v]', *INTERMEDIATE, '-r', str(edit.fps), '-y', str(piece)])
            pieces.append(piece)
        if clip.frames - head - tail <= 0:
            continue
        if not head and not tail:
            pieces.append(slot)
            continue
        piece = work / f'body{i:03d}.mkv'
        run(['ffmpeg', '-i', str(slot), '-filter_complex', f'[0:v]trim=start_frame={head}:end_frame={clip.frames - tail},{grid}[v]',
             '-map', '[v]', *INTERMEDIATE, '-r', str(edit.fps), '-y', str(piece)])
        pieces.append(piece)
    listing = work / 'main.txt'
    listing.write_text(''.join(f"file '{piece.resolve()}'\n" for piece in pieces))
    return listing


def _grid(edit: Edit) -> str:
    # xfade and concat need one shared timebase: 1/fps everywhere, pts = frame number. Slots already hold exactly
    # `frames` frames; renumber them instead of resampling (fps would drop one on ms timestamps).
    return f'settb={edit.fps.denominator}/{edit.fps.numerator},setpts=N,fps={edit.fps}'


def _overlay_size(clip: Clip, edit: Edit) -> tuple[int, int]:
    """Pixel size an overlay is drawn at (its scale before keys)."""
    if clip.kind == 'color':
        return clip.w or edit.width, clip.h or edit.height
    src_w, src_h = clip.src_size or (edit.width, edit.height)
    if clip.w and clip.h:
        return clip.w, clip.h
    if clip.w:
        return clip.w, round(src_h * clip.w / src_w / 2) * 2
    if clip.h:
        return round(src_w * clip.h / src_h / 2) * 2, clip.h
    return src_w, src_h


MARGIN = 4  # transparent px around a moving overlay's canvas


def _moves(clip: Clip) -> bool:
    return bool(clip.keys) and any(k in clip.keys for k in ('scale', 'rotate', 'x', 'y'))


def _overlay_chain(clip: Clip, edit: Edit, index: int) -> str:
    parts = []
    if clip.kind in ('video', 'sequence'):
        parts.append(_timing(clip, edit.fps))
    size = f'{clip.w or -2}:{clip.h or -2}' if (clip.w or clip.h) and clip.kind != 'color' else 'iw:ih'
    parts.append(f'scale={size}:flags=lanczos:{to_bt709(clip.matrix)}')
    if clip.vf:
        parts.append(clip.vf)
    parts.append(f'format=yuva420p,{audio_mod.BT709}')
    # Integer frame pts on the output grid (as _join): a float seconds offset truncates one frame early.
    parts.append(f'tpad=stop_mode=clone:stop={clip.frames},trim=end_frame={clip.frames},'
                 f'settb={edit.fps.denominator}/{edit.fps.numerator},setpts=N')
    seconds = clip.frames / edit.fps
    if clip.opacity < 1:
        parts.append(f'colorchannelmixer=aa={clip.opacity:.4f}')
    if clip.fade_in:
        parts.append(f'fade=t=in:st=0:d={clip.fade_in:.4f}:alpha=1')
    if clip.fade_out:
        parts.append(f'fade=t=out:st={max(0.0, float(seconds) - clip.fade_out):.4f}:d={clip.fade_out:.4f}:alpha=1')
    if clip.keys and 'opacity' in clip.keys:
        # T is seconds from the overlay's first frame here (pts = frame number, timebase 1/fps).
        parts.append(f"geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='alpha(X,Y)*({_curve(clip.keys['opacity'], 'T', 1)})'")
    if _moves(clip):
        # Put the layer on a transparent canvas the size of the frame, then move it there. perspective repeats the
        # canvas edge for anything it samples outside, so the canvas keeps a transparent margin on every side and is
        # overlaid at (-MARGIN, -MARGIN).
        ow, oh = _overlay_size(clip, edit)
        cw, ch = max(edit.width, ow) + 2 * MARGIN, max(edit.height, oh) + 2 * MARGIN
        t = f'((on-1)/{float(edit.fps)})'
        cx = f'({_curve(clip.keys.get("x"), t, clip.x)}+{ow / 2 + MARGIN})'
        cy = f'({_curve(clip.keys.get("y"), t, clip.y)}+{oh / 2 + MARGIN})'
        parts.append(f'pad={cw}:{ch}:{MARGIN}:{MARGIN}:color=black@0')
        parts.append('perspective=' + _perspective(clip.keys, edit.fps, centre=(cx, cy))
                     .replace('{px}', str(ow / 2 + MARGIN)).replace('{py}', str(oh / 2 + MARGIN)) + ':eval=frame:interpolation=cubic')
    parts.append(f'setpts=PTS+{clip.timeline_start}')
    return f'[{index}:v]' + ','.join(parts) + f'[o{index}]'


def _render_video(edit: Edit, slots: list[Path], work: Path, out: Path, span: tuple[int, int]) -> None:
    inputs = ['-f', 'concat', '-safe', '0', '-i', str(_join(edit, slots, work))]
    lines, current = [f'[0:v]{_grid(edit)}[base]'], 'base'
    def add(clip: Clip) -> int:
        index = sum(1 for arg in inputs if arg == '-i')
        # A solid has no size of its own: missing dimensions fill the frame.
        inputs.extend(_video_input(clip, edit.fps, (clip.w or edit.width, clip.h or edit.height)))
        lines.append(_overlay_chain(clip, edit, index))
        return index

    def canvas(color: str, fmt: str) -> str:
        """A full-frame stream lasting the whole timeline, to lay a layer or a matte on."""
        index = sum(1 for arg in inputs if arg == '-i')
        inputs.extend(['-f', 'lavfi', '-i', f'color=c={color}:s={edit.width}x{edit.height}:r={edit.fps}:'
                                             f'd={float(edit.total_frames / edit.fps):.6f},format={fmt}'])
        return f'{index}:v'

    for clip in edit.overlays:
        index = add(clip)
        x, y = (-MARGIN, -MARGIN) if _moves(clip) else (clip.x, clip.y)
        if not (clip.mask or clip.blend):
            lines.append(f'[{current}][o{index}]overlay=x={x}:y={y}:eof_action=pass:format=auto[ov{index}]')
            current = f'ov{index}'
            continue
        # Masks and blend modes work on the layer spread over the whole frame and timeline (alpha kept).
        lines.append(f'[{canvas("black@0", "yuva420p")}][o{index}]overlay=x={x}:y={y}:eof_action=pass:format=auto[layer{index}]')
        layer = f'layer{index}'
        if clip.mask:
            matte = add(clip.mask)
            pick = 'alphaextract' if clip.mask_mode == 'alpha' else 'format=gray'
            # Inverted before it is laid on white: where the matte has no frames the layer stays unmasked.
            lines.append(f'[o{matte}]{pick}{",negate" if clip.mask_invert else ""}[mg{index}]')
            lines.append(f'[{canvas("white", "gray")}][mg{index}]overlay=eof_action=pass:format=auto,format=gray[mk{index}]')
            lines.append(f'[{layer}]split[lc{index}][la{index}];[la{index}]alphaextract[a{index}];'
                         f'[a{index}][mk{index}]blend=all_mode=multiply[am{index}];[lc{index}][am{index}]alphamerge[lm{index}]')
            layer = f'lm{index}'
        if clip.blend and clip.blend != 'normal':
            # Blend modes are RGB arithmetic: convert explicitly (BT.709 limited) both ways, never by swscale defaults.
            rgb = 'scale=in_color_matrix=bt709:in_range=tv'
            lines.append(f'[{current}]{rgb},format=gbrp,split[b1_{index}][b2_{index}]')
            lines.append(f'[{layer}]{rgb},format=gbrap,split[l1_{index}][l2_{index}];[l1_{index}]format=gbrp[lr{index}];'
                         f'[l2_{index}]alphaextract[lx{index}]')
            lines.append(f'[b1_{index}][lr{index}]blend=all_mode={clip.blend}[bl{index}];[bl{index}][lx{index}]alphamerge[bx{index}]')
            lines.append(f'[b2_{index}][bx{index}]overlay=format=gbrp,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,'
                         f'{audio_mod.BT709}[ov{index}]')
        else:
            lines.append(f'[{current}][{layer}]overlay=format=auto[ov{index}]')
        current = f'ov{index}'
    tail = f'[{current}]'
    if edit.subtitles:
        shutil.copyfile(edit.subtitles, work / f'subs{edit.subtitles.suffix.lower()}')
        style = f":force_style='{edit.subtitle_style}'" if edit.subtitle_style else ''
        lines.append(f"{tail}subtitles=filename=subs{edit.subtitles.suffix.lower()}{style}[sub]")
        tail = '[sub]'
    lines.append(f'{tail}format=yuv420p,{audio_mod.BT709},trim=start_frame={span[0]}:end_frame={span[1]},setpts=N/({edit.fps}*TB)[out]')
    run(['ffmpeg', *inputs, '-filter_complex', ';'.join(lines), '-map', '[out]', *audio_mod.VIDEO_ENCODE, *audio_mod.CFR_VUI,
         '-crf', str(edit.crf), '-preset', edit.preset, '-r', str(edit.fps), '-an', '-y', str(out.resolve())],
        cwd=work)


def _render_audio(edit: Edit, slot_audio: list[Path], work: Path) -> Path | None:
    has_clip_audio = any(c.audio and c.has_audio for c in edit.base)
    if not has_clip_audio and not edit.audio:
        return None
    total_samples = round(edit.total_frames / edit.fps * SR)
    inputs, lines = [], []
    for path in slot_audio:
        inputs += ['-i', str(path)]
    current = '0:a'
    for i, clip in enumerate(edit.base[1:], start=1):
        label = f'b{i}'
        if clip.transition_frames:
            lines.append(f'[{current}][{i}:a]acrossfade=d={float(clip.transition_frames / edit.fps):.6f}:c1=tri:c2=tri[{label}]')
        else:
            lines.append(f'[{current}][{i}:a]concat=n=2:v=0:a=1[{label}]')
        current = label
    lines.append(f'[{current}]apad,atrim=end_sample={total_samples}[base]')
    roles: dict[str, list[str]] = {'music': [], 'voice': [], 'sfx': []}
    for item in edit.audio:
        index = sum(1 for arg in inputs if arg == '-i')
        inputs += ['-i', str(item.source)]
        chain = f'asetpts=PTS-STARTPTS,atrim=start={item.start_s:.6f}'
        if item.duration:
            chain += f':duration={item.duration:.6f}'
        chain += f',asetpts=PTS-STARTPTS,aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo'
        if item.gain_db:
            chain += f',volume={item.gain_db}dB'
        chain += _gain(item.keys, item.af)
        if item.fade_in:
            chain += f',afade=t=in:st=0:d={item.fade_in:.4f}'
        if item.fade_out:
            length = item.duration or (probe(item.source)['audio']['duration'] or 0) - item.start_s
            available = min(length, edit.duration - item.at)
            chain += f',afade=t=out:st={max(0.0, available - item.fade_out):.4f}:d={item.fade_out:.4f}'
        if item.at:
            chain += f',adelay=delays={round(item.at * 1000)}:all=1'
        label = f'x{index}'
        lines.append(f'[{index}:a]{chain}[{label}]')
        roles[item.role].append(label)

    def bus(labels: list[str], name: str) -> str | None:
        if not labels:
            return None
        if len(labels) == 1:
            return labels[0]
        lines.append(''.join(f'[{label}]' for label in labels) + f'amix=inputs={len(labels)}:normalize=0:duration=longest[{name}]')
        return name

    voice = bus(roles['voice'], 'voice')
    music = bus(roles['music'], 'music')
    sfx = bus(roles['sfx'], 'sfx')
    if edit.duck_db and voice and music:
        # ponytail: sidechain compression approximates duck_db; a gain envelope from voice activity is exact.
        ratio = min(20.0, 1 + edit.duck_db / 2)
        # The key is padded with silence: sidechaincompress ends when either input does, which would cut the
        # music off at the last word of the voice.
        lines.append(f'[{voice}]asplit=2[voice_mix][voice_raw];[voice_raw]apad[voice_key]')
        lines.append(f'[{music}][voice_key]sidechaincompress=threshold=0.02:ratio={ratio:.2f}:attack=30:release=400[ducked]')
        voice, music = 'voice_mix', 'ducked'
    stems = ['base'] + [s for s in (voice, music, sfx) if s]
    if len(stems) > 1:
        lines.append(''.join(f'[{s}]' for s in stems) + f'amix=inputs={len(stems)}:normalize=0:duration=first[mixed]')
        final = 'mixed'
    else:
        final = 'base'
    lines.append(f'[{final}]apad,atrim=end_sample={total_samples}[aout]')
    mix = work / 'mix.wav'
    run(['ffmpeg', *inputs, '-filter_complex', ';'.join(lines), '-map', '[aout]', '-c:a', 'pcm_f32le', '-ar', str(SR), '-y', str(mix)])
    return mix


PREVIEW_SCALE = 0.5


def _preview(edit: Edit, scale: float) -> None:
    """A draft: smaller frame, fast encode. Overlay pixel positions and sizes scale with the frame; main-track keys are
    fractions of the frame and libass scales subtitles by their PlayRes, so the draft is the same edit, smaller.
    ponytail: renders the whole timeline; a time range needs anchors, transitions and subtitles shifted, add if drafts get slow."""
    edit.width, edit.height = (max(2, round(v * scale / 2) * 2) for v in (edit.width, edit.height))
    edit.crf, edit.preset = 30, 'ultrafast'
    for clip in edit.overlays:
        if clip.kind != 'color' and not (clip.w or clip.h):
            clip.w = (clip.src_size or (probe(clip.source)['video']['width'],))[0]
        clip.x, clip.y = round(clip.x * scale), round(clip.y * scale)
        clip.w, clip.h = (max(1, round(v * scale)) if v else v for v in (clip.w, clip.h))
        for prop in ('x', 'y'):
            if clip.keys and prop in clip.keys:
                clip.keys[prop] = [(t, v * scale, ease) for t, v, ease in clip.keys[prop]]
        if clip.mask:
            clip.mask.w, clip.mask.h = edit.width, edit.height


def _files(source: Path | None, sequence: bool = False) -> list:
    """What a render depends on besides its parameters: the source files' size and mtime (a sequence: its folder's)."""
    if source is None:
        return []
    files = sorted(f for f in source.parent.iterdir() if f.is_file()) if sequence else [source]
    return [(f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files]


def _slot_key(clip: Clip, edit: Edit, part: str) -> str:
    # Where a clip sits and how it is named do not change its slot; moving or renaming a clip reuses the render.
    params = {k: v for k, v in dataclasses.asdict(clip).items()
              if k not in ('where', 'timeline_start', 'id', 'meta', 'transition', 'transition_frames')}
    material = [__version__, part, edit.width, edit.height, str(edit.fps), edit.background, params,
                _files(clip.source, clip.kind == 'sequence')]
    return hashlib.sha256(json.dumps(material, default=str, sort_keys=True).encode()).hexdigest()[:24]


def _cached(cache: Path | None, key: str, suffix: str, work: Path, render) -> Path:
    """render(path) once per key. ponytail: the cache only grows; delete .rn-cache (or RN_CACHE) to reclaim space."""
    if cache is None:
        target = work / f'{key}{suffix}'
        render(target)
        return target
    target = cache / f'{key}{suffix}'
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = cache / f'{key}.{os.getpid()}.part{suffix}'
        render(partial)
        partial.replace(target)
    return target


def _fingerprint(path: Path, placeholders: bool) -> str:
    """A nested edit's render depends on its text and, recursively, on every file it reads."""
    edit = load(path, placeholders=placeholders)
    parts = [__version__, path.read_text(), placeholders]
    for clip in edit.base + edit.overlays:
        parts.append(_fingerprint(clip.source, placeholders) if clip.kind == 'edit' else _files(clip.source, clip.kind == 'sequence'))
    parts += [_files(item.source) for item in edit.audio] + [_files(edit.subtitles)]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:24]


def _render_nested(edit: Edit, cache: Path | None, work: Path, placeholders: bool) -> None:
    """Nested edits render first (to the cache when on) and then play as ordinary video sources."""
    for clip in edit.base + edit.overlays:
        if clip.kind == 'edit':
            key = 'edit-' + _fingerprint(clip.source, placeholders)
            clip.source = _cached(cache, key, '.mp4', work, lambda target, source=clip.source: assemble_command(
                str(source), str(target), dry_run=False, keep=False, cache=cache is not None, placeholders=placeholders))
            clip.kind = 'video'


def _span(edit: Edit, time_range: str | None) -> tuple[int, int]:
    """--range A-B (seconds) -> output frames [a, b). Clips wholly outside it render as cheap background slots and
    overlays there are dropped; everything is still laid out on the full timeline, so anchors and transitions hold."""
    if not time_range:
        return 0, edit.total_frames
    try:
        a, b = (float(part) for part in time_range.split('-'))
    except ValueError:
        raise RnError(f'--range "{time_range}": seconds as A-B, e.g. 3.5-6') from None
    span = (max(0, round(a * edit.fps)), min(edit.total_frames, round(b * edit.fps)))
    if span[0] >= span[1]:
        raise RnError(f'--range {time_range}: empty inside the timeline (0-{edit.duration:.3f}s)')
    for i, clip in enumerate(edit.base):
        if clip.timeline_start + clip.frames <= span[0] or clip.timeline_start >= span[1]:
            edit.base[i] = dataclasses.replace(clip, kind='color', color=edit.background, source=None, keys=None, audio=False,
                                               has_audio=False, src_fps=None, matrix='bt709', decoder=None)
    edit.overlays = [c for c in edit.overlays if c.timeline_start < span[1] and c.timeline_start + c.frames > span[0]]
    return span


def assemble_command(edl_path: str, out: str, *, dry_run: bool, keep: bool, preview: bool = False, cache: bool = True,
                     placeholders: bool = False, time_range: str | None = None) -> dict:
    edit = load(edl_path, placeholders=placeholders)
    if edit.subtitles and not has_filter('subtitles'):
        raise RnError('this ffmpeg has no libass, so it cannot burn subtitles; on macOS: brew install ffmpeg-full, then put '
                      '"$(brew --prefix ffmpeg-full)/bin" first on PATH')
    if preview:
        _preview(edit, PREVIEW_SCALE)
    if dry_run:
        return edit.summary()
    out_path = Path(out)
    store = cache_dir(edit.path.parent) / 'slots' if cache else None
    span = _span(edit, time_range)
    with tempfile.TemporaryDirectory(prefix='rn-assemble-') as tmp:
        work = Path(tmp)
        _render_nested(edit, store, work, placeholders)
        slots, slot_audio = [], []
        for i, clip in enumerate(edit.base):
            slots.append(_cached(store, _slot_key(clip, edit, 'video'), '.mkv', work, lambda target, c=clip: _render_slot(c, edit, target)))
            # A hard cut's own sound fades on both sides; a transition already crossfades it.
            fades = (edit.cut_fade if i and not clip.transition_frames else 0.0,
                     edit.cut_fade if i + 1 < len(edit.base) and not edit.base[i + 1].transition_frames else 0.0)
            slot_audio.append(_cached(store, _slot_key(clip, edit, f'audio{fades}'), '.wav', work,
                                      lambda target, c=clip, f=fades: _render_slot_audio(c, edit, target, f)))
        video = work / 'video.mp4'
        _render_video(edit, slots, work, video, span)
        frames = probe(video, exact=True)['video']['frames']
        if frames != span[1] - span[0]:
            raise RnError(f'rendered {frames} frames, expected {span[1] - span[0]}')
        mix = _render_audio(edit, slot_audio, work)
        loudness = None
        m4a = None
        if mix:
            m4a = work / 'audio.m4a'
            excerpt = mix
            if span != (0, edit.total_frames):
                # A range keeps the full mix's level: the gain is measured on the whole mix, applied to the excerpt.
                excerpt = work / 'excerpt.wav'
                run(['ffmpeg', '-i', str(mix), '-af', f'atrim=start_sample={round(span[0] / edit.fps * SR)}:'
                     f'end_sample={round(span[1] / edit.fps * SR)},asetpts=PTS-STARTPTS', '-c:a', 'pcm_f32le', '-y', str(excerpt)])
            loudness = audio_mod.finish_audio(excerpt, m4a, lufs=edit.lufs, true_peak=edit.true_peak, reference=mix)
        audio_mod.mux(video, m4a, out_path, frames=span[1] - span[0])
        if keep:
            kept = out_path.with_suffix('.parts')
            shutil.copytree(work, kept, dirs_exist_ok=True)
    summary = edit.summary()
    summary['path'] = str(out_path)
    summary['audio'] = loudness
    summary['preview'] = preview
    if span != (0, edit.total_frames):
        summary['range'] = {'start_frame': span[0], 'end_frame': span[1]}
        summary['output'].update(frames=span[1] - span[0], duration=round(float((span[1] - span[0]) / edit.fps), 4))
    return summary


def finish_command(source: str, out: str, *, lufs: float | None, true_peak: float, crf: int, preset: str, copy_video: bool,
                   height: int | None = None) -> dict:
    """Any video -> the delivery standard: H.264 High yuv420p bt709, moov first, LUFS target, encoded TP <= limit."""
    info = probe(source, exact=True)
    video = info['video']
    if not video or video['still']:
        raise RnError(f'{source}: not a video')
    frames = video['frames']
    out_path = Path(out)
    with tempfile.TemporaryDirectory(prefix='rn-finish-') as tmp:
        work = Path(tmp)
        encoded = work / 'video.mp4'
        compliant = video['codec'] == 'h264' and video['pix_fmt'] == 'yuv420p' and video['sar'] == '1:1'
        if height and copy_video:
            raise RnError('--height re-encodes the picture; drop --copy-video')
        if copy_video and compliant:
            run(['ffmpeg', '-i', source, '-map', '0:v:0', '-c:v', 'copy', '-an', '-y', str(encoded)])
        else:
            size = f'-2:{height // 2 * 2}' if height else 'trunc(iw/2)*2:trunc(ih/2)*2'  # --height keeps the aspect, even width
            even = f'scale={size}:flags=lanczos:{to_bt709(source_matrix(video))},setsar=1,format=yuv420p,{audio_mod.BT709}'
            run(['ffmpeg', *(['-c:v', video['alpha_decoder']] if video.get('alpha_decoder') else []), '-i', source, '-map', '0:v:0', '-vf', even, *audio_mod.VIDEO_ENCODE,
                 *(audio_mod.CFR_VUI if video['cfr'] else []), '-crf', str(crf), '-preset', preset,
                 '-fps_mode', 'passthrough', '-an', '-y', str(encoded)])
        loudness = None
        m4a = None
        if info['audio']:
            wav = work / 'mix.wav'
            seconds = float(frames / Fraction(video['fps']))
            run(['ffmpeg', '-i', source, '-map', '0:a:0', '-af', f'aresample={SR},apad,atrim=end={seconds:.6f}', '-ac', '2',
                 '-c:a', 'pcm_f32le', '-y', str(wav)])
            m4a = work / 'audio.m4a'
            loudness = audio_mod.finish_audio(wav, m4a, lufs=lufs, true_peak=true_peak)
        audio_mod.mux(encoded, m4a, out_path, frames=frames)
    return {'path': str(out_path), 'frames': frames, 'video_copied': bool(copy_video and compliant), 'audio': loudness}
