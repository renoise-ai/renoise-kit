"""rn: Renoise media kit. Measure (probe, frame, sheet, cuts, shots, listen, faces, ref, signal, align, text, matte, key, grid, look),
render (capture, template, fx, fonts, assets, captions, grade), edit and assemble an EDL frame-exactly, compare, finish, QA and export. It measures
and guarantees the mechanics; every creative decision stays yours. rn doctor says what this machine can run; rn schema
prints the edit.json schema.

Every command prints a short summary; --json FILE writes the full result. Failures exit non-zero with
`rn: error: <reason>` on the last line. Contract: SPEC.md in the renoise-kit repository.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
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


def _edit_ops(ops) -> None:
    """The rn edit operations; each line of `rn edit EDL batch` is parsed with these too."""
    ops.add_parser('ids', help='give every clip and audio item without an id one (c1.. main, o1.. overlays, a1.. audio)')
    ops.add_parser('upgrade', help='rewrite a version-1 edit.json under version-2 names (style, keys left/top)')
    op_set = ops.add_parser('set', help='set fields: key=value (JSON when it parses), dotted keys reach into objects')
    op_set.add_argument('ref')
    op_set.add_argument('assignments', nargs='*', metavar='key=value')
    op_set.add_argument('--unset', action='append', default=[], metavar='KEY')
    op_split = ops.add_parser('split', help='cut a clip in two at a timeline time; both halves play as the one did')
    op_split.add_argument('ref')
    op_split.add_argument('--at', type=float, required=True, help='timeline seconds')
    op_split.add_argument('--id', help='id for the second half (default <id>-2)')
    op_delete = ops.add_parser('delete', help='remove a clip (main track ripples; --lift keeps a gap)')
    op_delete.add_argument('ref')
    op_delete.add_argument('--lift', action='store_true')
    for name, text in (('insert', 'add a clip given as JSON or @file.json'), ('move', 'move a clip next to another or onto a track')):
        op = ops.add_parser(name, help=text)
        if name == 'insert':
            op.add_argument('--clip', required=True, help='clip JSON or @file.json')
        else:
            op.add_argument('ref')
        op.add_argument('--before', metavar='ID')
        op.add_argument('--after', metavar='ID')
        op.add_argument('--track', metavar='N|audio', help='append to track N (one past the last adds a track) or to audio')
    hits_text = 'one sound effect at many points: every cut, every (Nth) beat, or given seconds (audio items, role sfx)'
    op_hits = ops.add_parser(
        'hits', help=hits_text, description=f'{hits_text[0].upper()}{hits_text[1:]}.\n'
        'Cuts are the start of every main-track clip but the first (with a transition, where it begins), written as\n'
        '{"clip": "<id>"} (or {"clip": i} for a clip without an id), so the items follow later edits; beats are\n'
        '{"beat": n, "listen": LISTEN.json}; given seconds stay where they are. A point before 0 s or past the end of\n'
        'the main track is not placed and is listed. Beat times are the analysed file\'s: when the music starts later in\n'
        'the edit, add its start to --offset. The sound can come from rn assets get <id> -o assets/.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    op_hits.add_argument('sfx', metavar='SFX_FILE|PACK_ID',
                         help='the sound, relative to the current folder (or to the EDL), or an rn pack id like whoosh/air-1 '
                              '(copied to sfx/ next to the EDL; without --pivot its measured peak lands on each point)')
    op_hits.add_argument('--at', required=True, metavar='cuts|beats|T1,T2,..',
                         help='main-track cuts, the beats of --listen, or timeline seconds')
    op_hits.add_argument('--listen', metavar='LISTEN.json', help='rn listen MUSIC --json LISTEN.json, for --at beats')
    op_hits.add_argument('--offset', type=float, default=0.0, metavar='S',
                         help='seconds added to every point (negative: earlier); kept in the anchor')
    op_hits.add_argument('--pivot', type=float, metavar='S',
                         help='the second of the sound that lands on each point, e.g. a whoosh\'s peak ("pivot")')
    op_hits.add_argument('--gain', type=float, metavar='DB', help='"gain_db" of every item')
    op_hits.add_argument('--every', type=int, default=1, metavar='N', help='only the 1st, (N+1)th, (2N+1)th, ... point')
    op_sil = ops.add_parser('silences', help='cut a main-track clip\'s long pauses out (its "ranges"), from rn listen of its source',
                            description='Cut the silences of a main-track clip\'s source, as rn listen measured them, out of the clip: it\n'
                            'gets "ranges", the spans between silences of at least --min seconds, inside its own in/out (or its\n'
                            'ranges: run it again with other numbers to cut more); dead air before the first word and after the last goes\n'
                            'at any length. --pad seconds stay next to the speech at each cut,\n'
                            '--keep-gap seconds of each removed pause stay in all, so the rhythm is not airless. Defaults suit a\n'
                            'talking head; what follows the clip ripples. Example:\n'
                            '  rn listen take.mp4 --speech --json take.json && rn edit e.json silences c1 --listen take.json',
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    op_sil.add_argument('ref', metavar='CLIP')
    op_sil.add_argument('--listen', required=True, metavar='LISTEN.json', help='rn listen SOURCE (--speech) --json of the clip\'s source')
    op_sil.add_argument('--min', type=float, default=0.5, metavar='S', help='only silences this long are cut (default 0.5)')
    op_sil.add_argument('--pad', type=float, default=0.1, metavar='S', help='kept next to the speech on each side of a cut (default 0.1)')
    op_sil.add_argument('--keep-gap', type=float, default=0.0, metavar='S',
                        help='seconds of each removed pause kept in all (default 0: only the pads)')
    op_hits.add_argument('--id-prefix', default='sfx', metavar='P', help='item ids P1, P2, ... skipping ids already used (default sfx)')
    snap_text = 'move main-track cuts onto the nearest beat or downbeat (rolling edits: nothing else moves)'
    op_snap = ops.add_parser(
        'snap', help=snap_text, description=f'{snap_text[0].upper()}{snap_text[1:]}.\n'
        'For each cut, the clip before it ends later or earlier and the clip after it starts later or earlier in its own\n'
        'source by the same time (a rolling edit), so the cut lands on the beat and the timeline length stays. A cut with a\n'
        'transition is snapped by the transition\'s middle. Cuts with no beat within --tolerance, or whose clips have no\n'
        'source to spare, stay where they are and are listed with the reason.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    op_snap.add_argument('--listen', required=True, metavar='LISTEN.json', help='rn listen MUSIC --json LISTEN.json')
    op_snap.add_argument('--to', choices=['beats', 'downbeats'], default='beats')
    op_snap.add_argument('--tolerance', type=float, default=0.2, metavar='S', help='only move a cut this far at most (default 0.2 s)')
    op_snap.add_argument('--only', metavar='ID,..', help='only the cuts into these clips (ids of the clips that start at the cut)')
    op_snap.add_argument('--offset', type=float, default=0.0, metavar='S',
                         help='where the analysed music starts on the timeline (its audio item\'s "at")')


def _crop(text: str) -> tuple[int, int, int, int]:
    try:
        w, h, x, y = (int(part) for part in text.split(':'))
    except ValueError:
        raise argparse.ArgumentTypeError('expected W:H:X:Y in whole pixels, e.g. 260:120:60:290') from None
    return w, h, x, y


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='rn', description=__doc__, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=f'rn {__version__}')
    sub = parser.add_subparsers(dest='command', required=True, metavar='command')

    def add(name: str, help_text: str, description: str) -> argparse.ArgumentParser:
        # No help= here: rn --help lists the commands by job (GROUPS), not argparse's flat list.
        command = sub.add_parser(name, description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
        SHORT[name] = help_text
        command.add_argument('--json', metavar='FILE', help='write the full result as JSON')
        if name in PROOF_COMMANDS:
            command.add_argument('--no-proof', action='store_true', help='skip the proof image (<out>.proof.jpg) written to look at')
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
                '  rn sheet out.mp4 --indices 0,47,48,95  # both sides of each cut\n'
                '  rn sheet out.mp4 --seconds 8.4,9.6 --crop 260:120:60:290 --width 520  # zoom into the sign')
    sheet.add_argument('files', nargs='+', help='videos, or folders of stills (tiled in name order, labelled by file name)')
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
    sheet.add_argument('--crop', type=_crop, metavar='W:H:X:Y',
                       help='zoom every tile into one region, in source pixels (ffmpeg crop order): a sign, a caption, a face')

    cuts = add('cuts', 'shot boundaries by decoded frame (+ suspects to check)',
               'Scene-change scores per decoded frame; a cut where the score passes --threshold. Continuous camera moves\n'
               'and morphs score low, so weaker changes are listed as suspects: look at --sheet before trusting either.\n'
               'The shots it finds are what EDL positions {"shot": n} mean.')
    cuts.add_argument('file')
    cuts.add_argument('--threshold', type=float, default=0.3)
    cuts.add_argument('--min-gap', type=float, default=0.3, help='seconds; closer cuts keep the stronger one')
    cuts.add_argument('--sheet', metavar='JPG', help='contact sheet of both sides of every cut and suspect')
    cuts.add_argument('--max-kb', type=int, default=125)

    shots = add('shots', 'every shot (dissolves, fades and wipes too) and what its picture does, as numbers',
                'Boundaries: hard cuts, fades through black/white, dissolves (told apart from camera moves and animated\n'
                'gradients), straight wipes (with the way the edge runs) and flash cuts; each dissolve, fade and wipe with\n'
                'how many frames it takes. Per shot: still (identical frames), cadence (24->30 pulldown repeats), motion\n'
                '(how well one zoom/pan of the whole frame explains it, zoom/s, pan px/s), texture (flat blocks, colours,\n'
                'straight edges, noise, fine texture, gentle slopes, vivid colour, light/dark smoothness), pixels (unchanged\n'
                'share, noise in still areas), speed (change per quarter, fastest over slowest, longest hold) and with\n'
                '--faces how often a face fills the picture. Numbers only, no\n'
                'labels. rn cuts finds hard cuts only and stays what EDL {"shot": n} means; rn shots can count more shots.')
    shots.add_argument('file')
    shots.add_argument('--min-shot', type=float, default=0.35, help='seconds; a shorter shot merges into the one before')
    shots.add_argument('--threshold', type=float, default=0.3, help='hard-cut score (0..1)')
    shots.add_argument('--jobs', type=int, help='shots measured in parallel (default: every CPU this machine or container allows)')
    shots.add_argument('--sheet', metavar='JPG', help='contact sheet: first, middle and last frame of every shot')
    shots.add_argument('--max-kb', type=int, default=125)
    shots.add_argument('--faces', action='store_true',
                       help='also per shot: in how many sampled frames a face covers 2%%+ of the picture (YuNet, as rn faces)')
    shots.add_argument('--face-model', metavar='ONNX', help='YuNet model (default: as rn faces finds it)')
    shots.add_argument('--beats', nargs='?', const='audio', metavar='LISTEN.json',
                       help='each boundary against the nearest beat: of the file\'s own sound, or of an rn listen --json result '
                            '(music for a recut); needs the "listen" extra for the file\'s own sound')
    shots.add_argument('--keyframes', metavar='DIR',
                       help='one full-size PNG per shot (DIR/shot_000.png ...), frame-exact, from frames clear of its transitions; '
                            'shot["keyframe"] gives its index, time and sharpness')
    shots.add_argument('--which', choices=['sharpest', 'mid', 'first', 'last'], default='sharpest',
                       help='--keyframes: the sharpest of 5 frames across the shot (default; variance of the Laplacian), or its '
                            'middle, first or last clear frame')

    listen = add('listen', 'audio as facts + one picture (you cannot hear)',
                 'Loudness (integrated, true peak, short-term curve), silences, onsets, tempo with half/double\n'
                 'alternatives and beat times, and with --transcribe word timestamps (faster-whisper). One image on a\n'
                 'shared time axis: waveform, spectrogram, loudness, onset and beat ticks. Facts only, no cut suggestions.\n'
                 'Its --json feeds EDL anchors {"beat": n, "listen": ...} / {"word": n, ...} and rn captions.\n'
                 '--speech instead measures voice takes (several files at once): where the speech starts and ends (TTS pads\n'
                 'both ends, so the file length lies), its length, the pauses inside it and "silences" for rn edit silences.\n'
                 'With --expect the take\'s intended text: words (CJK: characters) per second of speech; add --transcribe for\n'
                 '"match": ok | mismatch against that text, with the stretches that differ (a wrong or cut-off take). Examples:\n'
                 '  rn listen vo/*.wav --speech --json takes.json\n'
                 '  rn listen vo3.wav --speech --expect @lines/3.txt --transcribe')
    listen.add_argument('files', nargs='+', metavar='file', help='one file; several with --speech')
    listen.add_argument('-o', '--out', help='image path (default listen/<name>.jpg)')
    listen.add_argument('--transcribe', action='store_true')
    listen.add_argument('--language', help='e.g. zh, ja, en (default: detect)')
    listen.add_argument('--model', default='small', help='faster-whisper model (small is baked into the sandbox)')
    listen.add_argument('--max-kb', type=int, default=125)
    listen.add_argument('--speech', action='store_true', help='voice-take facts only (no picture, no beats): speech span, pauses, silences')
    listen.add_argument('--expect', metavar='TEXT|@FILE', help='--speech: the words the take should say')
    listen.add_argument('--pause', type=float, default=0.8, metavar='S', help='--speech: a quiet stretch inside the speech this long is a pause (default 0.8)')
    listen.add_argument('--silence-db', type=float, default=-45.0, metavar='DB',
                        help='--speech: 20 ms windows quieter than this (dBFS RMS) are silence (default -45; raise it for a noisy room)')
    listen.add_argument('--match-min', type=float, default=0.85, metavar='R',
                        help='--speech --expect --transcribe: similarity (0..1) from which the take counts as "match": "ok" (default 0.85)')

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
    faces.add_argument('--track', action='store_true',
                       help='one subject per frame: picked, followed, gaps filled, smoothed ("track" in --json)')
    faces.add_argument('--pick', default='largest', choices=['largest', 'center', 'left', 'right'],
                       help='which face is the subject (default largest; picked again after a gap longer than --max-gap)')
    faces.add_argument('--smooth', type=float, default=0.25, metavar='S', help='Gaussian smoothing of the track, sigma in seconds (0: off)')
    faces.add_argument('--max-gap', type=int, default=12, metavar='N', help='frames without the face that are filled in (default 12)')
    faces.add_argument('--reframe', metavar='WxH',
                       help='a moving crop with this aspect that keeps the subject in frame (implies --track), e.g. 1080x1920 '
                            'from 16:9; -o renders it')
    faces.add_argument('--zoom', type=float, default=1.0, help='--reframe: crop tighter than the largest window (>= 1)')
    faces.add_argument('-o', '--out', metavar='MP4', help='--reframe: render the reframed video (H.264, sound copied) to use as "src"')

    key = add('key', 'backdrop matte (green / blue / any solid colour, or white): grey PNGs for EDL masks, or cut-outs',
              'For footage or stills on a real backdrop, no model: the backdrop colour is measured on the frame border (or\n'
              'given), and each pixel\'s distance from it (chroma, luma at half weight) becomes alpha. --white instead keys\n'
              'near-white pixels connected to the border, so white inside the subject (teeth, a label) stays. Writes\n'
              'DIR/%05d.png, white = subject, ready as an overlay "mask"; --cutout writes RGBA cut-outs (--despill takes\n'
              'the backdrop\'s tint off the edges). Reports the backdrop colour, how much of the border matches it, the\n'
              'subject\'s coverage and soft-edge share. Faster than rn matte and exact on a real backdrop; for footage\n'
              'without one use rn matte.\n'
              'Examples:\n  rn key avatar_greenscreen.mp4 -o key/avatar --cutout --despill\n'
              '  rn key product_white.png -o key/product --white --cutout')
    key.add_argument('file', help='video or image')
    key.add_argument('-o', '--out', required=True, help='output folder (its PNGs are replaced)')
    key.add_argument('--color', default='auto', metavar='auto|#rrggbb', help='backdrop colour (default: measured on the border)')
    key.add_argument('--white', action='store_true', help='white / near-white backdrop, keyed by flood fill from the border')
    key.add_argument('--tolerance', type=float, help='distance from the backdrop still keyed fully out (0..255; default 40)')
    key.add_argument('--softness', type=float,
                     help='width of the edge ramp: colour distance (default 30); with --white, a feather radius in px (default 1)')
    key.add_argument('--despill', action='store_true', help='--cutout: cap the backdrop\'s main channel at the others on the subject')
    key.add_argument('--cutout', action='store_true', help='write RGBA cut-outs instead of grey mattes')
    key.add_argument('--every', type=int, default=1, help='key every Nth frame and hold in between')

    grid = add('grid', 'slice a sheet (storyboard, character sheet, 2x2 / 3x3 grid) into tiles',
               'Rows and columns not given are found from the gutters: full-length lines of one even colour between\n'
               'tiles. With --rows / --cols the sheet is split evenly, --gap px between tiles. --trim drops a uniform\n'
               'margin or frame around each tile. Writes DIR/r1c1.png ...\n'
               'Examples:\n  rn grid board.png -o tiles/\n  rn grid kf_grid.png -o kf/ --rows 3 --cols 3')
    grid.add_argument('image')
    grid.add_argument('-o', '--out', required=True, help='output folder')
    grid.add_argument('--rows', type=int)
    grid.add_argument('--cols', type=int)
    grid.add_argument('--gap', type=int, default=0, help='with --rows / --cols: px between tiles')
    grid.add_argument('--trim', action='store_true', help='cut each tile down to what differs from its edge colour')

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

    capture = add('capture', 'render an HTML page frame by frame to RGBA PNGs, or shoot a live site',
                  'The page defines window.renderFrame(i) (may be async) and draws frame i completely: the frame index is\n'
                  'the time, so frames are exact and any order or shard gives the same pixels. window.RN = {fps, frames,\n'
                  'width, height, shard} and window.RNFX (rn fx) exist before the page runs; window.rnReady (a Promise) is\n'
                  'awaited once. Transparent where the page does not paint. Put the result in an EDL as\n'
                  '{"frames": "<dir>/%05d.png", "fps": R}. Example:\n'
                  '  <canvas id=c></canvas><script>const x = c.getContext("2d"); c.width = RN.width; c.height = RN.height;\n'
                  '  window.renderFrame = i => { x.clearRect(0, 0, c.width, c.height); /* draw frame i */ RNFX.grain(x, i, {}); };</script>\n'
                  'A live site (http/https URL) instead: real screenshots in a phone (default) or desktop browser:\n'
                  '  rn capture https://example.com -o site                                   # full.png, the whole page\n'
                  '  rn capture https://example.com -o site --sections "Pricing,Reviews" --hide "#cookie-banner"\n'
                  '  rn capture https://example.com -o site --scroll-video site/scroll.mp4 --duration 6   # 1080x1920 scroll')
    capture.add_argument('page', help='an HTML file with renderFrame(i), or an http(s) URL of a live site')
    capture.add_argument('-o', '--out', required=True, help='output folder (its PNGs are replaced)')
    capture.add_argument('--frames', type=int, help='page: frames to render (required for a page)')
    capture.add_argument('--size', metavar='WxH', help='output pixels (page default 1080x1920; URL: 1080x1920 mobile, 1920x1080 desktop)')
    capture.add_argument('--fps', type=float, default=30)
    capture.add_argument('--shards', type=int, default=4, help='parallel Chromium instances (each boots in 1-3 s)')
    capture.add_argument('--start', type=int, default=0, help='first frame index (render a part)')
    web = capture.add_argument_group('a live site (rn capture https://... -o shots): app and product ads')
    device = web.add_mutually_exclusive_group()
    device.add_argument('--mobile', dest='device', action='store_const', const='mobile', help='a phone (default): 360x640 CSS px at 3x')
    device.add_argument('--desktop', dest='device', action='store_const', const='desktop', help='1280x720 CSS px at 1.5x')
    web.add_argument('--full', action='store_true', help='the whole page as full.png (the default when nothing else is asked)')
    web.add_argument('--sections', metavar='"Pricing,Reviews"', help='a still of each section, found by its heading text')
    web.add_argument('--scroll-video', metavar='MP4', help='a scroll from top to bottom, frame-exact, eased, held at both ends')
    web.add_argument('--duration', type=float, default=8.0, metavar='S', help='--scroll-video length (default 8 s)')
    web.add_argument('--hide', metavar='"SEL,SEL"', help='CSS selectors to hide first: cookie banners, chat bubbles, pop-ups')

    signal = add('signal', 'frame-by-frame numbers: luma, motion, saturation, loudness, bands, onsets, clicks',
                 'One series per quantity on the video\'s frame clock (30/s for audio alone): luma, frame difference and\n'
                 'saturation (0..1); rms, low/mid/high band levels (dB) and onset (dB of spectral rise); plus flash frames\n'
                 'and click times. Flashes, freezes, pacing, beats and music-driven motion all read from it. -o draws them.')
    signal.add_argument('file')
    signal.add_argument('-o', '--out', metavar='PNG', help='plot the series on one time axis (needs matplotlib: extra listen)')

    align = add('align', 'where recording B sits inside A, by sound (offset + how sure)',
                'Cross-correlates onset envelopes, then refines on the waveform: a song inside a reference edit, camera\n'
                'angles, a separately recorded voice. "match" is the waveform correlation at that offset (near 1: the same\n'
                'sound); "distinct" how far ahead of other offsets it is (low for repetitive music).')
    align.add_argument('a')
    align.add_argument('b')
    align.add_argument('--max-offset', type=float, metavar='S', help='only consider offsets within +-S seconds')

    matte = add('matte', 'subject alpha per frame (rembg) as a grey PNG sequence for EDL masks',
                'White = subject. Use it as an overlay "mask" ("invert": true keeps graphics off the subject, e.g. text\n'
                'behind a person) or --cutout for RGBA cut-outs to place as an overlay. Check edges on a sheet.')
    matte.add_argument('file')
    matte.add_argument('-o', '--out', required=True, help='output folder (its PNGs are replaced)')
    matte.add_argument('--model', default='isnet-general-use', help='rembg model: isnet-general-use (any subject), u2net_human_seg (people), ...')
    matte.add_argument('--every', type=int, default=1, help='compute every Nth frame and hold in between (faster, less exact)')
    matte.add_argument('--cutout', action='store_true', help='write the subject as RGBA instead of the grey matte')

    text = add('text', 'words on screen and when they are there (tesseract OCR)',
               'Reads every --every-th frame (default two per second); lines with boxes per frame, and spans: the same\n'
               'line read in consecutive samples. OCR misreads stylised type: boxes are where to look, strings a draft.\n'
               'Each span has a style measured on the frame where it was first read: color (the glyphs), background,\n'
               'outline (a contrasting ring around the glyphs, else null), all #rrggbb, and height_px / height_share\n'
               '(box height in source pixels / as a share of the frame height).')
    text.add_argument('file')
    text.add_argument('--every', type=int, metavar='N')
    text.add_argument('--lang', help='tesseract languages, e.g. eng+chi_sim (default: those installed of eng, chi_sim, chi_tra, jpn, kor)')
    text.add_argument('--min-conf', type=float, default=50, help='drop words below this confidence (0-100)')

    look = add('look', 'tone and colour as numbers: black/mid/white L*, zone casts, chroma, clipping; vs references',
               'Per input (an image, a folder of stills, or a video sampled at --seconds / --count frames, default 8; with\n'
               '--shots the middle frame of every shot of one video): L* black / mid / white (p2 / p50 / p98) and contrast, the\n'
               'mean a*/b* cast in shadows, mids and highlights (L* < 33, 33-66, > 66), mean chroma, the share of crushed and\n'
               'blown pixels (flagged above 1%). With --ref the references are pooled, and each input gets its differences, a\n'
               'distance (tone + cast + chroma / 2; lower is closer), dE76 of the mean Lab and a verdict: close (<= --tolerance),\n'
               'noticeable (<= 3x) or far. Generated shots from different calls drift in grade: measure them against one\n'
               'reference, then rn grade the far ones. Examples:\n'
               '  rn look gen/*.mp4 --ref ref/hero.png\n'
               '  rn look ref.mp4 --shots ref-shots.json       # the source look shot by shot (rn shots ref.mp4 --json ref-shots.json)')
    look.add_argument('files', nargs='+', help='images, folders of stills or videos')
    look.add_argument('--ref', nargs='+', metavar='REF', help='reference images, folders or videos (pooled)')
    look_times = look.add_mutually_exclusive_group()
    look_times.add_argument('--seconds', type=_numbers(float), metavar='T1,T2,..', help='video frames on screen at these times')
    look_times.add_argument('--count', type=int, metavar='N', help='N evenly spaced video frames (default 8)')
    look_times.add_argument('--shots', metavar='SHOTS.json', help='rn shots --json result of the one input: its middle frame per shot')
    look.add_argument('--tolerance', type=float, default=4.0, help='distance still called "close" (default 4)')

    grade = add('grade', 'a .cube LUT that moves a clip\'s tone and colour toward references',
                'Fits one per-pixel transform from frames pooled across SRC and the references (no flicker): an L* tone curve\n'
                'through matched percentiles, chroma scaled by the ratio of mean chroma (0.5-2x), and each zone\'s mean a*/b*\n'
                'moved to the reference\'s with soft zone weights; --strength blends it with the original. Writes a .cube\n'
                '(sRGB in and out) and reports the distance to the references before and after (as rn look). Apply it in an\n'
                'EDL on the clip: "vf": "lut3d=file=look.cube" (a path relative to the EDL, like every path there), or\n'
                'ffmpeg -vf lut3d=file=look.cube. Check the result with rn look on the graded render. Example:\n'
                '  rn grade gen/s3.mp4 --ref ref/hero.png -o looks/s3.cube --preview sheets/s3-graded.png')
    grade.add_argument('src', help='image, folder of stills or video to grade')
    grade.add_argument('--ref', nargs='+', required=True, metavar='REF', help='reference images, folders or videos (pooled)')
    grade.add_argument('-o', '--out', required=True, help='.cube path')
    grade.add_argument('--strength', type=float, default=1.0, help='0..1 blend with the original (default 1)')
    grade.add_argument('--size', type=int, default=33, help='LUT points per axis (default 33)')
    grade.add_argument('--count', type=int, metavar='N', help='frames sampled per video (default 12)')
    grade.add_argument('--preview', metavar='PNG', help='SRC\'s first sample graded through the LUT by ffmpeg')

    export = add('export', 'the EDL as an editable project: .otio, .fcpxml (Final Cut), .xml (Premiere, Resolve)',
                 'Cuts, trims, speed, transitions, nested edits, overlay and audio timing carry over, media by absolute path.\n'
                 'Position/size, keys, blend, masks, raw filters and gain curves are kept as "rn" metadata and listed.')
    export.add_argument('edl')
    export.add_argument('-o', '--out', required=True, help='.otio, .fcpxml or .xml')
    export.add_argument('--srt', metavar='FILE', help='also write the subtitles as SRT')

    template = add('template', 'built-in motion templates (headline, lower third, callout, counter, bars, steps, text behind) to PNGs',
                   'Parameterised HTML pages rendered with rn capture to an RGBA PNG sequence for an EDL overlay. Each page is\n'
                   'readable: rn template show NAME prints its params (with defaults) and its path to copy and change.\n'
                   '  rn template list\n'
                   '  rn template show counter\n'
                   '  rn template render counter -o stat --size 1080x1920 --duration 3 --params \'{"to": 250, "suffix": "%%"}\'\n'
                   'Then in the EDL: {"frames": "stat/%%05d.png", "fps": 30, "at": 2.0}. Every template takes font (a CSS family\n'
                   'list) and font_file (a font file loaded first), in / out (entrance and fade-out seconds) and colours.')
    template.add_argument('action', choices=['list', 'show', 'render'])
    template.add_argument('name', nargs='?')
    template.add_argument('-o', '--out', help='render: output folder (its PNGs are replaced)')
    template.add_argument('--size', default='1080x1920', metavar='WxH')
    template.add_argument('--fps', type=float, default=30)
    template.add_argument('--duration', type=float, default=3.0, help='seconds (default 3)')
    template.add_argument('--params', metavar='JSON|@file', help='parameter overrides')
    template.add_argument('--shards', type=int, default=4)

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

    pack = add('pack', 'the built-in offline pack: sound effects, fonts, colour looks, icons (licence-clean)',
               'Ships inside rn, no network: CC0 sound effects with measured onset/peak, OFL/Apache fonts, colour looks as\n'
               '.cube LUTs, Lucide icons as SVG. Ids: <category>/<name> (sounds), font/<name>, look/<name>, icon/<name>.\n'
               '  rn pack list --kind sfx --search whoosh         # what there is (categories, peak times)\n'
               '  rn edit e.json hits whoosh/air-1 --at cuts      # a pack id works directly; its peak lands on each cut\n'
               '  rn pack path look/teal-orange                   # EDL clip: "vf": "lut3d=file=<that path>"\n'
               '  rn pack path font/anton                         # drawtext fontfile=..., or any page/tool that takes a file\n'
               '  rn pack get icon/arrow-right -o icons/ --color "#ffffff"   # an SVG to rasterise or inline\n'
               'Pack fonts are found by family name in rn assemble subtitles (.ass Fontname=Anton) and in rn capture pages\n'
               '(font-family: "Anton"; already loaded, so canvas text works without waiting). Licences: rn pack path licenses.')
    pack.add_argument('action', choices=['list', 'show', 'path', 'get'])
    pack.add_argument('ids', nargs='*', metavar='ID', help='item ids for show / path / get')
    pack.add_argument('--kind', choices=['sfx', 'font', 'look', 'icon'])
    pack.add_argument('--category')
    pack.add_argument('--search', metavar='WORDS')
    pack.add_argument('--limit', type=int, default=60)
    pack.add_argument('-o', '--out', default='pack', help='folder for get (default pack/)')
    pack.add_argument('--color', default='currentColor', help='icons: stroke colour (default currentColor)')
    pack.add_argument('--stroke-width', type=float, default=2, help='icons: stroke width in the 24-unit icon box (default 2)')

    assets = add('assets', 'licensed sound effects, overlay footage, textures, music and LUTs from the Renoise library (online; offline: rn pack)',
                 'rn assets search --kind overlay       # no words: what the library holds, counted by category\n'
                 'rn assets search "warm light leak" --kind overlay ; rn assets get <id>... -o assets/\n'
                 'search tiles the matches\' previews into a proof image to look at before you download; get prints the EDL\n'
                 'object to drop in (blend mode, role, lut3d). Files are checked against their sha256 and cached; the licence\n'
                 'comes with each file. The pack that ships inside rn (sounds, fonts, looks, icons; no network) is rn pack.')
    assets.add_argument('action', choices=['search', 'get'])
    assets.add_argument('terms', nargs='*', help='search words, or asset ids for get')
    assets.add_argument('--kind', choices=['sfx', 'overlay', 'texture', 'music', 'lut'])
    assets.add_argument('--limit', type=int, default=20)
    assets.add_argument('-o', '--out', default='assets')

    captions = add('captions', 'timed, chunked, face-aware captions from rn listen words (plain or animated styles)',
                   'Align your script to the transcript timing, chunk lines (CJK by character with kinsoku, Latin by word),\n'
                   'keep to platform safe areas and off faces. Writes .ass, .srt and .json with per-word times.\n'
                   '--style (the .ass; every style is readable ASS override tags you can edit):\n'
                   '  plain      one readable style, outline + shadow (default)\n'
                   '  clean      bold, heavy black outline + soft drop shadow, no plate (add --upper for all caps)\n'
                   '  pop        clean, each caption scales in with a small overshoot\n'
                   '  karaoke    words turn --accent as they are spoken and stay\n'
                   '  highlight  only the word being spoken is --accent\n'
                   '  box        text on a box (--accent is the box colour; default black at 75%%)\n'
                   'Animated styles use larger type than plain; lower --max-chars for fewer, bigger words per line.\n'
                   'Burn with the EDL: "subtitles": {"file": "caps.ass"}.')
    captions.add_argument('words', help='rn listen --transcribe --json output')
    captions.add_argument('-o', '--out', required=True, help='.ass path (.srt and .json alongside)')
    captions.add_argument('--size', required=True, metavar='WxH', help='output video size')
    captions.add_argument('--script', help='text file with the intended wording')
    captions.add_argument('--faces', help='rn faces --json output of the footage')
    captions.add_argument('--max-chars', type=int)
    captions.add_argument('--max-lines', type=int, default=2)
    captions.add_argument('--safe', default='auto', choices=['auto', 'tiktok', 'reels', 'youtube', 'none'])
    captions.add_argument('--language')
    captions.add_argument('--style', default='plain', choices=['plain', 'clean', 'pop', 'karaoke', 'highlight', 'box'])
    captions.add_argument('--font', help='font family (default: Noto Sans / Noto Sans CJK for the language; rn fonts lists them)')
    captions.add_argument('--color', metavar='#RRGGBB', help='text colour (default white)')
    captions.add_argument('--accent', metavar='#RRGGBB', help='karaoke / highlight word colour (default #ffe000), box colour for box')
    captions.add_argument('--position', default='bottom', choices=['bottom', 'middle', 'top'],
                          help='preferred band; a caption over a face (--faces) moves to another band')
    captions.add_argument('--upper', action='store_true', help='all caps')

    assemble = add('assemble', 'render an EDL (edit.json) to a finished MP4',
                   'Frame-exact assembly: cuts, speed, fit, transitions, overlay tracks, subtitles, clip audio + music/voice/sfx,\n'
                   'ducking, LUFS target and encoded true peak <= limit. Output: H.264 High yuv420p bt709, moov first.\n'
                   'The EDL handles the mechanical assembly; render anything creative with your own code (rn capture for\n'
                   'HTML/canvas, numpy + rn.fx, Manim, Blender) to a PNG sequence or alpha clip and place it in the EDL.\n\n'
                   'edit.json version 2 (paths relative to it; times in seconds; # marks notes below, not JSON; a "note"\n'
                   'field is allowed anywhere; full JSON Schema: rn schema):\n'
                   '{\n'
                   '  "version": 2,\n'
                   '  "output": {"width": 1080, "height": 1920, "fps": 30, "background": "#000000", "crf": 18,\n'
                   '             "max_mbps": "auto"},             # optional bitrate ceiling: Mb/s or auto\n'
                   '  "tracks": [\n'
                   '    {"clips": [                                  # track 0 = main track, clips back to back\n'
                   '      {"src": "s1.mp4", "in": 0.4, "out": 3.2},\n'
                   '      {"src": "s2.mp4", "in": {"frame": 6}, "duration": 2.5, "speed": 0.5, "audio": false,\n'
                   '       "transition": {"type": "fade", "duration": 0.4}},   # any xfade name: dissolve, wipeleft, slideup...\n'
                   '      {"image": "card.png", "duration": 1.5, "style": {"object-fit": "contain"}},\n'
                   '      {"color": "#000000", "duration": 0.5}]},\n'
                   '    {"clips": [                                  # later tracks = overlays, placed in time and space\n'
                   '      {"image": "title.png", "at": 0.5, "duration": 2, "fade_in": 0.2, "fade_out": 0.2,\n'
                   '       "style": {"left": 90, "top": "73%", "width": 900, "opacity": 0.9, "mix-blend-mode": "screen"}}]},\n'
                   '    {"clips": [{"frames": "title/%05d.png", "fps": 30, "at": {"clip": 1, "offset": 0.2},\n'
                   '                "until": {"clip_end": 1}, "keys": {"scale": [[0, 0.9], [0.3, 1, "out3"]], "opacity": [[0, 0], [0.2, 1]]}}]}],\n'
                   '  "markers": {"drop": 4.5},                                   # named times for anchors\n'
                   '  "audio": [{"src": "music.mp3", "role": "music", "gain_db": -6, "fade_out": 1.5},\n'
                   '            {"src": "vo.wav", "role": "voice", "at": 0.3}],          # roles: music | voice | sfx\n'
                   '  "subtitles": {"file": "captions.ass"},\n'
                   '  "mix": {"lufs": -14, "true_peak": -1.0, "duck_db": 10}            # duck music under voice\n'
                   '}\n'
                   '"style" uses CSS names: overlays left/top/width/height (px: 90 or "90px"; "50%" of the frame), opacity,\n'
                   'scale, rotate (degrees: 5 or "5deg"), object-fit (cover | contain | fill), mix-blend-mode (normal multiply\n'
                   'screen overlay darken lighten color-dodge color-burn hard-light soft-light difference exclusion\n'
                   'plus-lighter); the main track fills the frame: object-fit, left/top, scale, rotate.\n'
                   'Positions: in/out take seconds, {"frame": n} or {"shot": n, "offset": s} (shots from rn cuts).\n'
                   'Overlay "at"/"until" and audio "at" take seconds or an anchor + "offset": {"clip": i or "id"},\n'
                   '{"clip_end": i or "id"} (main-track clip as laid out, or an earlier overlay by id), {"marker": name},\n'
                   '{"frame": n}, {"beat": n, "listen": f.json}, {"word": n, "listen": f.json, "edge": "start"|"end"}.\n'
                   'Keys [t, value, ease?] (t from the clip start; eases linear in out inout in3 out3 inout3 hold), the\n'
                   'moving form of a style value: main track scale/left/top/rotate (left, top: px the frame moves), speed\n'
                   '(a ramp; drops the clip\'s sound), gain_db; overlays scale/left/top/rotate/opacity/speed (scale and rotate\n'
                   'about the layer centre); audio gain_db. Keep main-track scale >= 1: edges smear.\n'
                   'Any clip or audio item: "id" (anchors, rn edit), "meta" (anything, kept as is), "vf" / "af" (one raw\n'
                   'ffmpeg filter chain, e.g. "eq=saturation=1.2,unsharp"; af on main-track clips and audio).\n'
                   'Main track: "ranges": [[in, out], ...] plays spans of one source back to back; "src": "part.json"\n'
                   'plays another edit. Overlays: "mask": {"frames"|"image"|"src": ..., "mode": "luma"|"alpha", "invert": bool}\n'
                   '(full frame, e.g. from rn matte). Overlays and audio: "pivot": s puts that point of the source (a hit, a\n'
                   'downbeat) on "at". "mix": {"cut_fade": 0.01} fades every hard cut\'s own sound. "accept": [{"check", "at",\n'
                   '"note"}] waives qa warnings judged intended. A file without "version" uses the version-1 names (x, y, w,\n'
                   'h, fit, blend; main-track keys x/y as fractions of the frame) and is upgraded on load; rn edit FILE\n'
                   'upgrade rewrites it.')
    assemble.add_argument('edl')
    assemble.add_argument('-o', '--out', default='out.mp4')
    assemble.add_argument('--dry-run', action='store_true', help='validate and print the timeline without rendering')
    assemble.add_argument('--keep', action='store_true', help='keep per-clip slots and the mix next to the output (<out>.parts/)')
    assemble.add_argument('--preview', action='store_true',
                          help='half-size draft with a fast encode: check timing and layout before the full render (not for delivery)')
    assemble.add_argument('--placeholders', action='store_true',
                          help='a missing source becomes a card (id, meta.label or meta.prompt) for its time span instead of an error')
    assemble.add_argument('--range', metavar='A-B', help='render only seconds A to B of the timeline (laid out in full, so anchors hold)')
    assemble.add_argument('--no-cache', action='store_true',
                          help='render every slot afresh (default: reuse slots and nested edits from .rn-cache next to the EDL, or RN_CACHE)')

    music = add('music', 'fit a music bed to the edit\'s length, ending on a bar or phrase',
                'rn music fit MUSIC --duration S -o bed.m4a: a bed exactly S seconds long that starts on a downbeat and whose\n'
                'final fade begins on a phrase boundary (4 bars from the start; --end-on downbeat|beat for finer points), so it\n'
                'ends like it was cut for the video. Shorter music is looped on phrase boundaries with a short crossfade.\n'
                'Without -o (and no loop needed) it only prints the EDL audio item: {"src", "in", "out", "fade_out"}.\n'
                'Beats and downbeats come from the music (extra "listen") or --listen (rn listen --json).')
    music.add_argument('action', choices=['fit'])
    music.add_argument('file')
    music.add_argument('--duration', type=float, required=True, metavar='S', help='bed length in seconds (the edit\'s length)')
    music.add_argument('-o', '--out', help='.wav, .m4a, .mp3 or .flac (omit to get the EDL audio item only)')
    music.add_argument('--listen', metavar='LISTEN.json', help='rn listen MUSIC --json result (skips the analysis)')
    music.add_argument('--start', type=float, metavar='S', help='start at this second of the music (default: the best downbeat)')
    music.add_argument('--end-on', choices=['phrase', 'downbeat', 'beat'], default='phrase',
                       help='where the final fade may begin (default phrase; falls back to finer points when none fits)')
    music.add_argument('--fade', type=float, metavar='S', help='wanted fade length (default about one bar, 1-3 s)')
    music.add_argument('--loop', action='store_true', help='loop on phrases even when the music is long enough')

    edit = add('edit', 'change an EDL by operations on clip ids (checked before writing)',
               'Each operation lays the whole edit out again and writes the file only if it is valid. Clips are addressed\n'
               'by "id" (rn edit e.json ids gives every clip one) or by position "track:index" / "audio:index".\n'
               'The main track plays back to back: deleting or trimming there ripples what follows; overlays and audio\n'
               'anchored with {"clip": "<id>"} follow, ones at fixed seconds stay. Examples:\n'
               '  rn edit e.json ids\n'
               '  rn edit e.json split c3 --at 4.2\n'
               '  rn edit e.json set c3 out=5.5 meta.take=2 --unset speed\n'
               '  rn edit e.json delete c4 --lift\n'
               '  rn edit e.json insert --after c2 --clip \'{"id": "c9", "src": "s9.mp4", "out": 2}\'\n'
               '  rn edit e.json move t1 --track 2\n'
               'A new edit from a reference\'s shots (one clip per shot, its transitions made again; sources not made yet are\n'
               'placeholders, filled with set <id> src=...):\n'
               '  rn shots ref.mp4 --keyframes kf --json shots.json && rn edit e.json init --from-shots shots.json\n'
               'One sound at many points (audio items anchored to each cut or beat; the file can come from rn assets get):\n'
               '  rn edit e.json hits assets/whoosh.wav --at cuts --pivot 0.18 --gain -6   # its peak on every cut\n'
               '  rn edit e.json hits hit.wav --at beats --listen music.json --every 4 --offset 2.0   # music starts at 2 s\n'
               '  rn edit e.json hits boom.wav --at 1.5,4.25 --id-prefix boom\n'
               'Long pauses of a talking clip cut out (from rn listen of its source):\n'
               '  rn edit e.json silences c1 --listen take.json --min 0.5 --pad 0.1\n'
               'Cuts onto the music (rolling edits; cuts with no beat near enough stay and are listed):\n'
               '  rn edit e.json snap --listen music.json --tolerance 0.15            # nearest beat\n'
               '  rn edit e.json snap --listen music.json --to downbeats --only c4,c7 --offset 0.5   # music starts at 0.5 s\n'
               'Several changes in one call, checked step by step and written once (a failing step writes nothing):\n'
               '  rn edit e.json batch \'set c2 transition.type=dissolve transition.duration=0.25\' \'set c1 out=3.2\'\n'
               "  rn edit e.json batch <<'EOF'   # one operation per line\n"
               '  split c3 --at 4.2\n'
               '  insert --after c2 --clip \'{"id": "c9", "src": "s9.mp4", "out": 2}\'\n'
               '  EOF')
    edit.add_argument('edl')
    ops = edit.add_subparsers(dest='op', required=True, metavar='op')
    _edit_ops(ops)
    op_init = ops.add_parser(
        'init', help='create the EDL from a reference\'s rn shots --json: one main-track clip per shot, its transitions made again',
        description='A new edit whose main track is the reference\'s shots, frame for frame: one clip per shot (s01, s02, ...)\n'
        'lasting what the shot lasts. Dissolves and wipes become xfade overlaps of their measured frames (dissolve: "fade",\n'
        'the smooth mix; wipe: wipeleft/right/up/down as measured), so the total is the reference\'s length; a fade through\n'
        'black or white becomes the clip before fading out and the one after fading in ("vf"). Without --src each source is\n'
        '<slots>/<id>.mp4, not made yet: rn assemble --placeholders / rn qa --placeholders show cards until it exists, and\n'
        'rn edit set <id> src=... fills a slot. With --src every clip is its shot of the reference (a rebuild to check).\n'
        'meta keeps each shot\'s times, transition, keyframe (rn shots --keyframes), camera and speed numbers.\n'
        '  rn shots ref.mp4 --keyframes kf --json shots.json && rn edit edit.json init --from-shots shots.json',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    op_init.add_argument('--from-shots', required=True, metavar='SHOTS.json', help='rn shots REF --json SHOTS.json')
    op_init.add_argument('--size', metavar='WxH', help='output size (default: the reference\'s)')
    op_init.add_argument('--fps', metavar='R', help='output frame rate (default: the reference\'s)')
    op_init.add_argument('--src', metavar='REF', help='cut every clip from the reference itself (the file SHOTS.json measured)')
    op_init.add_argument('--slots', default='gen', metavar='DIR', help='folder of the not-yet-made sources, relative to the EDL (default gen)')
    op_init.add_argument('--force', action='store_true', help='replace an existing EDL')
    op_batch = ops.add_parser('batch', help='several operations as one: all are checked, then written once (nothing on a failure)')
    op_batch.add_argument('steps', nargs='*', metavar='"OP ..."',
                          help='one operation per argument, or one per line on stdin (blank lines and # comments skipped)')

    compare = add('compare', 'two videos side by side at the same times (+ cut timing)',
                  'A cut against its reference, or two versions of a cut: frames at the same times, A on the top row and B\n'
                  'underneath, labelled with each one\'s own frame index. --cuts runs rn cuts on both and pairs the cuts\n'
                  '(within 0.25 s) with their offsets: the rhythm comparison. --shots runs rn shots on both and pairs each A\n'
                  'shot with the B shot that overlaps it most: transitions (cut, dissolve, fade, flash) and camera moves\n'
                  '(zoom/s, pan in frame widths/s, direction, rigid fit). --video writes A|B side by side to watch.\n'
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
    compare.add_argument('--shots', action='store_true',
                         help='rn shots on both: each A shot with the B shot that overlaps it most, their transitions and camera moves')
    compare.add_argument('--jobs', type=int, help='--shots: shots measured in parallel (default: every CPU allowed)')
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
    finish.add_argument('--height', type=int, metavar='N', help='scale to N px high, aspect kept (e.g. a 1080p copy of a 4K master)')
    finish.add_argument('--audio', metavar='FILE', help='take the sound from FILE instead (e.g. a separate WAV mix from the renderer)')
    finish.add_argument('--max-mbps', metavar='N|auto',
                        help='bitrate ceiling: clean shots keep --crf quality, grain and noise stop at N Mb/s (auto: 12 at 1080x1920 30fps, '
                             'scaled by pixels x fps); for upload limits and grain-heavy masters')
    finish.add_argument('--audio-offset', type=float, default=0.0, metavar='S',
                        help='move the sound S seconds later (negative: earlier), e.g. -0.0437 to undo an uncompensated AAC delay')

    qa = add('qa', 'delivery checks on an encoded file (exit 1 on FAIL)',
             'Frame count, fps, size, codec, pix_fmt, SAR, color tags, faststart, audio/video duration, container\n'
             'duration, true peak, loudness, dead air, level jumps, clicks, black frames, frozen frames, black bars,\n'
             'fixed frame rate. With --edl the expected frames,\n'
             'size, fps and loudness come from the edit, and its color/still clips are not flagged. FAIL blocks\n'
             'delivery; WARN means look at it (it may be intended). Check fixes on the new render, not the old one.')
    qa.add_argument('file')
    qa.add_argument('--edl')
    qa.add_argument('--placeholders', action='store_true', help='read --edl as rn assemble --placeholders does: missing sources are cards')
    qa.add_argument('--expect-frames', type=int)
    qa.add_argument('--expect-size', metavar='WxH')
    qa.add_argument('--expect-fps', metavar='R')
    qa.add_argument('--lufs', type=float, help='loudness target to check against')
    qa.add_argument('--lufs-tolerance', type=float, default=2.0)
    qa.add_argument('--true-peak', type=float, help='limit in dBTP (default -1.0)')
    qa.add_argument('--require-audio', action='store_true')
    qa.add_argument('--accept', action='append', default=[], metavar='CHECK[@T]',
                    help='a WARN judged intended: the whole check, or the finding at T seconds (EDL: "accept": [{"check", "at", "note"}])')

    add('doctor', 'what this machine can do with rn: ffmpeg, filters, extras, models, OCR, fonts (offline)',
        'Python, rn, ffmpeg / ffprobe (version, the H.264 / AAC encoders and every filter rn\'s graphs use), the Python\n'
        'modules of each extra, Playwright\'s Chromium, tesseract, fontconfig and the caption fonts, and the YuNet,\n'
        'Whisper and matte models. Each is ok, warn or missing, with the commands it affects and a one-line fix.\n'
        'Nothing is downloaded or fetched (the asset manifest URL is only reported). Exits 1 when the core is broken:\n'
        'Python, its required modules, ffmpeg, ffprobe or their encoders; missing extras only narrow what works.')

    schema = add('schema', 'print the edit.json JSON Schema (shipped with rn)',
                 'The JSON Schema (2020-12) of edit.json version 2, as shipped in the installed package: the contract for\n'
                 'other hosts. Fields marked "x-rn-only" are rn extensions. Rules the schema cannot state (anchors, unique\n'
                 'ids, transitions fitting their clips) are checked by rn assemble --dry-run.')
    schema.add_argument('--path', action='store_true', help='print where the schema file is instead')
    parser.epilog = _grouped_help(sub.choices)
    return parser


SHORT: dict[str, str] = {}
# rn --help: every command once, under the job it is for (tests/test_v7_agent.py fails when a command is missing).
GROUPS = (
    ('Look (you cannot watch or listen: these are your eyes and ears)', ('probe', 'frame', 'sheet', 'compare', 'look', 'signal', 'listen', 'text', 'faces')),
    ('Break down a reference', ('ref', 'shots', 'cuts', 'align')),
    ('Make pictures and sound', ('capture', 'template', 'captions', 'key', 'matte', 'grid', 'grade', 'music', 'pack', 'fx', 'fonts', 'assets')),
    ('Edit', ('edit', 'assemble')),
    ('Deliver', ('finish', 'qa', 'export')),
    ('This machine', ('doctor', 'schema')),
)
RECIPES = """recipes (each step prints proof: to look at and next: to run):
  remake a reference   rn shots ref.mp4 --keyframes kf --json shots.json && rn edit edit.json init --from-shots shots.json
                       fill each slot (rn edit edit.json set s03 src=gen/s03.mp4), rn assemble edit.json --placeholders --preview
  talking-head cut     rn listen take.mp4 --speech --json take.json && rn edit edit.json silences c1 --listen take.json
                       rn listen take.mp4 --transcribe --json words.json && rn captions words.json --style karaoke ...
  product / app ad     rn capture https://site -o site --sections "Pricing" --scroll-video scroll.mp4 --duration 8
                       rn template render callout -o call --params '{...}'; rn pack list --kind sfx
  music-driven edit    rn listen song.mp3 --json song.json && rn music fit song.mp3 --duration 28 -o bed.m4a
                       rn edit edit.json snap --listen song.json; rn edit edit.json hits whoosh/swish-1 --at cuts
  16:9 to 9:16         rn faces wide.mp4 --reframe 1080x1920 -o vertical.mp4 (then rn sheet vertical.mp4 --count 12)"""


def _grouped_help(commands) -> str:
    width = max(map(len, commands)) + 2
    out = []
    for title, names in GROUPS:
        out.append(f'{title}:')
        out += [f'  {name:<{width}}{SHORT.get(name, "")}' for name in names if name in commands]
    return '\n'.join(out) + '\n\n' + RECIPES + '\n\n' + EPILOG


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
                  + ('  (preview: draft size and encode, not for delivery)' if result.get('preview') else '')
                  + (f'  (range: timeline frames {result["range"]["start_frame"]}..{result["range"]["end_frame"] - 1})' if result.get('range') else ''))
            if result.get('audio'):
                audio = result['audio']
                print(f'audio {audio["lufs"]} LUFS, true peak {audio["true_peak"]} dBTP, gain {audio["gain_db"]} dB')
        else:
            print(f'timeline {out["width"]}x{out["height"]} {out["fps"]}fps  {out["frames"]} frames  {out["duration"]}s')
            for clip in result['base']:
                fade = f'  (xfade {clip["transition_frames"]}f)' if clip['transition_frames'] else ''
                print(f'  {clip["id"] or clip["where"]}: frames {clip["start_frame"]}..{clip["start_frame"] + clip["frames"] - 1}{fade}')
            for clip in result['overlays']:
                print(f'  {clip["id"] or clip["where"]}: overlay frames {clip["start_frame"]}..{clip["start_frame"] + clip["frames"] - 1}')
        for warning in result.get('warnings', []):
            print(f'warning: {warning}')
    elif command == 'finish':
        audio = result.get('audio')
        line = f'{result["path"]}  {result["frames"]} frames' + ('  (video copied)' if result['video_copied'] else '')
        if result.get('video_mbps') is not None:
            line += f'  video {result["video_mbps"]} Mb/s' + (f' (cap {result["max_mbps"]})' if result.get('max_mbps') else '')
        if audio:
            line += f'  audio {audio["lufs"]} LUFS, true peak {audio["true_peak"]} dBTP'
        print(line)
    elif command == 'qa':
        for item in result['checks']:
            if item['status'] != 'PASS':
                print(f'{item["status"]:4}  {item["check"]}: {item["detail"]}')
        print(f'qa {result["status"]}: {result["failed"]} failed, {result["warnings"]} warnings, '
              f'{len(result["checks"]) - result["failed"] - result["warnings"]} passed  ({result["path"]})')


PROOF_COMMANDS = {'key', 'matte', 'grid', 'template', 'capture', 'faces', 'music', 'grade', 'assets'}  # their output is a picture or sound
SUMMARIZED = {'look', 'grade', 'template', 'music', 'cuts', 'shots', 'listen', 'faces', 'key', 'grid', 'ref', 'capture', 'fx', 'fonts', 'assets',
              'captions', 'compare', 'edit', 'signal', 'align',
              'matte', 'text', 'export', 'doctor', 'schema', 'pack'}
NO_FFMPEG = {'doctor', 'schema', 'pack'}  # they work without ffmpeg on PATH: doctor reports it missing
# The pip extra that brings a command's optional Python modules (the media sandbox has them all).
EXTRAS = {'template': 'capture', 'listen': 'listen', 'music': 'listen', 'ref': 'listen', 'faces': 'faces', 'shots': 'shots', 'capture': 'capture', 'captions': 'captions', 'fonts': 'fonts',
          'signal': 'listen', 'matte': 'matte', 'export': 'export'}


def _size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split('x'))
    except ValueError:
        raise RnError(f'size "{text}": expected WxH like 1080x1920') from None
    return width, height


class _StepParser(argparse.ArgumentParser):
    """Parses one batch line; a bad line is an rn error naming the step, not an exit."""

    def error(self, message: str):
        raise RnError(message)


def _edit_step(path: Path, args) -> dict:
    from . import editops
    if args.op == 'ids':
        return editops.ids_op(path)
    if args.op == 'upgrade':
        return editops.upgrade_op(path)
    if args.op == 'set':
        return editops.set_op(path, args.ref, args.assignments, args.unset)
    if args.op == 'split':
        return editops.split_op(path, args.ref, args.at, args.id)
    if args.op == 'delete':
        return editops.delete_op(path, args.ref, args.lift)
    if args.op == 'insert':
        return editops.insert_op(path, args.clip, args.before, args.after, args.track)
    if args.op == 'hits':
        return editops.hits_op(path, args.sfx, args.at, listen=args.listen, offset=args.offset, pivot=args.pivot, gain=args.gain,
                               every=args.every, prefix=args.id_prefix)
    if args.op == 'silences':
        return editops.silences_op(path, args.ref, args.listen, min_silence=args.min, pad=args.pad, keep_gap=args.keep_gap)
    if args.op == 'snap':
        return editops.snap_op(path, args.listen, to=args.to, tolerance=args.tolerance, only=args.only, offset=args.offset)
    return editops.move_op(path, args.ref, args.before, args.after, args.track)


def _step_words(line: str, windows: bool = os.name == 'nt') -> list[str]:
    """One batch line as words, quotes grouping as in a shell. On Windows a backslash separates folders
    (C:\\clips\\a.mp4), so it is kept as written instead of escaping the next character."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split, lex.commenters = True, ''
    if windows:
        lex.escape = ''
    try:
        return list(lex)
    except ValueError as error:  # an unbalanced quote
        raise RnError(str(error)) from None


def _edit_step_runner():
    parser = _StepParser(prog='rn edit EDL batch', add_help=False)
    _edit_ops(parser.add_subparsers(dest='op', required=True, metavar='op'))

    def run(path: Path, line: str) -> dict:
        return _edit_step(path, parser.parse_args(_step_words(line)))
    return run


def _batch_lines(steps: list[str]) -> list[str]:
    if not steps:
        if sys.stdin.isatty():
            raise RnError('batch: give the operations as arguments, or one per line on stdin')
        steps = sys.stdin.read().splitlines()
    return [line.strip() for line in steps if line.strip() and not line.strip().startswith('#')]


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
    if command == 'shots':
        from .shots import shots_command, summary_lines
        result = shots_command(args.file, min_shot=args.min_shot, threshold=args.threshold, jobs=args.jobs, sheet=args.sheet,
                               max_kb=args.max_kb, faces=args.faces, face_model=args.face_model, beats=args.beats,
                               keyframes=args.keyframes, which=args.which)
    elif command == 'listen':
        from .listen import listen_command, speech_command, summary_lines
        if args.speech:
            result = speech_command(args.files, expect=args.expect, pause=args.pause, silence_db=args.silence_db, transcribe=args.transcribe,
                                    language=args.language, model=args.model, match_min=args.match_min)
        elif len(args.files) > 1:
            raise RnError('rn listen takes one file; several voice takes go with --speech')
        elif args.expect is not None:
            raise RnError('--expect goes with --speech')
        else:
            result = listen_command(args.files[0], out=args.out, transcribe=args.transcribe, language=args.language, model=args.model,
                                    max_kb=args.max_kb)
    elif command == 'faces':
        from .faces import faces_command, summary_lines
        result = faces_command(args.file, every=args.every, model=args.model, min_score=args.min_score, out_sheet=args.sheet, max_kb=args.max_kb,
                               track=args.track, pick=args.pick, smooth=args.smooth, max_gap=args.max_gap,
                               reframe_size=_size(args.reframe) if args.reframe else None, zoom=args.zoom, out=args.out)
    elif command == 'key':
        from .key import key_command, summary_lines
        result = key_command(args.file, out=args.out, color=args.color, white=args.white, tolerance=args.tolerance,
                             softness=args.softness, despill_edges=args.despill, cutout=args.cutout, every=args.every)
    elif command == 'grid':
        from .grid import grid_command, summary_lines
        result = grid_command(args.image, out=args.out, rows=args.rows, cols=args.cols, gap=args.gap, trim=args.trim)
    elif command == 'ref':
        from .ref import ref_command, summary_lines
        result = ref_command(args.file, out=args.out, every=args.every, transcribe=args.transcribe, language=args.language,
                             model=args.model, threshold=args.threshold)
    elif command == 'capture' and args.page.startswith(('http://', 'https://')):
        from .capture import web_command, web_summary_lines
        split = lambda text: [part.strip() for part in text.split(',') if part.strip()] if text else []  # noqa: E731
        result = web_command(args.page, out=args.out, device=args.device or 'mobile', size=_size(args.size) if args.size else None,
                             full=args.full, sections=split(args.sections), scroll_video=args.scroll_video,
                             duration=args.duration, fps=args.fps, hide=split(args.hide))
        return result, web_summary_lines(result)
    elif command == 'capture':
        from .capture import capture_command, summary_lines
        if args.frames is None:
            raise RnError('rn capture PAGE.html needs --frames N (or give an http(s) URL to shoot a live site)')
        if args.device or args.full or args.sections or args.scroll_video or args.hide:
            raise RnError('--mobile/--desktop/--full/--sections/--scroll-video/--hide are for an http(s) URL')
        width, height = _size(args.size or '1080x1920')
        result = capture_command(args.page, out=args.out, frames=args.frames, width=width, height=height, fps=args.fps,
                                 shards=args.shards, start=args.start)
    elif command == 'template':
        from .template import summary_lines, template_command
        width, height = _size(args.size)
        result = template_command(args.action, args.name, out=args.out, width=width, height=height, fps=args.fps,
                                  duration=args.duration, params=args.params, shards=args.shards)
    elif command == 'fx':
        from .fx import catalog, js_path
        result = {'effects': catalog(), 'js': str(js_path()), 'py': str(js_path().with_name('__init__.py'))}
        lines = [f'{e["name"]:10} {e["js"]:16} {e["py"]:18} {e["what"]}' for e in result['effects']]
        return result, lines + ([f'source: {result["js"]}', f'        {result["py"]}'] if args.source else
                                ['rn fx --source prints the files to read or copy'])
    elif command == 'edit':
        from . import editops
        path = Path(args.edl)
        if args.op == 'batch':
            result = editops.batch_op(path, _batch_lines(args.steps), _edit_step_runner())
        elif args.op == 'init':
            result = editops.init_op(path, args.from_shots, size=args.size, fps=args.fps, src=args.src, slots=args.slots, force=args.force)
        else:
            result = _edit_step(path, args)
        return result, editops.summary_lines(result)
    elif command == 'music':
        from .music import fit_command, summary_lines
        result = fit_command(args.file, duration=args.duration, out=args.out, listen=args.listen, start=args.start,
                             end_on=args.end_on, fade=args.fade, loop=True if args.loop else None)
    elif command == 'signal':
        from .signal import signal_command, summary_lines
        result = signal_command(args.file, out=args.out)
    elif command == 'align':
        from .align import align_command, summary_lines
        result = align_command(args.a, args.b, max_offset=args.max_offset)
    elif command == 'matte':
        from .matte import matte_command, summary_lines
        result = matte_command(args.file, out=args.out, model=args.model, every=args.every, cutout=args.cutout)
    elif command == 'text':
        from .text import summary_lines, text_command
        result = text_command(args.file, every=args.every, lang=args.lang, min_conf=args.min_conf)
    elif command == 'look':
        from .look import look_command, summary_lines
        result = look_command(args.files, refs=args.ref, seconds=args.seconds, count=args.count, shots=args.shots, tolerance=args.tolerance)
    elif command == 'grade':
        from .agent import proof_path
        from .look import grade_command, grade_summary_lines
        result = grade_command(args.src, args.ref, out=args.out, strength=args.strength, size=args.size, count=args.count,
                               proof=None if args.no_proof else str(proof_path(args.out)),
                               preview=args.preview)
        return result, grade_summary_lines(result)
    elif command == 'export':
        from .export import export_command, summary_lines
        result = export_command(args.edl, args.out, srt=args.srt)
    elif command == 'compare':
        from .compare import compare_command, summary_lines
        result = compare_command(args.a, args.b, out=args.out, count=args.count, seconds=args.seconds, width=args.width,
                                 max_kb=args.max_kb, cuts=args.cuts, video=args.video, shots=args.shots, jobs=args.jobs)
    elif command == 'doctor':
        from .doctor import doctor_command, summary_lines
        result = doctor_command()
    elif command == 'schema':
        from .edl import SCHEMA
        text = SCHEMA.read_text(encoding='utf-8')
        return json.loads(text), [str(SCHEMA) if args.path else text.rstrip('\n')]
    elif command == 'fonts':
        from .fonts import fonts_command, summary_lines
        result = fonts_command(family=args.family, script=args.script, licenses=args.licenses)
    elif command == 'pack':
        from . import pack
        if args.action == 'list':
            result = pack.list_command(kind=args.kind, category=args.category, search=args.search, limit=args.limit)
        elif args.action == 'show':
            result = pack.show_command(args.ids)
        elif args.action == 'path':
            result = pack.path_command(args.ids)
        else:
            result = pack.get_command(args.ids, args.out, color=args.color, stroke_width=args.stroke_width)
        return result, pack.summary_lines(result)
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
                                  max_chars=args.max_chars, max_lines=args.max_lines, safe=args.safe, language=args.language,
                                  style=args.style, font=args.font, color=args.color, accent=args.accent, position=args.position,
                                  upper=args.upper)
    return result, summary_lines(result)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == 'win32' and not os.environ.get('PYTHONIOENCODING'):
        # A pipe (an agent reading rn) would get the ANSI code page, which cannot hold every file name or caption.
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
    args = _parser().parse_args(argv)
    for tool in ('ffmpeg', 'ffprobe'):
        if args.command not in NO_FFMPEG and not shutil.which(tool):
            print(f'rn: error: {tool} not found on PATH', file=sys.stderr)
            return 2
    lines: list[str] = []
    from .agent import after, warn_if_long
    estimate_s = warn_if_long(args)
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
                                   around=args.around, span=args.span, diff=args.diff, crop=args.crop)
        elif args.command in SUMMARIZED:
            result, lines = _run_summarized(args)
        elif args.command == 'assemble':
            from .assemble import assemble_command
            result = assemble_command(args.edl, args.out, dry_run=args.dry_run, keep=args.keep, preview=args.preview,
                                      cache=not args.no_cache, placeholders=args.placeholders, time_range=args.range)
        elif args.command == 'finish':
            from .assemble import finish_command
            lufs = None if args.lufs == 'keep' else float(args.lufs)
            result = finish_command(args.file, args.out, lufs=lufs, true_peak=args.true_peak, crf=args.crf,
                                    preset=args.preset, copy_video=args.copy_video, height=args.height, audio=args.audio,
                                    audio_offset=args.audio_offset, max_mbps=args.max_mbps)
        else:
            from .qa import parse_accept, qa_command
            result = qa_command(args.file, edl=args.edl, placeholders=args.placeholders, frames=args.expect_frames, size=args.expect_size, fps=args.expect_fps,
                                lufs=args.lufs, lufs_tolerance=args.lufs_tolerance, true_peak=args.true_peak,
                                require_audio=args.require_audio, accept=parse_accept(args.accept))
    except RnError as error:
        print(f'rn: error: {error}', file=sys.stderr)
        return 1
    except ValueError as error:
        print(f'rn: error: {error}', file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        extra = EXTRAS.get(args.command, 'all')
        print(f"rn: error: rn {args.command} needs the Python module {error.name} (extra \"{extra}\"): "
              'uv tool install "renoise-kit[all]"', file=sys.stderr)
        return 1
    except Exception as error:  # a kit bug: keep the traceback for the report, end on the usual one-liner
        import traceback
        traceback.print_exc()
        print(f'rn: error: unexpected {type(error).__name__}: {error} (a bug in rn; work around it with ffmpeg directly)', file=sys.stderr)
        return 1
    tail = [] if args.command in ('probe', 'schema') else after(args, result, estimate_s)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    if args.command in SUMMARIZED:
        print('\n'.join(lines))
    else:
        _print_summary(args.command, result)
    if tail:
        print('\n'.join(tail))
    if args.command == 'doctor':
        return 0 if result['ok'] else 1
    return 1 if args.command == 'qa' and result['status'] == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
