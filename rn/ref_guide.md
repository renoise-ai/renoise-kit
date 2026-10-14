# Reading a reference edit

`rn ref` measured this reference: `ref.json` (probe, shots, cuts, suspects, listen, cut_sync, coverage), `cuts.jpg`
(both sides of every cut and suspect), `frames*.jpg` (the coverage pages) and `listen.jpg`. This guide is how to turn
that into `breakdown.md` in this folder. Reading costs no credits and decides most of what gets spent later, so do it
fully before proposing any generation.

**The sheets are the truth; the detector only proposes.** On whip edits, strobe edits and dark or grainy footage a
scene detector routinely finds well under half the real cuts, and on boiling grain it fires every couple of frames.
Every number you write comes from a frame you looked at.

## 1. Order of work

1. `cuts.jpg`: confirm or reject every cut and suspect. Merged near-duplicates are listed as suspects on purpose.
2. Overview: look at a few pages first and write a draft of the trick line (§3).
3. Every coverage page, in order, first frame to last (§2). Keep the event log (§2.3) as you go.
4. Zoom into anything unclear (§2.2). Never settle an ambiguity from a small tile.
5. Sound: `listen.jpg` and `ref.json` `listen` / `cut_sync` (§5). Re-run `rn ref --transcribe` if words matter.
6. People, text, look (§4, §6), then meaning and roles (§7).
7. Write `breakdown.md` (§8), check it against §10, and tell the user what you found (§9).

With `--every 1` (the default) the coverage pages hold every decoded frame. If you used `--every N`, the gaps are not
inspected: fill them with `rn sheet ref.mp4 --indices ...` before calling the read complete.

## 2. Reading the pages

### 2.1 Budget and labels
- One `image_paths` view takes at most 4 images and 512 KB in total. `rn` pages are under 125 KB, so four per call.
  Check sizes on anything you made yourself; split a batch rather than skip a page.
- Tiles are labelled with the absolute frame index and timecode. If you build a sheet with ffmpeg yourself, put
  `drawtext ... %{n}` before `select`, or every page restarts at 0 and every frame number you log is wrong.
- Select frames by index (`rn frame --index`, `rn sheet --indices`), never with ffmpeg's input `-ss`: it lands a
  frame or two off.

### 2.2 Zooming in
- `rn sheet ref.mp4 --indices 35,36,37,38,39,40 --width 540 --columns 3`: consecutive frames, large.
- `rn sheet ref.mp4 --around 3.37 --span 4`: both sides of a cut, beat or transition.
- `rn sheet ref.mp4 --indices ... --diff`: frame-to-frame difference; shows held frames, flicker and hidden cuts.
- `rn frame ref.mp4 --index 52 -o f52.png`: one frame at native pixels for text, texture and edges.

### 2.3 Event log
One line per event, by frame, saved as `events.txt` next to the breakdown so later turns and other agents can reuse it:

```
f0-2    white flash in, product already centred
f3      CUT  close-up of the label, slow push-in starts
f11-14  price tag slides in from the right, lands f14 with an overshoot
f18     CUT hidden in a 3-frame blur pulse (f17-19); new angle, same table
f26-27  1-frame black insert at f26, NOT a new shot
```

### 2.4 Coverage
- Record each range you actually viewed as `[first, last]`. `ref.json` `coverage.page_frames` lists each page's
  range. Their union must cover 0 to frames-1 with no gaps. A page that was generated but not looked at does not count.
- Also keep one line per second `[k, k+1)`, including the final partial second: what changes in it (cut, text,
  effect, camera, sound), or "no change".
- If something stops you from finishing (time, a broken file), name the ranges you did not inspect. Never call a
  partial read complete.

## 3. The trick line (write it first)

After the overview, before per-shot work, write one sentence:

> **The trick of this reference is** <the device> **on** <what timing or structure> **so that** <what the viewer
> feels or gets>.

List the frames where it happens. Refine it after the full read, but keep it one sentence. Tests:
1. Could a stranger rebuild the idea from the sentence alone?
2. Does it say what changes over time, not only how it looks?
3. Would a remake be obviously wrong without it?

Copying the timing while losing the device is a failed remake; so is copying the device without serving the new
subject. If there is no device beyond "fast cuts on the beat", say so: the trick is then the rhythm plus the look.

The trick tells you how each shot should be made, before any model is chosen:

| What the reference is | Make it with |
|---|---|
| New people or worlds under styled graphics | generated clips + graphics in the sandbox |
| The user's own footage, re-cut | the sandbox only, no generation |
| A montage of real events or people | the user's or licensed footage + sandbox; don't fake real events |
| A photo series / stop motion: locked camera, one detail changing every 1-2 frames | stills + code, not video |
| Typography and shapes only | code (`rn capture`), no generation |
| The same edit with the user's product or person swapped in | per shot: reuse, still + code, or generate |

Mixed references are normal: pick the dominant kind, then decide per shot.

## 4. What to look for on every page

### 4.1 Cuts the detector misses
A cut is the first frame of the new shot. Flash, blur and burn frames belong to the shot they sit in.

| Kind | On the sheet |
|---|---|
| Whip / smear | 2-3 smeared frames; the content behind the smear changes mid-way; often straddles the beat |
| Hidden in a transition | burn, blur, mosaic or flash frames with a different pose or framing on each side |
| Dark | both sides near black; only shapes change |
| Same-tone jump cut | same place and palette, different pose or framing |
| 1-2 frame insert | one foreign frame: black, white, a colour, another shot, a ghost blend |
| Strobe | the picture alternates every 1-3 frames |
| Micro-cut burst | a run of tiny shots on a 4-6 frame grid: log it as one shot with the sub-shots in notes |

### 4.2 False cuts the detector adds
Grain or photocopy boil, flash frames, exposure flashes, text or card pops, light flicker, camera shake, letters
flying out.

### 4.3 Transitions
Name each with its frames and say whether it hides a cut: hard cut, whip, overexposure burn, white or black frames,
colour flash, mosaic, glitch, blur pulse, dissolve, invert, zoom punch, slide, wipe, split strips, mirror, match cut.
**Ink / matte wipes** are often mislogged as a smear and rebuilt as a blur: both pictures stay sharp, a ragged or
organic edge (brush, ink, torn paper, smoke) travels across, and mid-wipe one frame holds both shots at full contrast.
A dissolve shows both everywhere at reduced contrast instead.

### 4.4 Text
For every word or block: first frame, first fully-on frame, start of exit, last frame; the animation (fade length in
frames, pop, typewriter, slam, write-on, smear-out); whether the subject passes in front; whether it crosses a cut.
Measure on native frames (`rn frame`): fades read from small tiles are often 1-2 frames off.
- Box and size: the tight box of the letters, size as cap height / frame height, centre as fractions of the frame.
- Colour from the letter core, not the edge; note stroke, shadow, glow.
- Font: describe the class (condensed grotesk caps, didone serif, bold italic sans...) and pick the nearest family
  that `rn fonts` shows with the weight really installed. Don't claim to have identified the exact font.

### 4.5 Layers (everything that is not the footage)
Outlines and halos around the subject, panels and picture-in-picture, shapes, stickers and doodles, borders,
windows, letterbox, watermark, light leaks, particles, HUD. For each: under or over the subject, and whether it is
static or follows the subject, the face or the camera. **Count stickers and doodles per second and note their size
relative to the subject**: density and scale are what viewers notice when a remake looks thinner than the reference.

### 4.6 Look
- **Clean**: natural colour, no visible grain, no vignette, no cast beyond the scene's light. Add nothing.
- **Stylised**: B/W, threshold, duotone, one accent colour, visible or boiling grain (size, boil rate), halation,
  light leaks, a hard curve, one cast over everything. Measure it on native crops of the darkest, brightest, skin and
  text frames, plus every other look state the per-second log shows.
- Generated clips usually come back less saturated than stock footage: compare against the reference, don't assume.

### 4.7 Camera
Separate moves in the footage (pan, tilt, push, orbit, handheld) from moves added in the edit (zoom punch, eased
push-in, shake, mirror, rotation). Edit moves are rebuilt in the sandbox at the output fps; don't also ask a video
model for them, or the move doubles.

### 4.8 Cadence and speed
Count unique frames per shot (`--diff` shows held frames as black tiles): native, 24p in 30p (every 5th repeated),
2:3 pulldown, stepped (e.g. ~18 unique fps in a 30p file), interpolated slow motion, frame-blend retime, freeze,
speed ramp (note frame and speed). Generated clips come at their model's fps (`rn probe`): a smooth 60p reference
shot rebuilt from a 24 fps clip loses images that code cannot bring back, and a stepped reference rebuilt smooth
looks wrong too. Note both in the plan.

## 5. Sound

`rn listen` measures; it cannot hear. Treat its numbers as facts and its tempo as a guess.
- **BPM check**: express the shot lengths in beats at the detected BPM and at x2, x1/2, x3/4 and x4/3. The right
  tempo puts most of them near whole or half beats. On chants or VO, cross-check with word starts. Write down the
  check, not just the number.
- **Phase**: do cuts lead the beat (`cut_sync.offsets_frames` mostly negative) or lag it?
- **Cut on the sound**: `cut_sync.share` is the share of cuts within 2 frames of an onset. Around 0.7 or more, the
  edit is cut on the sound and the remake's cuts must follow its own track the same way. Low: the cuts follow the
  picture or the words.
- **Hidden music edits**: a sudden change in energy or timbre off the bar grid, a section starting mid-phrase, a
  skipped or doubled beat. Note them as suspected splices; they matter if the user swaps the track.
- **Sound effects**: whooshes, booms, risers, shutters, ducks under VO, with times. They are part of the edit.
- **Words**: Whisper's word times run late on TTS and merge chanted words; check them against the loudness curve.
- **Loudness**: integrated LUFS and true peak, as the QA reference.

## 6. People in each shot

For every shot: how visible the person is (`face clear`, `face partial`, `body only` for backs, helmets, hands or
silhouettes, `none` when nobody is there), the shot size (ECU, CU, MCU, MS, MWS, WS, EWS, insert, graphic), who it
is when there are several, and who must stay themselves. A hidden face does not exempt a shot from a requested
person or product swap: continuity across the back view and the close-up is part of the job.

## 7. Meaning and roles

- For every graphic and text event, write what it means relative to the words, the beat or the story, and what it
  becomes in the remake. A remake that keeps the shapes but loses the meaning reads as random graphics: lyrics on
  their syllables are the song's words, not decoration; a sentence spread across shots breaks if one word is lost.
- Give each shot a role from what you saw: hook, identity, action, accent, reveal, consequence, transition, breath,
  payoff, closure. Don't invent a plot a non-narrative reference doesn't have.
- **Missing-role inventory**: for each role the new video needs, what the user already supplied that you actually
  looked at, what is missing, and how to fill it. Generate only what is missing, not a fresh version of every shot.
  A photo gives identity and design; it does not contain an action it doesn't show.
- When the user's subject differs from the reference's (a nation vs one player, a range vs one product), map the
  reference's sections onto the user's subject first; don't keep the structure and give the subject a corner of it.

## 8. breakdown.md

```markdown
# <reference name>: breakdown
Source: <path>, <w>x<h>, <fps as a fraction>, <frames> frames, video <s> s, audio <s> s
Trick: The trick of this reference is ... (frames: ...)
Made with: <the §3 row, then per-shot exceptions>
Look: clean | stylised: <grain, grade, cast, measured on frames ...>
Palette: <hex - role>, ...
Fonts: <class -> nearest installed family/weight>, ...
Sound: <bpm and how it was checked>, cut on sound <share>, phase <lead/lag>, splices <t or none>,
       sfx <t type>, speech <none/vo/lyrics/chant>, <LUFS> LUFS / <dBTP> dBTP
Coverage: inspected [0, N-1] in ranges <...>; per-second log in events.txt

| shot | frames | content | person | size | camera (footage / edit) | cadence | in / out transition | effects, layers | role |
|---|---|---|---|---|---|---|---|---|---|

| text | frames in / full / out | words | font class | size % H | centre | colour | animation | front / behind |
|---|---|---|---|---|---|---|---|---|

Meaning: <element -> what it means -> what it becomes>
Missing roles: <role, what the user supplied, gap, plan>
Doubts: <what you could not decide, with frames>
```

Shots are contiguous: the first starts at frame 0, each starts one after the previous ends, the last ends at
frames-1. Text inside a shot stays inside its frames unless marked as crossing a cut. Keep observations ("the
reference does X at f12") apart from proposals ("we could do Y"); never present an idea as something seen.

## 9. Splitting the read across agents

Only for long or dense references (more than ~25 shots, longer than ~20 s, or 100+ text and sticker events) and only
when the host can run subagents with these tools. Otherwise read it yourself, section by section: one careful reader
beats several careless ones, and every agent costs tokens.
- Before splitting: finish §1 steps 1-2 yourself (cuts confirmed, trick line drafted). Agents work inside your frame.
- Split by section where the reference repeats a scheme, otherwise 3-4 shots per agent; at most ~6 agents, each
  with its frame range plus 2 frames of overlap on each side.
- Each agent: works only in `ref/<name>/agents/<its id>/`, uses `rn sheet` / `rn frame` for zooms and never re-runs
  `rn ref` (it rewrites the shared folder), only foreground calls, never `restart`, no credit-spending or upload tools.
- Each returns: its shot rows and text rows, any boundary changes with the evidence frame, its event-log lines, its
  doubts, and the ranges it actually viewed.
- After: merge, settle the overlaps yourself on a zoom strip, and check the union of viewed ranges covers every frame.

## 10. Before you call the read done

- [ ] Every frame 0..N-1 viewed; the viewed ranges have no gaps; the per-second log includes the partial last second.
- [ ] Every cut and suspect checked against grain, flashes, text pops and flying letters; 1-frame inserts found.
- [ ] Ink or matte wipes logged as wipes, not smears; transitions that hide cuts marked.
- [ ] Cadence counted per shot, not assumed; fps kept as a fraction (29.97 is 30000/1001).
- [ ] Letterboxed or windowed references measured inside the picture (bars skew the look and fake cuts).
- [ ] BPM checked against shot lengths; cut-on-sound share and phase noted; suspected splices noted.
- [ ] Text frames confirmed on native frames; sticker density counted per second.
- [ ] Video length taken from frames, not from the container or the audio.
- [ ] Nothing paid during the read. Anything that can only be learned by generating goes into the plan, priced.

## 11. Telling the user

3-5 plain lines in the user's language: what the reference does (the trick), how the remake will be made, what stays
the same and what changes, and anything you could not inspect. No pipeline words or bare shot numbers: describe shots
("the close-up of the label", "the back view on the stairs"). If you refer to shots by number, upload the labelled
cut sheet with it. Then follow the active workflow skill: plan, estimate, and wait for the user's approval before
anything that spends credits.
