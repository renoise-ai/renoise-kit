"""rn assemble: render an EDL to a finished MP4, frame-exact. rn finish: the same output standard for any video.

Pipeline (spec/mcp/03-media-kit.md §4-5):
1. every main-track clip -> its own intermediate at the output size/fps with exactly `frames` frames, plus a WAV
   of exactly the same length (a slot engine: each piece is checkable on its own);
2. one ffmpeg graph joins the slots (concat or xfade), stacks overlay tracks, burns subtitles, encodes video only;
3. audio is mixed separately (clip audio with matching crossfades + audio items + optional ducking), gained to the
   LUFS target and limited until the ENCODED true peak fits; then both streams are copied into the MP4.
"""
from __future__ import annotations

import math
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from . import audio as audio_mod
from .edl import Clip, Edit, load
from .ff import RnError, ff_color, probe, run, source_matrix, to_bt709

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


def _timing(clip: Clip, fps: Fraction) -> str:
    """Source range -> output frame grid. Starts half a source frame early so float rounding never drops the first frame."""
    start = max(0.0, clip.start_s - 0.5 / float(clip.src_fps)) if clip.src_fps else clip.start_s
    chain = f'setpts=PTS-STARTPTS,trim=start={start:.6f},setpts=PTS-STARTPTS'
    if clip.speed != 1:
        chain += f',setpts=PTS/{clip.speed:.6f}'
    # Slow motion blends neighbouring frames instead of repeating them (a visible stutter at 0.3-1x).
    chain += f',framerate=fps={fps}' if clip.speed < 1 else f',fps={fps}'
    return chain


def _video_input(clip: Clip, fps: Fraction, size: tuple[int, int]) -> list[str]:
    if clip.kind == 'color':
        # Generated as RGB so the BT.709 conversion downstream gets the colour right.
        return ['-f', 'lavfi', '-i', f'color=c={ff_color(clip.color or "black")}:s={size[0]}x{size[1]}:r={fps},format=rgb24']
    if clip.kind == 'image':
        return ['-loop', '1', '-framerate', str(fps), '-i', str(clip.source)]
    return [*(['-c:v', clip.decoder] if clip.decoder else []), '-i', str(clip.source)]


def _render_slot(clip: Clip, edit: Edit, out: Path) -> None:
    chain = _timing(clip, edit.fps) + ',' if clip.kind == 'video' else ''
    chain += _fit(clip, edit.width, edit.height, edit.background) + f',setsar=1,format=yuv420p,{audio_mod.BT709}'
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


def _render_slot_audio(clip: Clip, edit: Edit, out: Path) -> None:
    samples = round(clip.frames / edit.fps * SR)
    if clip.audio and clip.has_audio:
        chain = f'asetpts=PTS-STARTPTS,atrim=start={clip.start_s:.6f},asetpts=PTS-STARTPTS'
        tempo = _atempo(clip.speed) if clip.speed != 1 else ''
        chain += (',' + tempo if tempo else '') + f',aresample={SR},aformat=sample_fmts=flt:channel_layouts=stereo'
        if clip.gain_db:
            chain += f',volume={clip.gain_db}dB'
        chain += f',apad,atrim=end_sample={samples}'
        run(['ffmpeg', '-i', str(clip.source), '-filter_complex', f'[0:a]{chain}[a]', '-map', '[a]', '-c:a', 'pcm_f32le', '-y', str(out)])
    else:
        run(['ffmpeg', '-f', 'lavfi', '-i', f'anullsrc=r={SR}:cl=stereo', '-af', f'atrim=end_sample={samples}',
             '-c:a', 'pcm_f32le', '-y', str(out)])


def _join(edit: Edit, slots: list[Path]) -> tuple[list[str], list[str], str]:
    """Inputs and graph lines that join the main-track slots into [base]."""
    inputs, lines = [], []
    # xfade and concat need one shared timebase: 1/fps everywhere, pts = frame number.
    grid = f'settb={edit.fps.denominator}/{edit.fps.numerator},setpts=N,fps={edit.fps}'
    for i, slot in enumerate(slots):
        inputs += ['-i', str(slot)]
        # Slots already hold exactly `frames` frames; renumber them instead of resampling (fps would drop one on ms timestamps).
        lines.append(f'[{i}:v]{grid}[s{i}]')
    current = 's0'
    for i, clip in enumerate(edit.base[1:], start=1):
        label = f'j{i}'
        if clip.transition_frames:
            offset = clip.timeline_start / edit.fps
            duration = clip.transition_frames / edit.fps
            lines.append(f'[{current}][s{i}]xfade=transition={clip.transition}:duration={float(duration):.6f}:offset={float(offset):.6f},{grid}[{label}]')
        else:
            lines.append(f'[{current}][s{i}]concat=n=2:v=1:a=0,{grid}[{label}]')
        current = label
    return inputs, lines, current


def _overlay_chain(clip: Clip, edit: Edit, index: int) -> str:
    parts = []
    if clip.kind == 'video':
        parts.append(_timing(clip, edit.fps))
    size = f'{clip.w or -2}:{clip.h or -2}' if (clip.w or clip.h) and clip.kind != 'color' else 'iw:ih'
    parts.append(f'scale={size}:flags=lanczos:{to_bt709(clip.matrix)}')
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
    parts.append(f'setpts=PTS+{clip.timeline_start}')
    return f'[{index}:v]' + ','.join(parts) + f'[o{index}]'


def _render_video(edit: Edit, slots: list[Path], work: Path, out: Path) -> None:
    inputs, lines, current = _join(edit, slots)
    for clip in edit.overlays:
        index = sum(1 for arg in inputs if arg == '-i')
        # A solid has no size of its own: missing dimensions fill the frame.
        size = (clip.w or edit.width, clip.h or edit.height)
        inputs += _video_input(clip, edit.fps, size)
        lines.append(_overlay_chain(clip, edit, index))
        lines.append(f'[{current}][o{index}]overlay=x={clip.x}:y={clip.y}:eof_action=pass:format=auto[ov{index}]')
        current = f'ov{index}'
    tail = f'[{current}]'
    if edit.subtitles:
        shutil.copyfile(edit.subtitles, work / f'subs{edit.subtitles.suffix.lower()}')
        style = f":force_style='{edit.subtitle_style}'" if edit.subtitle_style else ''
        lines.append(f"{tail}subtitles=filename=subs{edit.subtitles.suffix.lower()}{style}[sub]")
        tail = '[sub]'
    lines.append(f'{tail}format=yuv420p,{audio_mod.BT709},trim=end_frame={edit.total_frames},setpts=N/({edit.fps}*TB)[out]')
    run(['ffmpeg', *inputs, '-filter_complex', ';'.join(lines), '-map', '[out]', *audio_mod.VIDEO_ENCODE,
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


def assemble_command(edl_path: str, out: str, *, dry_run: bool, keep: bool) -> dict:
    edit = load(edl_path)
    if dry_run:
        return edit.summary()
    out_path = Path(out)
    with tempfile.TemporaryDirectory(prefix='rn-assemble-') as tmp:
        work = Path(tmp)
        slots, slot_audio = [], []
        for i, clip in enumerate(edit.base):
            slot = work / f'slot{i:03d}.mkv'
            _render_slot(clip, edit, slot)
            slots.append(slot)
            wav = work / f'slot{i:03d}.wav'
            _render_slot_audio(clip, edit, wav)
            slot_audio.append(wav)
        video = work / 'video.mp4'
        _render_video(edit, slots, work, video)
        frames = probe(video, exact=True)['video']['frames']
        if frames != edit.total_frames:
            raise RnError(f'rendered {frames} frames, expected {edit.total_frames}')
        mix = _render_audio(edit, slot_audio, work)
        loudness = None
        m4a = None
        if mix:
            m4a = work / 'audio.m4a'
            loudness = audio_mod.finish_audio(mix, m4a, lufs=edit.lufs, true_peak=edit.true_peak)
        audio_mod.mux(video, m4a, out_path, frames=edit.total_frames)
        if keep:
            kept = out_path.with_suffix('.parts')
            shutil.copytree(work, kept, dirs_exist_ok=True)
    summary = edit.summary()
    summary['path'] = str(out_path)
    summary['audio'] = loudness
    return summary


def finish_command(source: str, out: str, *, lufs: float | None, true_peak: float, crf: int, preset: str, copy_video: bool) -> dict:
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
        if copy_video and compliant:
            run(['ffmpeg', '-i', source, '-map', '0:v:0', '-c:v', 'copy', '-an', '-y', str(encoded)])
        else:
            even = f'scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos:{to_bt709(source_matrix(video))},setsar=1,format=yuv420p,{audio_mod.BT709}'
            run(['ffmpeg', *(['-c:v', video['alpha_decoder']] if video.get('alpha_decoder') else []), '-i', source, '-map', '0:v:0', '-vf', even, *audio_mod.VIDEO_ENCODE, '-crf', str(crf), '-preset', preset,
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
