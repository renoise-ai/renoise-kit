/* RNFX: edit effects for renderFrame pages (Canvas2D). Readable source: call it, copy it, change it, ignore it.
 * Defaults are engine defaults, not design defaults; pick values for the piece in front of you.
 * Deterministic: randomness comes only from hash(seed, frame index, ...), so any frame renders the same
 * in any order, shard or rerun. Never use Math.random() in a frame.
 * Time: functions taking `i` use the frame index; those taking `t` use seconds (t = i / fps).
 * fps / size come from opts, else window.RN = {fps, frames, width, height}, else the canvas itself. */
(function () {
  const RN = () => window.RN || {};
  const size = (ctx, o) => [o.width || RN().width || ctx.canvas.width, o.height || RN().height || ctx.canvas.height];

  // hash(a,b,c) -> [0,1): integer mix, same inputs always give the same number.
  function hash(a, b, c) {
    let h = Math.imul((a | 0) ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul((b | 0) + 0x7f4a7c15, 0xc2b2ae35) ^ Math.imul((c | 0) + 0x165667b1, 0x27d4eb2f);
    h = Math.imul(h ^ (h >>> 15), 0x2c1b3c6d); h = Math.imul(h ^ (h >>> 12), 0x297a2d39); h ^= h >>> 15;
    return (h >>> 0) / 4294967296;
  }
  // rng(seed) -> () => [0,1): mulberry32 stream.
  function rng(seed) {
    let s = seed | 0;
    return () => { s = (s + 0x6d2b79f5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  const EASE = {
    linear: (t) => t, in2: (t) => t * t, out2: (t) => 1 - (1 - t) * (1 - t), out3: (t) => 1 - Math.pow(1 - t, 3),
    inout2: (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
    outBack: (t) => 1 + 2.70158 * Math.pow(t - 1, 3) + 1.70158 * Math.pow(t - 1, 2),
  };
  // ease(name, t): t clamped to [0,1].
  const ease = (name, t) => EASE[name](Math.min(1, Math.max(0, t)));
  // envelope(t, at, attack, hold, release): 0 before `at`, ramps to 1 over attack, holds, ramps to 0 over release.
  function envelope(t, at, attack = 0.05, hold = 0, release = 0.2) {
    const u = t - at;
    if (u < 0) return 0;
    if (u < attack) return u / attack;
    if (u < attack + hold) return 1;
    const r = u - attack - hold;
    return r < release ? 1 - r / release : 0;
  }

  const tiles = {};
  // grain: film grain. amount=opacity, cell=grain size px, mono=gray noise, op=composite mode.
  function grain(ctx, i, o = {}) {
    const { amount = 0.3, cell = 2, mono = true, seed = 7, op = 'soft-light' } = o;
    const [w, h] = size(ctx, o), T = 256, n = Math.ceil(T / cell), key = seed + ':' + cell + ':' + mono;
    if (!tiles[key]) {
      tiles[key] = [0, 1, 2, 3].map((k) => {  // 4 cached tiles; frames pick one and offset it
        const small = document.createElement('canvas'); small.width = small.height = n;
        const sc = small.getContext('2d'), img = sc.createImageData(n, n), r = rng(seed * 131 + k);
        for (let p = 0; p < n * n; p++) {
          const v = r() * 255;
          img.data[p * 4] = v; img.data[p * 4 + 1] = mono ? v : r() * 255; img.data[p * 4 + 2] = mono ? v : r() * 255; img.data[p * 4 + 3] = 255;
        }
        sc.putImageData(img, 0, 0);
        const tile = document.createElement('canvas'); tile.width = tile.height = T;
        const tc = tile.getContext('2d'); tc.imageSmoothingEnabled = false; tc.drawImage(small, 0, 0, T, T);
        return tile;
      });
    }
    const tile = tiles[key][Math.floor(hash(seed, i, 1) * 4)];
    // Grain only textures what is already drawn: on a transparent overlay, soft-light over empty pixels would paint
    // the noise itself and grey out the whole layer. Keep the frame's own alpha by masking with a copy of it.
    const before = document.createElement('canvas'); before.width = ctx.canvas.width; before.height = ctx.canvas.height;
    before.getContext('2d').drawImage(ctx.canvas, 0, 0);
    ctx.save();
    ctx.globalCompositeOperation = op; ctx.globalAlpha = amount;
    ctx.translate(Math.floor(hash(seed, i, 2) * T), Math.floor(hash(seed, i, 3) * T));
    ctx.fillStyle = ctx.createPattern(tile, 'repeat');
    ctx.fillRect(-T, -T, w + T, h + T);
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'destination-in';
    ctx.drawImage(before, 0, 0);
    ctx.restore();
  }

  // vignette: darkens edges. inner/outer = start/full radius as fraction of the half diagonal.
  function vignette(ctx, o = {}) {
    const { amount = 0.35, inner = 0.45, outer = 0.95, color = '#000' } = o;
    const [w, h] = size(ctx, o), R = Math.hypot(w, h) / 2;
    const g = ctx.createRadialGradient(w / 2, h / 2, inner * R, w / 2, h / 2, outer * R);
    g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, color);
    ctx.save(); ctx.globalAlpha = amount; ctx.fillStyle = g; ctx.fillRect(0, 0, w, h); ctx.restore();
  }

  // leak: drifting light leak. x,y = centre (0..1), r = radius as fraction of the long side, drift = wander as fraction of width.
  function leak(ctx, i, o = {}) {
    const { color = '#ff7a30', color2 = '#ffd27a', amount = 0.55, x = 0.85, y = 0.2, r = 0.6, drift = 0.1, seed = 3, op = 'screen' } = o;
    const [w, h] = size(ctx, o), fps = o.fps || RN().fps || 30, ph = hash(seed, 0, 0) * 6.283;
    const cx = (x + Math.sin(i / fps * 0.9 + ph) * drift) * w, cy = (y + Math.cos(i / fps * 0.7 + ph) * drift) * h, R = r * Math.max(w, h);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    g.addColorStop(0, color2); g.addColorStop(0.35, color); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save(); ctx.globalCompositeOperation = op; ctx.globalAlpha = amount; ctx.fillStyle = g; ctx.fillRect(0, 0, w, h); ctx.restore();
  }

  // flash: full-frame colour that fades out over `duration` s from `at`. Use color '#000' for a dip.
  function flash(ctx, t, o = {}) {
    const { at = 0, duration = 0.1, color = '#fff', amount = 1 } = o, u = (t - at) / duration;
    if (u < 0 || u >= 1) return;
    const [w, h] = size(ctx, o);
    ctx.save(); ctx.globalAlpha = amount * (1 - u); ctx.fillStyle = color; ctx.fillRect(0, 0, w, h); ctx.restore();
  }

  // letterbox: bars as fractions of the height.
  function letterbox(ctx, o = {}) {
    const { top = 0.1, bottom = 0.1, color = '#000' } = o, [w, h] = size(ctx, o);
    ctx.save(); ctx.fillStyle = color; ctx.fillRect(0, 0, w, h * top); ctx.fillRect(0, h * (1 - bottom), w, h * bottom); ctx.restore();
  }

  // rgbSplit: red shifted left, blue right by px (reads the canvas back, so call it on the finished frame).
  function rgbSplit(ctx, o = {}) {
    const px = o.px === undefined ? 6 : o.px, [w, h] = size(ctx, o);
    const src = ctx.getImageData(0, 0, w, h), out = ctx.createImageData(w, h), s = src.data, d = out.data;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const p = (y * w + x) * 4, rx = Math.min(w - 1, Math.max(0, Math.round(x + px))), bx = Math.min(w - 1, Math.max(0, Math.round(x - px)));
      d[p] = s[(y * w + rx) * 4]; d[p + 1] = s[p + 1]; d[p + 2] = s[(y * w + bx) * 4 + 2]; d[p + 3] = s[p + 3];
    }
    ctx.putImageData(out, 0, 0);
  }

  // glitch: horizontal slice displacement. amount = share of slices hit and how far they move.
  function glitch(ctx, i, o = {}) {
    const { amount = 0.5, slices = 8, seed = 5 } = o, [w, h] = size(ctx, o);
    const snap = document.createElement('canvas'); snap.width = w; snap.height = h; snap.getContext('2d').drawImage(ctx.canvas, 0, 0);
    const bh = Math.ceil(h / slices);
    for (let k = 0; k < slices; k++) {
      if (hash(seed, i, k) >= amount) continue;
      const dx = (hash(seed, i, k + 100) - 0.5) * amount * 0.3 * w;
      ctx.clearRect(0, k * bh, w, bh); ctx.drawImage(snap, 0, k * bh, w, bh, dx, k * bh, w, bh);
    }
  }

  // strobe: colour overlay on every `every`-th frame.
  function strobe(ctx, i, o = {}) {
    const { every = 2, color = '#fff', amount = 1 } = o;
    if (i % every) return;
    const [w, h] = size(ctx, o);
    ctx.save(); ctx.globalAlpha = amount; ctx.fillStyle = color; ctx.fillRect(0, 0, w, h); ctx.restore();
  }

  // punch: scale for a punch-in at `at` s that settles to 1 after `decay` s. Apply with ctx.scale around the centre.
  function punch(t, o = {}) {
    const { at = 0, amount = 0.07, decay = 0.25 } = o, e = Math.max(0, 1 - (t - at) / decay);
    return t < at ? 1 : 1 + amount * e * e;
  }

  // shake: {dx, dy} px offset, amp=[x,y] px, decaying to 0 after `decay` s. Apply with ctx.translate.
  function shake(i, t, o = {}) {
    const { at = 0, amp = [16, 12], decay = 0.25, seed = 9 } = o, e = t < at ? 0 : Math.max(0, 1 - (t - at) / decay);
    return { dx: (hash(seed, i, 1) * 2 - 1) * amp[0] * e, dy: (hash(seed, i, 2) * 2 - 1) * amp[1] * e };
  }

  window.RNFX = { hash, rng, ease, envelope, grain, vignette, leak, flash, letterbox, rgbSplit, glitch, strobe, punch, shake };
})();
