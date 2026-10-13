"""rn: Renoise media kit. Frame-exact probing, frames and contact sheets, EDL assembly, finishing and QA.

Every command prints a short summary; --json FILE writes the full result. Failures exit non-zero with
`rn: error: <reason>` on the last line. Contract: spec/mcp/03-media-kit.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .ff import RnError

EPILOG = 'Times are seconds. Run `rn <command> --help` for details.'


def _numbers(kind):
    def parse(text: str):
        try:
            return [kind(part) for part in text.split(',') if part.strip()]
        except ValueError:
            raise argparse.ArgumentTypeError(f'expected comma-separated {kind.__name__}s') from None
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='rn', description=__doc__, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=f'rn {__version__}')
    sub = parser.add_subparsers(dest='command', required=True, metavar='command')

    def add(name: str, help_text: str, description: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
        command.add_argument('--json', metavar='FILE', help='write the full result as JSON')
        return command

    probe = add('probe', 'facts an edit depends on (prints JSON)',
                'Decoded frame count, rational fps, size/SAR/pix_fmt/color tags, video and audio durations, faststart.\n'
                'The frame count comes from packets (or a full decode with --exact), never from the container duration.')
    probe.add_argument('file')
    probe.add_argument('--exact', action='store_true', help='decode every frame to count (slower, exact for any stream)')

    frame = add('frame', 'one exact frame by index, time or last',
                'Extract one frame by its decoded index (0-based). Use --last for a tail frame to continue a shot,\n'
                '--time T for the frame on screen at T seconds. Decodes from the start: exact, never off by one.')
    frame.add_argument('file')
    pick = frame.add_mutually_exclusive_group(required=True)
    pick.add_argument('--index', type=int)
    pick.add_argument('--time', type=float)
    pick.add_argument('--last', action='store_true')
    frame.add_argument('-o', '--out', required=True, help='.png (lossless) or .jpg')

    sheet = add('sheet', 'labelled contact sheets small enough to view',
                'Tiles labelled with frame index and timecode; pages of up to 24 tiles, each JPEG under --max-kb\n'
                '(default 125 KB, so four pages fit one sandbox_exec image_paths view). Default: 16 evenly spaced frames.\n'
                'Examples:\n  rn sheet take.mp4 --fps 4            # pick in-points from a generated take\n'
                '  rn sheet ref.mp4 --every 1 -o ref.jpg  # every frame of a reference, paged\n'
                '  rn sheet out.mp4 --indices 0,47,48,95  # both sides of each cut')
    sheet.add_argument('files', nargs='+')
    choose = sheet.add_mutually_exclusive_group()
    choose.add_argument('--every', type=int, metavar='N', help='every Nth frame')
    choose.add_argument('--fps', type=float, metavar='R', help='R frames per second of video')
    choose.add_argument('--indices', type=_numbers(int), metavar='A,B,..', help='exact frame indices')
    choose.add_argument('--seconds', type=_numbers(float), metavar='T1,T2,..', help='frames on screen at these times')
    choose.add_argument('--count', type=int, metavar='N', help='N evenly spaced frames (default 16)')
    sheet.add_argument('-o', '--out', help='output .jpg (pages get -p2, -p3...; several inputs get -<name>); default sheets/<name>.jpg')
    sheet.add_argument('--width', type=int, default=256, help='tile width in px (default 256)')
    sheet.add_argument('--columns', type=int, default=6)
    sheet.add_argument('--max-kb', type=int, default=125)

    assemble = add('assemble', 'render an EDL (edit.json) to a finished MP4',
                   'Frame-exact assembly: cuts, speed, fit, transitions, overlay tracks, subtitles, clip audio + music/voice/sfx,\n'
                   'ducking, LUFS target and encoded true peak <= limit. Output: H.264 High yuv420p bt709, moov first.\n'
                   'The EDL handles the mechanical assembly; render anything creative with your own code (HTML/canvas,\n'
                   'Manim, Blender, ffmpeg) to a clip or an alpha overlay and place it in the EDL.\n\n'
                   'edit.json (paths relative to it; times in seconds; {"frame": n} also works for in/out;\n'
                   '# marks notes below, not JSON; a "note" field is allowed on any clip):\n'
                   '{\n'
                   '  "output": {"width": 1080, "height": 1920, "fps": 30, "background": "#000000", "crf": 18},\n'
                   '  "tracks": [\n'
                   '    {"clips": [                                  # track 0 = main track, clips back to back\n'
                   '      {"src": "s1.mp4", "in": 0.4, "out": 3.2, "fit": "cover"},\n'
                   '      {"src": "s2.mp4", "in": {"frame": 6}, "duration": 2.5, "speed": 0.5, "audio": false,\n'
                   '       "transition": {"type": "fade", "duration": 0.4}},   # any xfade name: dissolve, wipeleft, slideup...\n'
                   '      {"image": "card.png", "duration": 1.5, "fit": "contain"},\n'
                   '      {"color": "#000000", "duration": 0.5}]},\n'
                   '    {"clips": [                                  # later tracks = overlays, positioned in time and space\n'
                   '      {"image": "title.png", "at": 0.5, "duration": 2, "x": 90, "y": 1400, "w": 900,\n'
                   '       "opacity": 1, "fade_in": 0.2, "fade_out": 0.2}]}],\n'
                   '  "audio": [{"src": "music.mp3", "role": "music", "gain_db": -6, "fade_out": 1.5},\n'
                   '            {"src": "vo.wav", "role": "voice", "at": 0.3}],          # roles: music | voice | sfx\n'
                   '  "subtitles": {"file": "captions.ass"},\n'
                   '  "mix": {"lufs": -14, "true_peak": -1.0, "duck_db": 10}            # duck music under voice\n'
                   '}')
    assemble.add_argument('edl')
    assemble.add_argument('-o', '--out', default='out.mp4')
    assemble.add_argument('--dry-run', action='store_true', help='validate and print the timeline without rendering')
    assemble.add_argument('--keep', action='store_true', help='keep per-clip slots and the mix next to the output (<out>.parts/)')

    finish = add('finish', 'bring any video to the delivery standard',
                 'For a video you built yourself: H.264 High yuv420p bt709, even size, square pixels, moov first,\n'
                 'loudness to --lufs and the ENCODED true peak <= --true-peak (AAC adds 1-2 dB of overshoot).')
    finish.add_argument('file')
    finish.add_argument('-o', '--out', required=True)
    finish.add_argument('--lufs', default='-14', help='integrated loudness target, or "keep" to leave the level as is (default -14)')
    finish.add_argument('--true-peak', type=float, default=-1.0)
    finish.add_argument('--crf', type=int, default=18)
    finish.add_argument('--preset', default='medium')
    finish.add_argument('--copy-video', action='store_true', help='keep the video stream when it is already H.264 yuv420p')

    qa = add('qa', 'delivery checks on an encoded file (exit 1 on FAIL)',
             'Frame count, fps, size, codec, pix_fmt, SAR, color tags, faststart, audio/video duration, container\n'
             'duration, true peak, loudness, black frames, frozen frames, black bars. With --edl the expected frames,\n'
             'size, fps and loudness come from the edit, and its color/still clips are not flagged. FAIL blocks\n'
             'delivery; WARN means look at it (it may be intended). Check fixes on the new render, not the old one.')
    qa.add_argument('file')
    qa.add_argument('--edl')
    qa.add_argument('--expect-frames', type=int)
    qa.add_argument('--expect-size', metavar='WxH')
    qa.add_argument('--expect-fps', metavar='R')
    qa.add_argument('--lufs', type=float, help='loudness target to check against')
    qa.add_argument('--lufs-tolerance', type=float, default=2.0)
    qa.add_argument('--true-peak', type=float, help='limit in dBTP (default -1.0)')
    qa.add_argument('--require-audio', action='store_true')
    return parser


def _print_summary(command: str, result: dict) -> None:
    if command == 'probe':
        print(json.dumps(result, indent=1, ensure_ascii=False))
    elif command == 'frame':
        print(f'frame {result["index"]} ({result["time"]}s) -> {result["path"]}')
    elif command == 'sheet':
        for page in result['pages']:
            indices = page['indices']
            print(f'{page["path"]}  {len(indices)} frames #{indices[0]}..#{indices[-1]}  {page["bytes"] // 1024} KB')
    elif command == 'assemble':
        out = result['output']
        if 'path' in result:
            print(f'{result["path"]}  {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s')
            if result.get('audio'):
                audio = result['audio']
                print(f'audio {audio["lufs"]} LUFS, true peak {audio["true_peak"]} dBTP, gain {audio["gain_db"]} dB')
        else:
            print(f'timeline {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s')
            for clip in result['base']:
                fade = f'  (xfade {clip["transition_frames"]}f)' if clip['transition_frames'] else ''
                print(f'  {clip["where"]}: frames {clip["start_frame"]}..{clip["start_frame"] + clip["frames"] - 1}{fade}')
            for clip in result['overlays']:
                print(f'  {clip["where"]}: overlay frames {clip["start_frame"]}..{clip["start_frame"] + clip["frames"] - 1}')
        for warning in result.get('warnings', []):
            print(f'warning: {warning}')
    elif command == 'finish':
        audio = result.get('audio')
        line = f'{result["path"]}  {result["frames"]} frames' + ('  (video copied)' if result['video_copied'] else '')
        if audio:
            line += f'  audio {audio["lufs"]} LUFS, true peak {audio["true_peak"]} dBTP'
        print(line)
    elif command == 'qa':
        for item in result['checks']:
            if item['status'] != 'PASS':
                print(f'{item["status"]:4}  {item["check"]}: {item["detail"]}')
        print(f'qa {result["status"]}: {result["failed"]} failed, {result["warnings"]} warnings, '
              f'{len(result["checks"]) - result["failed"] - result["warnings"]} passed  ({result["path"]})')


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            print(f'rn: error: {tool} not found on PATH', file=sys.stderr)
            return 2
    try:
        if args.command == 'probe':
            from .ff import probe
            result = probe(args.file, exact=args.exact)
        elif args.command == 'frame':
            from .frames import frame_command
            result = frame_command(args.file, index=args.index, time=args.time, last=args.last, out=args.out)
        elif args.command == 'sheet':
            from .frames import sheet_command
            result = sheet_command(args.files, out=args.out, every=args.every, rate=args.fps, indices=args.indices,
                                   seconds=args.seconds, count=args.count, width=args.width, columns=args.columns, max_kb=args.max_kb)
        elif args.command == 'assemble':
            from .assemble import assemble_command
            result = assemble_command(args.edl, args.out, dry_run=args.dry_run, keep=args.keep)
        elif args.command == 'finish':
            from .assemble import finish_command
            lufs = None if args.lufs == 'keep' else float(args.lufs)
            result = finish_command(args.file, args.out, lufs=lufs, true_peak=args.true_peak, crf=args.crf,
                                    preset=args.preset, copy_video=args.copy_video)
        else:
            from .qa import qa_command
            result = qa_command(args.file, edl=args.edl, frames=args.expect_frames, size=args.expect_size, fps=args.expect_fps,
                                lufs=args.lufs, lufs_tolerance=args.lufs_tolerance, true_peak=args.true_peak,
                                require_audio=args.require_audio)
    except RnError as error:
        print(f'rn: error: {error}', file=sys.stderr)
        return 1
    except ValueError as error:
        print(f'rn: error: {error}', file=sys.stderr)
        return 1
    except Exception as error:  # a kit bug: keep the traceback for the report, end on the usual one-liner
        import traceback
        traceback.print_exc()
        print(f'rn: error: unexpected {type(error).__name__}: {error} (a bug in rn; work around it with ffmpeg directly)', file=sys.stderr)
        return 1
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    _print_summary(args.command, result)
    return 1 if args.command == 'qa' and result['status'] == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
