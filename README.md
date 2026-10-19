# renoise-kit · `rn`

**English** · [简体中文](README.zh-CN.md)

The mechanical, error-prone half of editing video, as one command-line tool: frame-exact probing, contact sheets you
can actually look at, audio turned into numbers, code rendered into frames, an edit decision list (EDL) assembled
frame-exactly, delivery finishing, QA and export to other editors.

`rn` measures and guarantees the mechanics. Every creative decision stays yours, or your agent's. It was built for AI
agents editing generated video in the [Renoise](https://renoise.ai) media sandbox, and the same tool runs on your
machine.

- **Measure.** Turns media into facts a model can read: frames, shots, beats, word times, faces, on-screen text,
  per-frame signals.
- **Render.** Turns any code into frame-exact pictures, alpha included: HTML/canvas pages, numpy, Manim, Blender. They
  all meet in one format, a PNG sequence.
- **Assemble and verify.** Builds a frame-exact cut from an EDL, then checks it against the delivery standard.
- **No named effects, no auto-editing.** The EDL adds general capabilities (keyframes, anchors, masks, blend modes, raw
  ffmpeg filters), never a menu of looks. A common effect comes as readable source you can call, change or ignore.

## Install

`rn` drives the system's ffmpeg and ffprobe, version 7 or later, built with libass for burned-in subtitles.

| | macOS | Debian / Ubuntu | Windows 10 / 11 |
|---|---|---|---|
| ffmpeg with libass | `brew install ffmpeg-full`, then `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"` (keg-only; Homebrew's plain `ffmpeg` has no libass) | `apt install ffmpeg fontconfig` | `winget install Gyan.FFmpeg` (the full build, libass and fontconfig included), then open a new terminal |
| OCR for `rn text` (optional) | comes with `ffmpeg-full` | `apt install tesseract-ocr tesseract-ocr-chi-sim` | `winget install UB-Mannheim.TesseractOCR`, then add `C:\Program Files\Tesseract-OCR` to `PATH` |

```sh
uv tool install "renoise-kit[all]"     # or: pipx install "renoise-kit[all]"
playwright install chromium            # only for rn capture
rn --version
```

The same commands work in a POSIX shell, PowerShell and cmd (hence the double quotes). On Windows `rn fonts` reads the
system font folders directly when fontconfig is not installed, and `rn` writes UTF-8 when its output goes to a pipe.

The core install is Pillow and numpy. Heavier dependencies come as extras, so you can install only what you use. When
a command needs a missing extra, its error line names the extra to install.

| Extra | For | Installs |
|---|---|---|
| `listen` | `rn listen`, `rn ref`, `rn signal -o` | librosa, matplotlib |
| `transcribe` | `rn listen --transcribe` | faster-whisper |
| `faces` | `rn faces` | OpenCV (headless); the YuNet model is fetched once into `~/.cache/rn` and checked against its sha256 |
| `capture` | `rn capture` | Playwright |
| `captions` | `rn captions` | pysubs2 |
| `fonts` | `rn fonts --licenses` | fontTools |
| `matte` | `rn matte` | rembg (onnxruntime); models download on first use |
| `export` | `rn export` | OpenTimelineIO and its Final Cut / FCP 7 adapters |
| `all` | everything above | |

## A first cut

Starting from two generated shots, a music bed, a whoosh and an end card:

```sh
rn sheet shots/s2.mp4 --fps 4                    # look at a take: labelled frames, small enough to view
rn listen music.wav --json music.json            # loudness, beats, tempo
rn capture title.html -o title --frames 90 --size 1080x300   # an HTML title, frame by frame, transparent
```

`title.html` draws frame `i` when asked. The frame number is the time, so any frame order or number of shards gives
the same pixels:

```html
<canvas id=c></canvas><script>
const x = c.getContext('2d'); c.width = RN.width; c.height = RN.height;
window.renderFrame = i => {
  x.clearRect(0, 0, c.width, c.height);
  x.globalAlpha = Math.min(1, i / 10);
  x.fillStyle = '#fff'; x.font = '700 120px sans-serif'; x.textAlign = 'center';
  x.fillText('RENOISE', c.width / 2, 160);
};</script>
```

`edit.json` puts it together:

```json
{
  "version": 2,
  "output": {"width": 1080, "height": 1920, "fps": 30},
  "tracks": [
    {"clips": [
      {"id": "open", "src": "shots/s1.mp4", "in": 0.4, "out": 3.2},
      {"id": "hero", "src": "shots/s2.mp4", "ranges": [[0, 1.5], [2.5, 4]],
       "transition": {"type": "fade", "duration": 0.3}},
      {"id": "end", "image": "card.png", "duration": 1.5, "style": {"object-fit": "contain"}}
    ]},
    {"clips": [
      {"id": "title", "frames": "title/%05d.png", "fps": 30, "style": {"top": 1500},
       "at": {"clip": "hero", "offset": 0.2}, "until": {"clip_end": "hero"}}
    ]}
  ],
  "audio": [
    {"src": "music.wav", "role": "music", "pivot": 12.0, "at": {"clip": "hero"}, "fade_out": 1},
    {"src": "whoosh.wav", "role": "sfx", "pivot": 0.18, "at": {"clip": "hero"}}
  ],
  "mix": {"lufs": -14, "cut_fade": 0.01}
}
```

What each part does:

- **Main track.** It plays back to back. `hero` plays two spans of one take, dissolving in from `open`.
- **Title.** It follows the `hero` clip by id rather than by hand-computed seconds. If `open` gets longer, the title
  moves with it.
- **Music.** `pivot` lands the music's 12 s point (say, the drop) on the cut into `hero`, and the head of the track is
  trimmed away.
- **Whoosh.** Its peak lands on the same cut.

```sh
rn assemble edit.json --dry-run                  # the laid-out timeline, frame by frame, nothing rendered
rn assemble edit.json -o draft.mp4 --preview     # half-size draft, about 3x faster
rn assemble edit.json -o cut.mp4                 # full render; unchanged clips are reused from .rn-cache
rn qa cut.mp4 --edl edit.json                    # exit 1 on FAIL; WARN means look at it
```

```text
timeline 1080x1920 30fps  210 frames  7.0s
  open: frames 0..83
  hero: frames 75..119  (xfade 9f)
  hero.1: frames 120..164
  end: frames 165..209
  title: overlay frames 81..164
...
WARN  level_jumps: 6.0s -40->-13 dB, 6.5s -45->-19 dB
qa PASS: 0 failed, 1 warnings, 17 passed  (cut.mp4)
```

A WARN may be intended. Here the jumps are the music's own hard-hitting beats. You can waive it in the EDL with
`"accept": [{"check": "level_jumps", "note": "the music's hits"}]`, or with `rn qa --accept level_jumps`. A FAIL can
never be waived.

Make further changes by operation instead of rewriting the JSON:

```sh
rn edit edit.json split open --at 1.5
rn edit edit.json set title meta.copy="new tagline"
rn edit edit.json delete end --lift
```

## Commands

`rn <command> --help` is the reference. Every command prints a short summary, and `--json FILE` writes the full result.

**Measure**

| Command | What it gives |
|---|---|
| `rn probe FILE` | Decoded frame count, rational fps, size, color tags, separate video and audio durations, faststart (JSON) |
| `rn frame FILE --index N \| --time T \| --last -o out.png` | One exact frame, e.g. a tail frame to continue a shot |
| `rn sheet FILE... [--fps R \| --every N \| --indices ... \| --around T,.. --diff]` | Contact sheets labelled with frame number and timecode, each under 125 KB |
| `rn cuts FILE [--sheet JPG]` | Shot boundaries by decoded frame, plus weaker "suspects" to check |
| `rn listen FILE [--transcribe]` | Loudness, silences, onsets, tempo with half/double candidates, beat times, Whisper word times, one picture |
| `rn signal FILE [-o PNG]` | Per-frame luma, motion, saturation, loudness, low/mid/high bands, onset strength; flash frames, clicks |
| `rn align A B` | Where recording B sits inside A, by sound: offset, `match`, `distinct` |
| `rn faces FILE` | Face boxes and landmarks per frame (measurement only) |
| `rn text FILE` | On-screen text lines, their boxes and when they are on screen (tesseract) |
| `rn matte FILE -o DIR` | Subject alpha per frame as grey PNGs, ready to use as an EDL `mask`; `--cutout` for RGBA |
| `rn ref FILE` | A reference breakdown: shots, sound, cut-to-beat sync, every frame on sheets, and a reading guide |

**Render**

| Command | What it gives |
|---|---|
| `rn capture page.html -o DIR --frames N --size WxH` | An HTML page rendered frame by frame (`window.renderFrame(i)`) to RGBA PNGs, sharded across Chromiums |
| `rn fx [--source]` | The effects library (grain, vignette, leak, flash, glitch, ...) as readable source for pages and numpy |
| `rn captions WORDS.json --size WxH -o caps.ass` | Your script aligned to Whisper timing; CJK chunking, balanced lines, safe areas, kept off faces |
| `rn fonts [--script ja]` | Installed families with the weights that really exist, and the scripts they cover |
| `rn assets search\|get` | Licensed SFX and overlay footage, checked against sha256 |

**Edit and assemble**

| Command | What it gives |
|---|---|
| `rn edit EDL ids\|set\|split\|delete\|insert\|move\|upgrade` | Changes an EDL by clip id. Each step is checked against the whole timeline before the file is written |
| `rn assemble EDL -o OUT [--preview] [--range A-B] [--placeholders]` | Renders the EDL frame-exactly. `--placeholders` turns missing shots into labelled cards |

**Check and deliver**

| Command | What it gives |
|---|---|
| `rn qa FILE [--edl EDL] [--accept CHECK[@T]]` | Delivery checks. Format: frames, fps, size, codec, color, faststart, fixed frame rate. Sound: A/V length, loudness, true peak, dead air, level jumps, clicks. Picture: black, frozen, bars |
| `rn compare A B [--cuts] [--video sbs.mp4]` | Two videos at the same times on one sheet, cut timing paired, optional side-by-side video |
| `rn finish FILE -o OUT [--height N]` | Brings any video to the delivery standard; `--height` makes a scaled copy |
| `rn export EDL -o cut.otio\|.fcpxml\|.xml [--srt]` | The edit as a project for Final Cut, Premiere, Resolve or any OTIO tool |

## The EDL in one screen

- **Version.** `"version": 2`. A file without it uses the version-1 names (`x`, `y`, `w`, `h`, `fit`, `blend`) and is
  upgraded on load; `rn edit FILE upgrade` rewrites it.
- **Tracks.** `tracks[0]` is the main track: its clips play back to back. Later tracks are overlays, placed with `at`
  in time and with `style` in space.
- **Style.** Placement and look under CSS names and units: `left`, `top`, `width`, `height` (px, `"90px"`, or `"50%"`
  of the frame), `opacity`, `scale`, `rotate` (degrees), `object-fit` (`cover`, `contain`, `fill`) and
  `mix-blend-mode` (the CSS modes). The main track fills the frame, so it takes only `object-fit`, `left`, `top`,
  `scale` and `rotate`.
- **Audio items.** Items in `audio` are placed with `at`. `role` is `music`, `voice` or `sfx`; music ducks under voice
  when `mix.duck_db` is set.
- **One time grid.** `output.fps` is the only clock. A clip of N seconds is exactly `round(N × fps)` frames.
- **Sources.** A clip's source is one of: `src` (video, or another `.json` EDL to nest), `frames` (a numbered PNG
  sequence, alpha kept), `image`, or `color`.
- **Positions inside a source.** `in` and `out` take seconds, `{"frame": n}` or `{"shot": n}` (a shot found by
  `rn cuts`). `ranges` plays several spans of one source in a row.
- **Placing on the timeline.** `at` and `until` take seconds or an anchor plus `offset`:
  - `{"clip": "id"}` and `{"clip_end": "id"}`
  - `{"marker": name}`
  - `{"beat": n, "listen": file}` and `{"word": n, "listen": file}`

  `pivot` places a point inside the source on the anchor instead of the source's start.
- **Keys.** Written as `[t, value, ease]`: the moving form of a style value. Overlays animate `scale`, `left`, `top`,
  `rotate`, `opacity` and `speed`; the main track `scale`, `left`, `top`, `rotate`, `speed` and `gain_db`; audio items
  `gain_db`.
- **Compositing.** `mask` takes a full-frame matte, as luma or alpha, optionally inverted. `vf` and `af` insert one raw
  ffmpeg filter chain.
- **Bookkeeping.** `id` and `meta` go on anything; `meta` is kept as given. `accept` waives qa warnings judged
  intended.

The full schema is [schema/edit.schema.json](schema/edit.schema.json), with `rn assemble --help` and [SPEC.md](SPEC.md)
§4. Fields it marks `x-rn-only` (raw filters, masks, image sequences, nested edits, beat and word anchors, delivery
loudness, subtitles) are rn's own; another program that renders the rest of the format previews what rn delivers.

## Conventions

- **Output.** A few summary lines on stdout. `--json FILE` holds the details. `rn probe` prints JSON directly.
- **Errors.** A failure exits non-zero. Its last line is `rn: error: <what to fix>`, written so you can relay it as is.
  If ffmpeg is missing, the exit code is 2. `rn qa` exits 1 on FAIL.
- **Frame exactness.** Frames are picked and cut by decoded index or decode timestamp, never by input seeking. Every
  render is checked against the expected frame count.
- **Viewing budget.** Sheets default to 125 KB, so four fit in one 512 KB look.
- **Cache.** Rendered slots and nested edits are stored by content in `.rn-cache/` next to the EDL. Moving or
  renaming a clip reuses its render. `--no-cache` renders everything afresh. Delete the folder to reclaim space.
- **Environment variables.**
  - `RN_CACHE`: cache folder
  - `RN_YUNET_MODEL`: face model file
  - `RN_ASSETS_URL` / `RN_ASSETS_CACHE`: asset library
  - `U2NET_HOME`: rembg models

## Delivery standard

`rn assemble` and `rn finish` produce this standard, and `rn qa` checks it:

- **Video.** H.264 High, yuv420p, BT.709 limited range (converted, not just tagged), square pixels, `moov` first. The
  SPS sets `fixed_frame_rate_flag=1`; with 0, some players show a 30 fps video at half its length.
- **Audio.** AAC 48 kHz stereo at −14 LUFS (configurable), with the true peak measured on the **encoded** AAC at or
  below −1 dBTP.
- **Length.** Audio is cut to the video by sample, so the container is never longer than the picture.

## With agents

`rn` is designed to be called by a model. `--help` is the documentation and loads only when needed. One sandbox call
can chain several commands. Summaries are short, and errors say what to change. Sheets, `listen`, `signal` and `align`
exist because a model cannot watch or listen; check the result through them rather than through the format alone.
`rn qa` passing does not mean the edit is good.

In the Renoise media sandbox, `rn` comes preinstalled at the version pinned by the sandbox template.

## Development

```sh
uv run pytest -q    # Python 3.12 (as the sandbox); needs ffmpeg with libass; playwright install chromium for capture tests
```

CI runs on Debian trixie with ffmpeg 7.1 (the same base as the sandbox), on macOS with ffmpeg-full and on Windows with
gyan.dev's full build (with Windows' own default encoding, so a file read without `encoding='utf-8'` fails there). The
tests generate their own media.

To release:

1. Bump `__version__` in `rn/__init__.py`.
2. Tag the commit:

   ```sh
   git tag v$(sed -n "s/^__version__ = '\(.*\)'/\1/p" rn/__init__.py) && git push --tags
   ```

`.github/workflows/release.yml` then publishes the tag to PyPI through trusted publishing.

Design notes, the full EDL contract and the roadmap are in [SPEC.md](SPEC.md) (in Chinese).

## License

MIT
