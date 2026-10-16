"""rn: Renoise media kit. Measure (probe, frame, sheet, cuts, listen, faces, ref), render (capture, fx, fonts,
assets, captions), assemble an EDL frame-exactly, compare, finish and QA. It measures and guarantees the mechanics; every
creative decision stays yours.

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
    sheet.add_argument('--around', type=_numbers(float), metavar='T1,T2,..', help='dense frames around these times (cuts, transitions, beats)')
    sheet.add_argument('--span', type=int, default=3, help='frames on each side for --around (default 3)')
    sheet.add_argument('--diff', action='store_true', help='show |frame - previous frame| x4: motion, flicker, repeated frames')

    cuts = add('cuts', 'shot boundaries by decoded frame (+ suspects to check)',
               'Scene-change scores per decoded frame; a cut where the score passes --threshold. Continuous camera moves\n'
               'and morphs score low, so weaker changes are listed as suspects: look at --sheet before trusting either.\n'
               'The shots it finds are what EDL positions {"shot": n} mean.')
    cuts.add_argument('file')
    cuts.add_argument('--threshold', type=float, default=0.3)
    cuts.add_argument('--min-gap', type=float, default=0.3, help='seconds; closer cuts keep the stronger one')
    cuts.add_argument('--sheet', metavar='JPG', help='contact sheet of both sides of every cut and suspect')
    cuts.add_argument('--max-kb', type=int, default=125)

    listen = add('listen', 'audio as facts + one picture (you cannot hear)',
                 'Loudness (integrated, true peak, short-term curve), silences, onsets, tempo with half/double\n'
                 'alternatives and beat times, and with --transcribe word timestamps (faster-whisper). One image on a\n'
                 'shared time axis: waveform, spectrogram, loudness, onset and beat ticks. Facts only, no cut suggestions.\n'
                 'Its --json feeds EDL anchors {"beat": n, "listen": ...} / {"word": n, ...} and rn captions.')
    listen.add_argument('file')
    listen.add_argument('-o', '--out', help='image path (default listen/<name>.jpg)')
    listen.add_argument('--transcribe', action='store_true')
    listen.add_argument('--language', help='e.g. zh, ja, en (default: detect)')
    listen.add_argument('--model', default='small', help='faster-whisper model (small is baked into the sandbox)')
    listen.add_argument('--max-kb', type=int, default=125)

    faces = add('faces', 'face boxes and landmarks per frame (measurement only)',
                'YuNet face boxes, eyes, nose and mouth corners in source pixels per sampled frame, plus the union\n'
                'box. For keeping captions and graphics off faces or following a subject with a separate layer; it never\n'
                'alters a face.')
    faces.add_argument('file')
    faces.add_argument('--every', type=int, default=1)
    faces.add_argument('--min-score', type=float, default=0.6)
    faces.add_argument('--sheet', metavar='JPG', help='contact sheet with the boxes drawn')
    faces.add_argument('--model')
    faces.add_argument('--max-kb', type=int, default=125)

    ref = add('ref', 'break down a reference edit: shots, audio, every frame',
              'Writes ref/<name>/: ref.json (probe, shots, cuts, suspects, listen), cuts.jpg, listen.jpg and\n'
              'frames*.jpg covering every --every-th frame in viewable pages. Interpretation is yours.')
    ref.add_argument('file')
    ref.add_argument('-o', '--out', help='folder (default ref/<name>)')
    ref.add_argument('--every', type=int, default=1)
    ref.add_argument('--transcribe', action='store_true')
    ref.add_argument('--language')
    ref.add_argument('--model', default='small')
    ref.add_argument('--threshold', type=float, default=0.3)

    capture = add('capture', 'render an HTML page frame by frame to RGBA PNGs',
                  'The page defines window.renderFrame(i) (may be async) and draws frame i completely: the frame index is\n'
                  'the time, so frames are exact and any order or shard gives the same pixels. window.RN = {fps, frames,\n'
                  'width, height, shard} and window.RNFX (rn fx) exist before the page runs; window.rnReady (a Promise) is\n'
                  'awaited once. Transparent where the page does not paint. Put the result in an EDL as\n'
                  '{"frames": "<dir>/%05d.png", "fps": R}. Example:\n'
                  '  <canvas id=c></canvas><script>const x = c.getContext("2d"); c.width = RN.width; c.height = RN.height;\n'
                  '  window.renderFrame = i => { x.clearRect(0, 0, c.width, c.height); /* draw frame i */ RNFX.grain(x, i, {}); };</script>')
    capture.add_argument('page')
    capture.add_argument('-o', '--out', required=True, help='output folder (its PNGs are replaced)')
    capture.add_argument('--frames', type=int, required=True)
    capture.add_argument('--size', default='1080x1920', metavar='WxH')
    capture.add_argument('--fps', type=float, default=30)
    capture.add_argument('--shards', type=int, default=4, help='parallel Chromium instances (each boots in 1-3 s)')
    capture.add_argument('--start', type=int, default=0, help='first frame index (render a part)')

    fx = add('fx', 'list the effects library (grain, leak, flash, glitch, ...)',
             'Readable, editable source: RNFX.<name> in pages for rn capture (fx.js), rn.fx.<name> for numpy frames in\n'
             'Python. Call it, copy and change it, or ignore it; defaults are engine defaults, not a style.')
    fx.add_argument('--source', action='store_true', help='print where fx.js and rn/fx/__init__.py live')

    fonts = add('fonts', 'installed font families, real weights and scripts',
                'From fontconfig: CSS weights that actually exist (a missing weight falls back silently), variable\n'
                'fonts, covered scripts (latin, zh-hans, zh-hant, ja, ko, emoji, ...). --licenses reads each font\'s licence.')
    fonts.add_argument('--family')
    fonts.add_argument('--script')
    fonts.add_argument('--licenses', action='store_true')

    assets = add('assets', 'licensed sound effects and overlay footage from the Renoise library',
                 'rn assets search "whoosh fast" --kind sfx ; rn assets get <id>... -o assets/\n'
                 'Files are checked against their sha256 and cached; the licence comes with each file.')
    assets.add_argument('action', choices=['search', 'get'])
    assets.add_argument('terms', nargs='*', help='search words, or asset ids for get')
    assets.add_argument('--kind', choices=['sfx', 'overlay', 'texture', 'music'])
    assets.add_argument('--limit', type=int, default=20)
    assets.add_argument('-o', '--out', default='assets')

    captions = add('captions', 'timed, chunked, face-aware captions from rn listen words',
                   'Mechanical only: align your script to the transcript timing, chunk lines (CJK by character with\n'
                   'kinsoku, Latin by word), keep to platform safe areas and off faces. Writes .ass (one plain readable\n'
                   'style), .srt and .json with per-word times to drive your own styled captions.')
    captions.add_argument('words', help='rn listen --transcribe --json output')
    captions.add_argument('-o', '--out', required=True, help='.ass path (.srt and .json alongside)')
    captions.add_argument('--size', required=True, metavar='WxH', help='output video size')
    captions.add_argument('--script', help='text file with the intended wording')
    captions.add_argument('--faces', help='rn faces --json output of the footage')
    captions.add_argument('--max-chars', type=int)
    captions.add_argument('--max-lines', type=int, default=2)
    captions.add_argument('--safe', default='auto', choices=['auto', 'tiktok', 'reels', 'youtube', 'none'])
    captions.add_argument('--language')

    assemble = add('assemble', 'render an EDL (edit.json) to a finished MP4',
                   'Frame-exact assembly: cuts, speed, fit, transitions, overlay tracks, subtitles, clip audio + music/voice/sfx,\n'
                   'ducking, LUFS target and encoded true peak <= limit. Output: H.264 High yuv420p bt709, moov first.\n'
                   'The EDL handles the mechanical assembly; render anything creative with your own code (rn capture for\n'
                   'HTML/canvas, numpy + rn.fx, Manim, Blender) to a PNG sequence or alpha clip and place it in the EDL.\n\n'
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
                   '    {"clips": [{"frames": "title/%05d.png", "fps": 30, "at": {"clip": 1, "offset": 0.2},\n'
                   '                "until": {"clip_end": 1}, "keys": {"scale": [[0, 0.9], [0.3, 1, "out3"]], "opacity": [[0, 0], [0.2, 1]]}}]}],\n'
                   '  "markers": {"drop": 4.5},                                   # named times for anchors\n'
                   '  "audio": [{"src": "music.mp3", "role": "music", "gain_db": -6, "fade_out": 1.5},\n'
                   '            {"src": "vo.wav", "role": "voice", "at": 0.3}],          # roles: music | voice | sfx\n'
                   '  "subtitles": {"file": "captions.ass"},\n'
                   '  "mix": {"lufs": -14, "true_peak": -1.0, "duck_db": 10}            # duck music under voice\n'
                   '}\n'
                   'Positions: in/out take seconds, {"frame": n} or {"shot": n, "offset": s} (shots from rn cuts).\n'
                   'Overlay "at"/"until" and audio "at" take seconds or an anchor + "offset": {"clip": i}, {"clip_end": i}\n'
                   '(main-track clip i as laid out), {"marker": name}, {"frame": n}, {"beat": n, "listen": f.json},\n'
                   '{"word": n, "listen": f.json, "edge": "start"|"end"}.\n'
                   'Keys [t, value, ease?] (t from the clip start; eases linear in out inout in3 out3 inout3 hold):\n'
                   'main track scale/x/y/rotate (x, y as fractions of the frame); overlays scale/x/y/rotate/opacity\n'
                   '(x, y in px, scale and rotate about the layer centre). Keep main-track scale >= 1: edges smear.')
    assemble.add_argument('edl')
    assemble.add_argument('-o', '--out', default='out.mp4')
    assemble.add_argument('--dry-run', action='store_true', help='validate and print the timeline without rendering')
    assemble.add_argument('--keep', action='store_true', help='keep per-clip slots and the mix next to the output (<out>.parts/)')
    assemble.add_argument('--preview', action='store_true',
                          help='half-size draft with a fast encode: check timing and layout before the full render (not for delivery)')

    compare = add('compare', 'two videos side by side at the same times (+ cut timing)',
                  'A cut against its reference, or two versions of a cut: frames at the same times, A on the top row and B\n'
                  'underneath, labelled with each one\'s own frame index. --cuts runs rn cuts on both and pairs the cuts\n'
                  '(within 0.25 s) with their offsets: the rhythm comparison. --video writes A|B side by side to watch.\n'
                  'Differences are facts to look at, not scores.')
    compare.add_argument('a')
    compare.add_argument('b')
    compare_times = compare.add_mutually_exclusive_group()
    compare_times.add_argument('--count', type=int, metavar='N', help='N evenly spaced times (default 12)')
    compare_times.add_argument('--seconds', type=_numbers(float), metavar='T1,T2,..', help='exact times')
    compare.add_argument('-o', '--out', help='output .jpg (pages get -p2, ...); default sheets/compare-<a>-<b>.jpg')
    compare.add_argument('--width', type=int, default=256, help='tile width in px (default 256)')
    compare.add_argument('--max-kb', type=int, default=125)
    compare.add_argument('--cuts', action='store_true', help='compare shot boundaries (rn cuts defaults)')
    compare.add_argument('--video', metavar='MP4', help='also write a side-by-side video (A left, B right, A\'s sound)')

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
            print(f'{result["path"]}  {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s'
                  + ('  (preview: draft size and encode, not for delivery)' if result.get('preview') else ''))
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


SUMMARIZED = {'cuts', 'listen', 'faces', 'ref', 'capture', 'fx', 'fonts', 'assets', 'captions', 'compare'}
# The pip extra that brings a command's optional Python modules (the media sandbox has them all).
EXTRAS = {'listen': 'listen', 'ref': 'listen', 'faces': 'faces', 'capture': 'capture', 'captions': 'captions', 'fonts': 'fonts',
          'fx': 'fx'}


def _size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split('x'))
    except ValueError:
        raise RnError(f'size "{text}": expected WxH like 1080x1920') from None
    return width, height


def _run_summarized(args) -> tuple[dict, list[str]]:
    """Commands whose module prints its own summary lines."""
    command = args.command
    if command == 'cuts':
        from .cuts import cuts_command
        result = cuts_command(args.file, threshold=args.threshold, min_gap=args.min_gap, sheet=args.sheet, max_kb=args.max_kb)
        lines = [f'{len(result["shots"])} shots, cuts at frames {[c["frame"] for c in result["cuts"]]}',
                 *(f'  shot {s["shot"]}: frames {s["start_frame"]}..{s["end_frame"] - 1}  {s["start"]}-{s["end"]}s' for s in result['shots']),
                 f'suspects (look before trusting): {[(c["frame"], c["score"]) for c in result["suspects"]]}']
        lines += [f'sheet {page["path"]}' for page in result.get('sheet', [])]
        return result, lines
    if command == 'listen':
        from .listen import listen_command, summary_lines
        result = listen_command(args.file, out=args.out, transcribe=args.transcribe, language=args.language, model=args.model, max_kb=args.max_kb)
    elif command == 'faces':
        from .faces import faces_command, summary_lines
        result = faces_command(args.file, every=args.every, model=args.model, min_score=args.min_score, out_sheet=args.sheet, max_kb=args.max_kb)
    elif command == 'ref':
        from .ref import ref_command, summary_lines
        result = ref_command(args.file, out=args.out, every=args.every, transcribe=args.transcribe, language=args.language,
                             model=args.model, threshold=args.threshold)
    elif command == 'capture':
        from .capture import capture_command, summary_lines
        width, height = _size(args.size)
        result = capture_command(args.page, out=args.out, frames=args.frames, width=width, height=height, fps=args.fps,
                                 shards=args.shards, start=args.start)
    elif command == 'fx':
        from .fx import catalog, js_path
        result = {'effects': catalog(), 'js': str(js_path()), 'py': str(js_path().with_name('__init__.py'))}
        lines = [f'{e["name"]:10} {e["js"]:16} {e["py"]:18} {e["what"]}' for e in result['effects']]
        return result, lines + ([f'source: {result["js"]}', f'        {result["py"]}'] if args.source else
                                ['rn fx --source prints the files to read or copy'])
    elif command == 'compare':
        from .compare import compare_command, summary_lines
        result = compare_command(args.a, args.b, out=args.out, count=args.count, seconds=args.seconds, width=args.width,
                                 max_kb=args.max_kb, cuts=args.cuts, video=args.video)
    elif command == 'fonts':
        from .fonts import fonts_command, summary_lines
        result = fonts_command(family=args.family, script=args.script, licenses=args.licenses)
    elif command == 'assets':
        from .assets import get_command, search_command, summary_lines
        if args.action == 'get':
            if not args.terms:
                raise RnError('rn assets get <id>...: give asset ids from rn assets search')
            result = get_command(args.terms, out=args.out)
        else:
            result = search_command(' '.join(args.terms), kind=args.kind, limit=args.limit)
    else:
        from .captions import captions_command, summary_lines
        width, height = _size(args.size)
        result = captions_command(args.words, script=args.script, faces_json=args.faces, out=args.out, width=width, height=height,
                                  max_chars=args.max_chars, max_lines=args.max_lines, safe=args.safe, language=args.language)
    return result, summary_lines(result)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            print(f'rn: error: {tool} not found on PATH', file=sys.stderr)
            return 2
    lines: list[str] = []
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
                                   seconds=args.seconds, count=args.count, width=args.width, columns=args.columns, max_kb=args.max_kb,
                                   around=args.around, span=args.span, diff=args.diff)
        elif args.command in SUMMARIZED:
            result, lines = _run_summarized(args)
        elif args.command == 'assemble':
            from .assemble import assemble_command
            result = assemble_command(args.edl, args.out, dry_run=args.dry_run, keep=args.keep, preview=args.preview)
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
    except ModuleNotFoundError as error:
        extra = EXTRAS.get(args.command, 'all')
        print(f"rn: error: rn {args.command} needs the Python module {error.name} (extra \"{extra}\"): "
              f"uv tool install 'renoise-kit[all]'", file=sys.stderr)
        return 1
    except Exception as error:  # a kit bug: keep the traceback for the report, end on the usual one-liner
        import traceback
        traceback.print_exc()
        print(f'rn: error: unexpected {type(error).__name__}: {error} (a bug in rn; work around it with ffmpeg directly)', file=sys.stderr)
        return 1
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    if args.command in SUMMARIZED:
        print('\n'.join(lines))
    else:
        _print_summary(args.command, result)
    return 1 if args.command == 'qa' and result['status'] == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
