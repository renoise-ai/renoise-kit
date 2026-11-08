"""rn capture URL --demo demo.json: a scripted product demo of a live web page (Screen Studio style), frame-exact.

How time works. Nothing is screen-recorded against the wall clock. rn keeps its own video clock and, for every output
frame (time n / fps), puts the page in the state of that instant and takes one screenshot:

* the cursor is drawn by rn (an arrow sprite composited onto the screenshot), moving on an eased curve; the real mouse
  is moved to the same point before each frame, so hover styles and pointer events are the page's own;
* the page's JavaScript time is Playwright's fake clock (page.clock): Date, performance.now, setTimeout / setInterval
  and requestAnimationFrame only move when rn advances them, by exactly one frame per frame;
* CSS animations and transitions (Web Animations) are paused as soon as they appear and seeked to "video time since
  they appeared" before each frame, so a hover fade or a spinner is at the same pose in every run;
* the native text caret blinks on the wall clock, so it is hidden and rn draws a steady caret at the insertion point
  of the focused field (input, textarea: by measuring the text; contenteditable: from the selection);
* each step's length is decided by the script and the layout alone: a glide takes 400-900 ms by distance, typing is
  seeded (human speed with small jitter), every action is followed by a hold. The frame count is therefore
  ceil(total_ms * fps / 1000), whatever the machine's speed. Two runs of the same script on the same page give the
  same frames (checked bit for bit on a page with timers, a hover transition, an infinite CSS animation and typing).

Waiting is off camera: {"wait": TARGET} polls until the element is visible (advancing the page clock while it waits,
real time for the network) and {"wait": ms} runs the page's timers for ms, both without adding frames, so a slow
backend changes nothing in the video but the moment a result is already there. {"pause": ms} is the on-camera hold.

Limits: what the page does outside these clocks is not controlled: network timing (it decides only how long rn waits
off camera), video elements, GIFs, Web Workers' own timers and canvas/WebGL work tied to wall time. Content that
depends on them may differ between runs; the frame count and step times never do. Dates on the page read a fixed
start time (2026-01-15 09:41 UTC, or demo.json "clock") plus video time. The drawn caret is approximate for centred / right-aligned text and wrapped
textarea lines.

Zoom crops the viewport through Chromium's own screenshot scale (DevTools Page.captureScreenshot clip.scale), so a
zoomed frame is re-rendered sharp, not upscaled. --vertical takes a second 9:16 crop per frame that follows the zoom
target, or the cursor (smoothed) when not zoomed; it zooms by the square root of the main zoom, since the 9:16
cut is already about 1.8x closer.

Run demos on staged or fictional data only: the script types what it says, the video shows everything on screen, and
the JSON and video are meant to be shared. Never put real passwords, tokens, customer data or personal details in a
demo.json; sign in through a test account set up in "setup" (localStorage / a staging URL) instead.
"""
from __future__ import annotations

import base64
import io
import json
import math
import random
import subprocess
import time
from pathlib import Path

from .ff import RnError

HOLD_MS = 600            # still time after each action, so the result reads (a step's "hold" overrides it)
ZOOM_MS = 700            # camera move in / out / across
GLIDE_MS = (400, 900)    # cursor move time, by distance
SMALL_TARGET = 0.04      # auto zoom when the target covers less than this share of the viewport
AUTO_ZOOM_MAX = 2.0
TYPE_MS = 75             # mean time per typed character (about 13 characters a second)
SCROLL_MS = (450, 1300)
TARGET_TIMEOUT_S = 5.0   # a step's target must be visible within this (real time)
WAIT_TIMEOUT_S = 15.0    # {"wait": TARGET}
SETTLE_MS = 500          # page time run before the first frame
CURSOR_CSS = 22          # cursor height in CSS px at zoom 1
CLOCK_START = 1_768_470_060.0  # 2026-01-15 09:41 UTC: what Date.now() says when recording starts (demo.json "clock")
BACKDROP = ((32, 34, 64), (122, 76, 188))  # top-left -> bottom-right gradient behind the window (--backdrop)

TARGET_KEYS = ('role', 'name', 'text', 'label', 'placeholder', 'exact')
STEP_KEYS = {'click': {'hold'}, 'type': {'into', 'hold'}, 'hover': {'hold'}, 'press': {'hold'}, 'scroll': {'hold'},
             'wait': set(), 'zoom': {'scale'}, 'pause': set(), 'caption': set()}
SETUP_KEYS = {'goto': set(), 'reload': set(), 'localStorage': set(), 'click': set(), 'type': {'into'}, 'press': set(),
              'wait': set()}

TICK_JS = '''
window.__rnDemoTick = (now, prev) => {
  for (const a of document.getAnimations()) {
    if (a.__rnDone) continue;
    if (a.__rnT0 === undefined) { a.__rnT0 = prev; try { a.pause(); } catch (e) {} }
    const t = now - a.__rnT0;
    let end = Infinity;
    try { end = a.effect ? a.effect.getComputedTiming().endTime : Infinity; } catch (e) {}
    if (Number.isFinite(end) && t >= end) { a.__rnDone = true; try { a.finish(); } catch (e) {} continue; }
    try { a.currentTime = t; } catch (e) {}
  }
  let caret = null;
  try { caret = window.__rnCaret(); } catch (e) {}
  return [scrollX, scrollY, caret];
};
// The native caret blinks on the wall clock, so it is hidden (as Playwright's own screenshots do) and rn draws a
// steady one here: [x, y, height, color] in viewport CSS px for a focused text field or contenteditable.
window.__rnCaret = () => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const cs = getComputedStyle(el);
  const fs = parseFloat(cs.fontSize) || 16;
  const sel = getSelection();
  if (el.isContentEditable) {
    if (!sel.rangeCount || !sel.getRangeAt(0).collapsed) return null;
    const range = sel.getRangeAt(0);
    const r = range.getClientRects()[0];
    if (r && r.height) return [r.left, r.top, r.height, cs.color];
    const box = el.getBoundingClientRect();
    return [box.left + parseFloat(cs.paddingLeft), box.top + parseFloat(cs.paddingTop), fs * 1.2, cs.color];
  }
  const textual = ['text', 'search', 'email', 'url', 'tel', 'password'];
  if (!((el.tagName === 'INPUT' && textual.includes(el.type)) || el.tagName === 'TEXTAREA')) return null;
  if (el.selectionStart !== el.selectionEnd) return null;
  const ctx = (window.__rnMeasure = window.__rnMeasure || document.createElement('canvas').getContext('2d'));
  ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  let before = el.value.slice(0, el.selectionStart);
  if (el.type === 'password') before = '\\u2022'.repeat(before.length);
  const lines = before.split('\\n');
  const line = lines[lines.length - 1];
  const box = el.getBoundingClientRect();
  const left = box.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
  const right = box.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight);
  const x = Math.min(Math.max(left + ctx.measureText(line).width - el.scrollLeft, left), right);
  const h = fs * 1.2;
  if (el.tagName === 'INPUT') return [x, box.top + box.height / 2 - h / 2, h, cs.color];
  const lh = cs.lineHeight === 'normal' ? h : parseFloat(cs.lineHeight);
  const top = box.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop) + (lines.length - 1) * lh - el.scrollTop;
  return [x, top + (lh - h) / 2, h, cs.color];
};
'''


# --------------------------------------------------------------------------------------------- the script

def _describe(target) -> str:
    if isinstance(target, str):
        return target
    return ' '.join(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in target.items())


def _check_target(where: str, target) -> None:
    if isinstance(target, str) and target.strip():
        return
    if isinstance(target, dict) and target and set(target) <= set(TARGET_KEYS) and \
            sum(k in target for k in ('role', 'text', 'label', 'placeholder')) == 1 and \
            all(isinstance(v, str) for k, v in target.items() if k != 'exact'):
        return
    raise RnError(f'{where}: a target is a selector string or one of {{"role", "name"}}, {{"text"}}, {{"label"}}, '
                  f'{{"placeholder"}} (optional "exact": true); got {json.dumps(target, ensure_ascii=False)}')


def _step_kind(where: str, step, allowed: dict) -> str:
    if not isinstance(step, dict):
        raise RnError(f'{where}: a step is an object like {{"click": "#save"}}')
    kinds = [k for k in step if k in allowed]
    if len(kinds) != 1:
        raise RnError(f'{where}: one action per step, one of {", ".join(allowed)}; got {json.dumps(step, ensure_ascii=False)}')
    kind = kinds[0]
    extra = set(step) - {kind} - allowed[kind]
    if extra:
        raise RnError(f'{where}: "{kind}" does not take {", ".join(sorted(extra))}')
    return kind


def _check_step(where: str, step: dict, kind: str) -> None:
    value = step[kind]
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if kind in ('click', 'hover'):
        _check_target(where, value)
    elif kind == 'type':
        if not isinstance(value, str):
            raise RnError(f'{where}: "type" takes the text to type')
        if 'into' in step:
            _check_target(where, step['into'])
    elif kind == 'press' and not (isinstance(value, str) and value):
        raise RnError(f'{where}: "press" takes a key like "Enter" or "Control+A"')
    elif kind == 'scroll' and not number(value):
        raise RnError(f'{where}: "scroll" takes pixels (negative scrolls up)')
    elif kind == 'wait' and not (number(value) and value >= 0):
        _check_target(where, value)
    elif kind == 'zoom' and value != 'off':
        _check_target(where, value)
        if 'scale' in step and not (number(step['scale']) and 1 <= step['scale'] <= 4):
            raise RnError(f'{where}: "scale" is 1..4')
    elif kind == 'pause' and not (number(value) and value >= 0):
        raise RnError(f'{where}: "pause" takes milliseconds')
    elif kind == 'caption' and not isinstance(value, str):
        raise RnError(f'{where}: "caption" takes text ("" ends the current caption)')
    elif kind == 'goto' and not isinstance(value, str):
        raise RnError(f'{where}: "goto" takes a URL (relative to the demo URL)')
    elif kind == 'localStorage' and not isinstance(value, dict):
        raise RnError(f'{where}: "localStorage" takes {{"key": value}}')
    if 'hold' in step and not (number(step['hold']) and step['hold'] >= 0):
        raise RnError(f'{where}: "hold" takes milliseconds')


def load_demo(path: str | Path) -> dict:
    """Read and check a demo script; every problem names the step (1-based) before a browser starts."""
    source = Path(path)
    if not source.is_file():
        raise RnError(f'no such demo script: {path}')
    try:
        demo = json.loads(source.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise RnError(f'{path}: not JSON ({error})') from None
    if not isinstance(demo, dict) or not isinstance(demo.get('steps'), list) or not demo['steps']:
        raise RnError(f'{path}: expected {{"setup": [...], "steps": [...]}} with at least one step')
    unknown = set(demo) - {'setup', 'steps', 'seed', 'clock', 'note'}
    if unknown:
        raise RnError(f'{path}: unknown keys {", ".join(sorted(unknown))} (setup, steps, seed, clock, note)')
    for index, step in enumerate(demo.get('setup') or [], 1):
        _check_step(f'setup step {index}', step, _step_kind(f'setup step {index}', step, SETUP_KEYS))
    for index, step in enumerate(demo['steps'], 1):
        _check_step(f'step {index}', step, _step_kind(f'step {index}', step, STEP_KEYS))
    return demo


# --------------------------------------------------------------------------------------------- motion

def _ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 4 * u * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def glide_ms(distance: float, diagonal: float) -> int:
    """Cursor move time: 400 ms for a short hop up to 900 ms across most of the screen; 0 when already there."""
    if distance < 3:
        return 0
    low, high = GLIDE_MS
    return round(low + (high - low) * min(1.0, distance / (0.8 * diagonal)))


class _Cursor:
    def __init__(self, x: float, y: float):
        self.start = self.end = (x, y)
        self.control = (x, y)
        self.t0 = self.t1 = 0.0
        self.clicks: list[tuple[float, float, float]] = []

    def glide(self, to: tuple[float, float], t0: float, duration: float, bend: float) -> None:
        start = self.at(t0)
        mid = ((start[0] + to[0]) / 2, (start[1] + to[1]) / 2)
        dx, dy = to[0] - start[0], to[1] - start[1]
        self.start, self.end, self.t0, self.t1 = start, to, t0, t0 + duration
        self.control = (mid[0] - dy * bend, mid[1] + dx * bend)  # a gentle arc, not a ruler line

    def at(self, t: float) -> tuple[float, float]:
        if t >= self.t1 or self.t1 <= self.t0:
            return self.end
        u = _ease((t - self.t0) / (self.t1 - self.t0))
        (ax, ay), (cx, cy), (bx, by) = self.start, self.control, self.end
        return ((1 - u) ** 2 * ax + 2 * (1 - u) * u * cx + u * u * bx, (1 - u) ** 2 * ay + 2 * (1 - u) * u * cy + u * u * by)


class _Camera:
    """Where the frame looks: a focus point (CSS px in the viewport, or None = follow the cursor) and a zoom, with eased
    moves between two such poses."""

    def __init__(self):
        self.a = self.b = (None, 1.0)
        self.t0 = self.t1 = 0.0
        self.manual = False

    @property
    def target(self):
        return self.b

    def go(self, focus, zoom: float, t0: float, duration: float, cursor: _Cursor) -> None:
        x, y, z = self.at(t0, cursor)
        self.a, self.b, self.t0, self.t1 = ((x, y), z), (focus, zoom), t0, t0 + duration

    def at(self, t: float, cursor: _Cursor) -> tuple[float, float, float]:
        def pose(p):
            focus, zoom = p
            fx, fy = focus if focus is not None else cursor.at(t)
            return fx, fy, zoom
        b = pose(self.b)
        if t >= self.t1 or self.t1 <= self.t0:
            return b
        a, u = pose(self.a), _ease((t - self.t0) / (self.t1 - self.t0))
        return tuple(a[k] + (b[k] - a[k]) * u for k in range(3))


def _clip(fx: float, fy: float, w: float, h: float, vw: float, vh: float) -> tuple[float, float, float, float]:
    w, h = min(w, vw), min(h, vh)
    return min(max(fx - w / 2, 0.0), vw - w), min(max(fy - h / 2, 0.0), vh - h), w, h


# --------------------------------------------------------------------------------------------- drawing

_ARROW = [(0, 0), (0, 16.6), (4.3, 12.8), (7.1, 19.2), (9.9, 18.0), (7.2, 11.8), (12.6, 11.8)]


class _Painter:
    def __init__(self):
        self.sprites: dict[int, tuple] = {}

    def cursor(self, height: int):
        """An arrow (white, dark outline, soft shadow) `height` px tall; (sprite, hotspot)."""
        from PIL import Image, ImageDraw, ImageFilter
        height = max(8, height)
        if height not in self.sprites:
            ss = 4
            unit = height * ss / 19.2
            pad = round(unit * 3)
            size = (round(13 * unit) + 2 * pad, round(20 * unit) + 2 * pad)
            points = [(pad + x * unit, pad + y * unit) for x, y in _ARROW]
            shadow = Image.new('RGBA', size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).polygon([(x + unit * 0.5, y + unit * 1.1) for x, y in points], fill=(0, 0, 0, 90))
            sprite = shadow.filter(ImageFilter.GaussianBlur(unit * 1.1))
            ImageDraw.Draw(sprite).polygon(points, fill=(255, 255, 255, 255), outline=(20, 20, 24, 255), width=max(1, round(unit * 1.2)))
            sprite = sprite.resize((size[0] // ss, size[1] // ss), Image.LANCZOS)
            self.sprites[height] = (sprite, (pad / ss, pad / ss))
        return self.sprites[height]

    @staticmethod
    def ripple(frame, x: float, y: float, radius: float, alpha: float, width: float) -> None:
        from PIL import Image, ImageDraw
        ss = 3
        r = radius + width
        box = math.ceil(r) + 2
        patch = Image.new('RGBA', (box * 2 * ss, box * 2 * ss), (0, 0, 0, 0))
        c = box * ss
        ImageDraw.Draw(patch).ellipse((c - radius * ss, c - radius * ss, c + radius * ss, c + radius * ss),
                                      fill=(64, 132, 255, round(alpha * 70)), outline=(64, 132, 255, round(alpha * 255)),
                                      width=max(1, round(width * ss)))
        patch = patch.resize((box * 2, box * 2), Image.LANCZOS)
        frame.paste(patch, (round(x) - box, round(y) - box), patch)


def _backdrop(size: tuple[int, int], window: tuple[int, int, int, int], radius: int):
    """Gradient background with the window's soft shadow, and the window's rounded-corner mask."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = ((xx / max(1, w - 1)) + (yy / max(1, h - 1))) / 2
    a, b = (np.array(c, dtype=np.float32) for c in BACKDROP)
    background = Image.fromarray((a + (b - a) * u[..., None]).clip(0, 255).astype(np.uint8), 'RGB')
    x, y, ww, wh = window
    shadow = Image.new('L', size, 0)
    offset = max(2, radius // 2)
    ImageDraw.Draw(shadow).rounded_rectangle((x, y + offset, x + ww, y + wh + offset), radius, fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius * 1.5))
    background.paste((0, 0, 0), (0, 0), shadow)
    mask = Image.new('L', (ww * 4, wh * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ww * 4 - 1, wh * 4 - 1), radius * 4, fill=255)
    return background, mask.resize((ww, wh), Image.LANCZOS)


def _encoder(path: Path, size: tuple[int, int], fps: float) -> subprocess.Popen:
    cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{size[0]}x{size[1]}',
           '-framerate', str(fps), '-i', 'pipe:0', '-c:v', 'libx264', '-crf', '16', '-preset', 'fast', '-pix_fmt', 'yuv420p',
           '-movflags', '+faststart', '-y', str(path)]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RnError('ffmpeg not found on PATH') from None


def _finish(encoder: subprocess.Popen, path: Path) -> None:
    encoder.stdin.close()
    stderr = encoder.stderr.read().decode('utf-8', 'replace')
    if encoder.wait():
        raise RnError(f'encoding {path}: {stderr.strip()[-300:]}')


def _even(n: float) -> int:
    return max(2, round(n) // 2 * 2)


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f'{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


# --------------------------------------------------------------------------------------------- the recorder

class _StepError(Exception):
    pass


class _Recorder:
    def __init__(self, page, cdp, *, fps: float, viewport: dict, scale: float, size: tuple[int, int],
                 window: tuple[int, int, int, int] | None, vertical: tuple[int, int] | None, out_dir: Path, seed: int):
        self.page, self.cdp, self.fps = page, cdp, fps
        self.vw, self.vh = viewport['width'], viewport['height']
        self.scale, self.size, self.window, self.vertical_size = scale, size, window, vertical
        self.out_dir = out_dir
        self.seed = seed
        self.t = 0               # video time recorded so far (ms)
        self.n = 0               # frames written
        self.clock_ms = 0        # page clock advanced since the start of recording
        self.offscreen_ms = 0    # page time run off camera ({"wait"})
        self.cursor = _Cursor(self.vw / 2, self.vh * 0.6)
        self.camera = _Camera()
        self.painter = _Painter()
        self.mouse = None
        self.vertical_x = None
        self.backdrop = _backdrop(size, window, max(6, round(size[0] / 110))) if window else None
        self.video = out_dir / 'demo.mp4'
        self.encoders = [_encoder(self.video, size, fps)]
        if vertical:
            self.vertical_video = out_dir / 'demo-vertical.mp4'
            self.encoders.append(_encoder(self.vertical_video, vertical, fps))

    # -- time

    def frame_ms(self, n: int) -> float:
        return n * 1000.0 / self.fps

    def record(self, until: float, each=None) -> None:
        """Write every frame whose time is before `until` (ms of video), calling each(t) first to pose the page."""
        until = round(until)
        while self.frame_ms(self.n) < until - 1e-6:
            t = self.frame_ms(self.n)
            if each:
                each(t)
            self.shoot(t)
            self.n += 1
        self.t = max(self.t, until)

    def run_page(self, ms: int) -> None:
        if ms > 0:
            self.page.clock.run_for(ms)
            self.clock_ms += ms

    def offscreen(self, ms: int) -> None:
        self.run_page(ms)
        self.offscreen_ms += ms

    # -- one frame

    def _tick(self, t: float) -> tuple[tuple[float, float], list | None]:
        """Seek the page's animations to video time t; the scroll offset and the caret to draw."""
        prev = self.frame_ms(self.n - 1) if self.n else t
        for attempt in range(2):
            try:
                x, y, caret = self.page.evaluate('([t, p]) => window.__rnDemoTick ? window.__rnDemoTick(t, p) : [scrollX, scrollY, null]',
                                                 [t, prev])
                return (x, y), caret
            except Exception:  # a navigation replaced the document between frames: wait for the new one
                if attempt:
                    raise
                self.page.wait_for_load_state('load')
        return (0.0, 0.0), None

    def _capture(self, clip: tuple[float, float, float, float], scroll: tuple[float, float], size: tuple[int, int]):
        from PIL import Image
        x, y, w, h = clip
        shot = self.cdp.send('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False,
                                                        'clip': {'x': x + scroll[0], 'y': y + scroll[1], 'width': w, 'height': h,
                                                                 'scale': size[0] / w}})
        image = Image.open(io.BytesIO(base64.b64decode(shot['data']))).convert('RGB')
        return image if image.size == size else image.resize(size, Image.LANCZOS)

    def _overlay(self, frame, clip, t: float, cx: float, cy: float, caret: list | None) -> None:
        from PIL import ImageDraw
        x0, y0, w, _ = clip
        k = frame.width / w  # output px per CSS px
        zoom = k / self.scale if self.scale else 1.0
        if caret:
            x, y, h, color = caret
            rgb = tuple(int(float(v)) for v in color[color.find('(') + 1:color.find(')')].split(',')[:3]) if '(' in color else (0, 0, 0)
            left = round((x - x0) * k)
            ImageDraw.Draw(frame).rectangle((left, round((y - y0) * k), left + max(1, round(k)) - 1, round((y + h - y0) * k)), fill=rgb)
        px, py = (cx - x0) * k, (cy - y0) * k
        press = 1.0
        for (tc, rx, ry) in self.cursor.clicks:
            age = t - tc
            if 0 <= age < 500:
                u = age / 500
                self.painter.ripple(frame, (rx - x0) * k, (ry - y0) * k, (7 + 22 * u) * k, (1 - u) ** 1.5, 2.2 * k)
            if 0 <= age < 140:
                press = 0.86
        sprite, (hx, hy) = self.painter.cursor(round(CURSOR_CSS * self.scale * math.sqrt(zoom) * press))
        frame.paste(sprite, (round(px - hx), round(py - hy)), sprite)

    def shoot(self, t: float) -> None:
        cx, cy = self.cursor.at(t)
        if self.mouse != (round(cx, 2), round(cy, 2)):
            self.page.mouse.move(cx, cy)
            self.mouse = (round(cx, 2), round(cy, 2))
        self.run_page(self.offscreen_ms + round(t) - self.clock_ms)
        scroll, caret = self._tick(t)
        fx, fy, z = self.camera.at(t, self.cursor)
        inner = (self.window[2], self.window[3]) if self.window else self.size
        clip = _clip(fx, fy, self.vw / z, self.vh / z, self.vw, self.vh)
        frame = self._capture(clip, scroll, inner)
        self._overlay(frame, clip, t, cx, cy, caret)
        if self.backdrop:
            background, mask = self.backdrop
            canvas = background.copy()
            canvas.paste(frame, self.window[:2], mask)
            frame = canvas
        self.encoders[0].stdin.write(frame.tobytes())
        if self.vertical_size:
            # Smooth the followed point (time constant 150 ms) so the 9:16 crop glides instead of sticking to the cursor.
            if self.vertical_x is None:
                self.vertical_x = fx
            self.vertical_x += (fx - self.vertical_x) * (1 - math.exp(-(1000 / self.fps) / 150))
            h = self.vh / math.sqrt(z)  # the 9:16 cut already magnifies ~1.8x, so it zooms less
            vclip = _clip(self.vertical_x, fy, h * self.vertical_size[0] / self.vertical_size[1], h, self.vw, self.vh)
            vframe = self._capture(vclip, scroll, self.vertical_size)
            self._overlay(vframe, vclip, t, cx, cy, caret)
            self.encoders[1].stdin.write(vframe.tobytes())

    def close(self) -> None:
        _finish(self.encoders[0], self.video)
        if self.vertical_size:
            _finish(self.encoders[1], self.vertical_video)

    def abort(self) -> None:
        for encoder in self.encoders:
            try:
                encoder.stdin.close()
            except OSError:
                pass
            encoder.kill()
            encoder.wait()
        for video in (self.video, getattr(self, 'vertical_video', None)):
            if video and video.exists():
                video.unlink()

    # -- targets and motion

    def locator(self, target):
        page = self.page
        if isinstance(target, str):
            locator = page.locator(target)
        else:
            exact = bool(target.get('exact'))
            if 'role' in target:
                locator = page.get_by_role(target['role'], name=target.get('name'), exact=exact) if target.get('name') is not None \
                    else page.get_by_role(target['role'])
            elif 'text' in target:
                locator = page.get_by_text(target['text'], exact=exact)
            elif 'label' in target:
                locator = page.get_by_label(target['label'], exact=exact)
            else:
                locator = page.get_by_placeholder(target['placeholder'], exact=exact)
        return locator.first

    def locate(self, target):
        locator = self.locator(target)
        try:
            locator.wait_for(state='visible', timeout=TARGET_TIMEOUT_S * 1000)
        except Exception:
            raise _StepError(f'no visible element for {_describe(target)} within {TARGET_TIMEOUT_S:g} s') from None
        return locator

    def box(self, locator) -> dict:
        box = locator.bounding_box()
        if not box or box['width'] <= 0 or box['height'] <= 0:
            raise _StepError('the element has no size on screen')
        return box

    def scroll_by(self, dy: float) -> None:
        """Eased scroll of the window by dy CSS px (clamped to the page), frame by frame."""
        start, limit = self.page.evaluate('[scrollY, document.documentElement.scrollHeight - innerHeight]')
        end = min(max(start + dy, 0), max(0, limit))
        if abs(end - start) < 1:
            return
        low, high = SCROLL_MS
        duration = round(min(high, max(low, 300 + abs(end - start) * 0.5)))
        t0 = self.t
        self.record(t0 + duration, lambda t: self.page.evaluate('(y) => scrollTo({left: scrollX, top: y, behavior: "instant"})',
                                                              start + (end - start) * _ease((t - t0) / duration)))
        self.page.evaluate('(y) => scrollTo({left: scrollX, top: y, behavior: "instant"})', end)

    def bring_into_view(self, locator) -> dict:
        box = self.box(locator)
        if box['y'] >= 0 and box['y'] + box['height'] <= self.vh and box['x'] >= 0 and box['x'] + box['width'] <= self.vw:
            return box
        self.zoom_out_if_auto()
        self.scroll_by(box['y'] + box['height'] / 2 - self.vh / 2)
        box = self.box(locator)
        if box['y'] < 0 or box['y'] + box['height'] > self.vh or box['x'] < 0 or box['x'] + box['width'] > self.vw:
            locator.scroll_into_view_if_needed()  # inside a scrolling panel: jump
            box = self.box(locator)
        return box

    def auto_zoom(self, box: dict) -> float:
        if box['width'] * box['height'] >= SMALL_TARGET * self.vw * self.vh:
            return 1.0
        zoom = min(AUTO_ZOOM_MAX, 0.5 * self.vw / box['width'], 0.5 * self.vh / box['height'])
        return zoom if zoom >= 1.2 else 1.0

    def zoom_out_if_auto(self) -> None:
        _, zoom = self.camera.target
        if not self.camera.manual and zoom > 1.0:
            self.camera.go(None, 1.0, self.t, ZOOM_MS, self.cursor)

    def move_to(self, locator, index: int) -> tuple[float, float]:
        """Glide to the target's centre (the camera zooms in on a small target, or back out, at the same time)."""
        box = self.bring_into_view(locator)
        point = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        here = self.cursor.at(self.t)
        duration = glide_ms(math.dist(here, point), math.hypot(self.vw, self.vh))
        if not self.camera.manual:
            zoom = self.auto_zoom(box)
            focus, current = self.camera.target
            if zoom > 1.0 and (focus is None or math.dist(focus, point) > 2 or abs(current - zoom) > 0.01):
                self.camera.go(point, zoom, self.t, max(duration, ZOOM_MS), self.cursor)
                duration = max(duration, ZOOM_MS)
            elif zoom == 1.0 and current > 1.0:
                self.camera.go(None, 1.0, self.t, max(duration, ZOOM_MS), self.cursor)
        bend = random.Random(self.seed * 1009 + index).uniform(-0.12, 0.12)
        self.cursor.glide(point, self.t, duration, bend)
        self.record(self.t + duration)
        return point

    def hold(self, step: dict, default: int = HOLD_MS) -> None:
        self.record(self.t + round(step.get('hold', default)))

    def click_here(self, point: tuple[float, float]) -> None:
        self.page.mouse.move(*point)
        self.mouse = (round(point[0], 2), round(point[1], 2))
        self.page.mouse.down()
        self.page.mouse.up()
        self.cursor.clicks.append((self.t, *point))

    # -- steps

    def step(self, index: int, step: dict, kind: str, captions: list) -> None:
        value = step[kind]
        if kind in ('click', 'hover'):
            point = self.move_to(self.locate(value), index)
            if kind == 'click':
                self.click_here(point)
            self.hold(step)
        elif kind == 'type':
            if 'into' in step:
                point = self.move_to(self.locate(step['into']), index)
                self.click_here(point)
                self.record(self.t + 250)
            rng = random.Random(self.seed * 7919 + index)
            for char in value:
                self.page.keyboard.type(char)
                pause = TYPE_MS * rng.uniform(0.6, 1.45) + (55 if char in ' .,!?;:' else 0)
                self.record(self.t + round(pause))
            self.hold(step)
        elif kind == 'press':
            self.page.keyboard.press(value)
            self.hold(step)
        elif kind == 'scroll':
            self.zoom_out_if_auto()
            self.scroll_by(float(value))
            self.hold(step)
        elif kind == 'wait':
            self.wait(value)
        elif kind == 'zoom':
            if value == 'off':
                self.camera.manual = False
                self.camera.go(None, 1.0, self.t, ZOOM_MS, self.cursor)
            else:
                box = self.bring_into_view(self.locate(value))
                zoom = float(step.get('scale') or max(1.5, self.auto_zoom(box)))
                self.camera.manual = True
                self.camera.go((box['x'] + box['width'] / 2, box['y'] + box['height'] / 2), zoom, self.t, ZOOM_MS, self.cursor)
            self.record(self.t + ZOOM_MS)
        elif kind == 'pause':
            self.record(self.t + round(value))
        elif kind == 'caption':
            if captions and captions[-1]['end'] is None:
                captions[-1]['end'] = self.t
            if value:
                captions.append({'text': value, 'start': self.t, 'end': None, 'step': index})

    def wait(self, value) -> None:
        if not isinstance(value, (str, dict)):
            self.offscreen(round(value))
            return
        # Polled in real time while the page clock moves on; the video does not show the wait.
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        locator = self.locator(value)
        while not locator.is_visible():
            if time.monotonic() > deadline:
                raise _StepError(f'{_describe(value)} did not appear within {WAIT_TIMEOUT_S:g} s')
            self.offscreen(100)
            self.page.wait_for_timeout(100)


# --------------------------------------------------------------------------------------------- the command

def _setup(page, base_url: str, step: dict, kind: str, recorder: _Recorder) -> None:
    from urllib.parse import urljoin
    value = step[kind]
    if kind == 'goto':
        page.goto(urljoin(base_url, value), wait_until='load')
    elif kind == 'reload':
        if value:
            page.reload(wait_until='load')
    elif kind == 'localStorage':
        page.evaluate('(items) => { for (const [k, v] of Object.entries(items)) '
                      'localStorage.setItem(k, typeof v === "string" ? v : JSON.stringify(v)); }', value)
    elif kind == 'click':
        recorder.locate(value).click()
    elif kind == 'type':
        if 'into' in step:
            recorder.locate(step['into']).fill(value)
        else:
            page.keyboard.type(value)
    elif kind == 'press':
        page.keyboard.press(value)
    elif kind == 'wait':
        recorder.wait(value)


def _hide_script(hide: list[str] | None) -> str:
    css = 'html,*{scroll-behavior:auto !important;caret-color:transparent !important}' + ''.join(f'{s}{{display:none !important}}' for s in hide or [])
    return ('(() => { const add = () => { const s = document.createElement("style"); s.textContent = ' + json.dumps(css) +
            '; (document.head || document.documentElement).appendChild(s); };'
            ' if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", add); else add(); })();')


def _step_entry(index: int, step: dict, kind: str, t0: int, n0: int, recorder: _Recorder) -> dict:
    entry = {'index': index, 'kind': kind, 'start': round(t0 / 1000, 3), 'end': round(recorder.t / 1000, 3),
             'start_frame': n0, 'end_frame': recorder.n}
    value = step[kind]
    if kind == 'type':
        entry['text'] = value
        if 'into' in step:
            entry['target'] = _describe(step['into'])
    elif isinstance(value, (str, dict)) and kind in ('click', 'hover', 'zoom', 'wait') and value != 'off':
        entry['target'] = _describe(value)
    else:
        entry['value'] = value
    return entry


def demo_command(url: str, *, demo: str, out: str, device: str = 'desktop', size: tuple[int, int] | None = None,
                 fps: float = 30, vertical: bool = False, backdrop: bool = False, hide: list[str] | None = None) -> dict:
    """Record the demo script against url into out/demo.mp4 (+ demo-vertical.mp4); the result times every step."""
    from .capture import MOBILE_UA, READY_TIMEOUT_MS, WEB_SCALE, WEB_SIZE
    script = load_demo(demo)
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise RnError('rn capture needs playwright (extra "capture"): uv tool install "renoise-kit[all]" && playwright install chromium') from None
    if not 1 <= fps <= 120:
        raise RnError('--fps: 1..120')
    if vertical and device == 'mobile':
        raise RnError('--vertical makes a 9:16 cut of a desktop demo; a --mobile demo is already vertical')
    width, height = (_even(v) for v in (size or WEB_SIZE[device]))
    scale = WEB_SCALE[device]
    window = None
    if backdrop:  # the page sits in a rounded window on a gradient, with the frame's aspect
        pad_x = _even(width * 0.06)
        inner_w = width - 2 * pad_x
        inner_h = _even(inner_w * height / width)
        window = (pad_x, (height - inner_h) // 2, inner_w, inner_h)
    inner = (window[2], window[3]) if window else (width, height)
    viewport = {'width': round(inner[0] / scale), 'height': round(inner[1] / scale)}
    vertical_size = (height, _even(height * 16 / 9)) if vertical else None
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('failed-*.png'):
        stale.unlink()
    seed = int(script.get('seed') or 0)
    start = CLOCK_START
    if script.get('clock'):
        from datetime import datetime
        try:
            start = datetime.fromisoformat(str(script['clock']).replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise RnError(f'{demo}: "clock" is an ISO time like 2026-01-15T09:41:00Z') from None

    errors: list[str] = []
    steps_out: list[dict] = []
    captions: list[dict] = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-gpu', '--font-render-hinting=none', '--hide-scrollbars'])
        recorder = page = None
        where, current = 'setup', None
        try:
            context = browser.new_context(viewport=viewport, device_scale_factor=scale, is_mobile=device == 'mobile',
                                          has_touch=device == 'mobile', user_agent=MOBILE_UA if device == 'mobile' else None)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            # Page time starts SETTLE_MS before the first frame and moves only when rn moves it.
            page.clock.install(time=start - SETTLE_MS / 1000 - 1)
            page.clock.pause_at(start - SETTLE_MS / 1000)
            page.add_init_script(TICK_JS)
            page.add_init_script(_hide_script(hide))
            try:
                page.goto(url, wait_until='networkidle', timeout=READY_TIMEOUT_MS)
            except PlaywrightError:  # pages that never go quiet: take what loaded
                page.goto(url, wait_until='load', timeout=READY_TIMEOUT_MS)
            recorder = _Recorder(page, context.new_cdp_session(page), fps=fps, viewport=viewport, scale=scale,
                                 size=(width, height), window=window, vertical=vertical_size, out_dir=out_dir, seed=seed)
            for index, step in enumerate(script.get('setup') or [], 1):
                where, current = f'setup step {index}', step
                _setup(page, url, step, _step_kind(where, step, SETUP_KEYS), recorder)
            where, current = 'setup', None
            page.clock.run_for(SETTLE_MS)
            page.evaluate('document.fonts.ready.then(() => true)')
            recorder.clock_ms = recorder.offscreen_ms = 0
            for index, step in enumerate(script['steps'], 1):
                where, current = f'step {index}', step
                kind = _step_kind(where, step, STEP_KEYS)
                t0, n0 = recorder.t, recorder.n
                recorder.step(index, step, kind, captions)
                steps_out.append(_step_entry(index, step, kind, t0, n0, recorder))
            where, current = 'encoding', None
            if recorder.n == 0:
                raise _StepError('no step takes time on camera (add a "pause" or an action)')
            recorder.close()
        except (PlaywrightError, _StepError) as error:
            failed = out_dir / f'failed-{where.replace(" ", "-")}.png'
            try:
                page.screenshot(path=str(failed))
            except Exception:  # noqa: BLE001 - no page, or the browser is gone
                failed = None
            if recorder:
                recorder.abort()
            message = (str(error).splitlines() or [type(error).__name__])[0]
            raise RnError(f'demo {where}' + (f' {json.dumps(current, ensure_ascii=False)}' if current else '') + f': {message}'
                          + (f' (screenshot: {failed})' if failed else '')) from None
        except BaseException:
            if recorder:
                recorder.abort()
            raise
        finally:
            browser.close()

    total = recorder.t
    for caption in captions:
        caption['end'] = round((caption['end'] if caption['end'] is not None else total) / 1000, 3)
        caption['start'] = round(caption['start'] / 1000, 3)
    result: dict = {'url': url, 'demo': str(demo), 'device': device, 'viewport': viewport, 'scale': scale, 'dir': str(out_dir),
                    'video': {'path': str(recorder.video), 'frames': recorder.n, 'fps': fps, 'seconds': round(recorder.n / fps, 3),
                              'size': [width, height], 'backdrop': bool(window)},
                    'steps': steps_out, 'captions': captions}
    if vertical_size:
        result['vertical'] = {'path': str(recorder.vertical_video), 'frames': recorder.n, 'fps': fps, 'size': list(vertical_size)}
    if captions:
        srt = out_dir / 'demo.srt'
        srt.write_text(''.join(f'{k}\n{_srt_time(c["start"])} --> {_srt_time(c["end"])}\n{c["text"]}\n\n'
                               for k, c in enumerate(captions, 1)), encoding='utf-8')
        result['srt'] = str(srt)
    result['page_errors'] = errors[:5]
    result['timeline'] = str(out_dir / 'demo.timeline.json')
    Path(result['timeline']).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding='utf-8')
    result['seconds_taken'] = round(time.monotonic() - started, 1)
    return result


def demo_summary_lines(result: dict) -> list[str]:
    video = result['video']
    lines = [f'{video["path"]}  {video["frames"]} frames at {video["fps"]:g} fps ({video["seconds"]} s)  '
             f'{video["size"][0]}x{video["size"][1]}, page {result["viewport"]["width"]}x{result["viewport"]["height"]} CSS px'
             + ('  in a window' if video['backdrop'] else '')]
    if result.get('vertical'):
        lines.append(f'{result["vertical"]["path"]}  9:16 {result["vertical"]["size"][0]}x{result["vertical"]["size"][1]}, follows the action')
    for s in result['steps']:
        what = (json.dumps(s['text'], ensure_ascii=False) + (f' into {s["target"]}' if s.get('target') else '') if 'text' in s
                else s.get('target') or s.get('value', ''))
        lines.append(f'  {s["index"]:>2}. {s["start"]:7.3f}-{s["end"]:7.3f} s  {s["kind"]:7} {str(what)[:60]}')
    if result.get('srt'):
        lines.append(f'captions: {result["srt"]} ({len(result["captions"])}), for EDL "subtitles" or rn captions')
    lines.append(f'step times: {result["timeline"]}')
    lines += [f'warning: page error: {e}' for e in result['page_errors']]
    return lines
