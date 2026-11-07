# SPDX-License-Identifier: Apache-2.0
# Ported from rea-video (https://github.com/JuneYaooo/rea-video): rea_video/shots.py, rea_video/features.py and the frame
# reader of rea_video/media.py. Copyright 2026 JuneYaooo, licensed under the Apache License, Version 2.0 (LICENSES/Apache-2.0.txt,
# NOTICE). Changed for rn: one module; rn's probe and frame clock; no classification (rea-video's production labels, VLM and
# OCR are left out); pan speed in source pixels; per-shot measurement in parallel processes; output grouped for reading.
"""rn shots: every shot of a video and what its picture does, as numbers. Measurement only: it never says how a shot was
made or what to do with it.

Boundaries: hard cuts, fades through black or white, dissolves (a frame that is a linear blend of the frames around it,
told apart from camera moves and animated gradients), and cuts that open on a bright flash. rn cuts finds hard cuts only,
and its shots stay what EDL {"shot": n} means; rn shots can count more shots on the same file.

Per shot, from up to 12 sampled frames and 20 frame pairs (frames touching a transition skipped):
  still    share of consecutive frames that are pixel-identical, first-to-last difference
  cadence  the repeat pattern of identical frames (24 -> 30 fps pulldown repeats every 4-6 frames)
  motion   a rigid fit of the whole frame from frame to frame: how well one zoom / pan explains it (rigid_fit 0..1:
           a still image being panned or zoomed scores high, parallax and things moving inside score low), zoom per
           second, pan speed in source pixels per second and direction, the share of points moving on their own
  texture  flat 8x8 blocks (all, and outside near-black), colours covering 95% of pixels, straight (axis-aligned)
           edges, sensor-like noise, fine texture left after removing smooth gradients, gentle slopes (soft gradients),
           saturation and vivid (saturated, not dark) pixels, near-black and near-white share, how smooth the lit
           (luma > 50) and the inked (luma < 205) parts are, fine bright detail on dark (particles, thin lines) and dark
           detail on light
  pixels   share of pixels exactly unchanged between frames and the noise in still areas (cameras keep noise everywhere;
           a composite redraws only what moves)
  faces    with --faces: in how many of up to 4 sampled frames a face covers 2%+ of the picture, and the largest face
"""
from __future__ import annotations

import math
import multiprocessing
import os
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from .ff import RnError, probe, source_fps

THUMB_W = 96      # boundary detection
ANALYSIS_W = 480  # per-shot measurement
_IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)


def usable_cpus() -> int:
    """CPUs this process may use: its affinity mask, capped by a cgroup v2 CPU limit (a container usually sees the host's)."""
    try:
        n = len(os.sched_getaffinity(0))
    except AttributeError:
        n = os.cpu_count() or 1
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()[:2]
        if quota != 'max':
            n = min(n, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, n)


def _frames(path: str, display: tuple[int, int], width: int, start: float | None = None, duration: float | None = None):
    """Decoded BGR frames at `width`, every frame passed through (no rate change), so identical frames stay identical."""
    dw, dh = display
    w = max(2, min(width, dw) // 2 * 2)
    h = max(2, int(round(dh * w / dw)) // 2 * 2)
    cmd = ['ffmpeg', '-v', 'error', '-nostdin']
    if start:
        cmd += ['-ss', f'{start:.4f}']
    cmd += ['-i', str(path)]
    if duration is not None:
        cmd += ['-t', f'{max(duration, 0.01):.4f}']
    cmd += ['-an', '-sn', '-vf', f'scale={w}:{h}:flags=area', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-']
    size = w * h * 3
    err = tempfile.TemporaryFile()  # an unread stderr pipe can fill up and stall ffmpeg
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 3))
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
        err.close()


# ---------------------------------------------------------------- boundaries
def _hist(bgr):
    import cv2

    h = cv2.calcHist([cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)], [0, 1, 2], None, [16, 4, 4], [0, 180, 0, 256, 0, 256])
    cv2.normalize(h, h, alpha=1.0, norm_type=cv2.NORM_L1)
    return h.flatten().astype(np.float32)


def _track(path: str, display: tuple[int, int]) -> dict:
    import cv2

    hist_d, pix_d, chg, luma, thumbs = [], [], [], [], []
    prev_h = prev_g = None
    for bgr in _frames(path, display, THUMB_W):
        g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        h = _hist(bgr)
        if prev_h is None:
            hist_d.append(0.0), pix_d.append(0.0), chg.append(0.0)
        else:
            d = np.abs(g - prev_g)
            hist_d.append(float(cv2.compareHist(prev_h, h, cv2.HISTCMP_BHATTACHARYYA)))
            pix_d.append(float(d.mean() / 255.0))
            chg.append(float((d > max(6.0, 0.35 * float(min(g.std(), prev_g.std())))).mean()))
        luma.append(float(g.mean()))
        thumbs.append(g.astype(np.uint8))
        prev_h, prev_g = h, g
    if not luma:
        raise RnError(f'{path}: no frames decoded')
    return {'hist': np.array(hist_d), 'pix': np.array(pix_d), 'chg': np.array(chg), 'luma': np.array(luma), 'thumbs': thumbs}


def _local_median(x: np.ndarray, radius: int) -> np.ndarray:
    out = np.empty_like(x)
    for i in range(len(x)):
        lo, hi = max(0, i - radius), min(len(x), i + radius + 1)
        window = np.concatenate([x[lo:i], x[i + 1:hi]]) if hi - lo > 1 else x[lo:hi]
        out[i] = np.median(window) if len(window) else 0.0
    return out


def _steady_morph(thumbs: list, i: int, k: int) -> bool:
    """The picture changes as fast just before and after [i-k, i+k] as inside it: an animated gradient, not a dissolve."""
    n = len(thumbs)

    def rate(a, b):
        a, b = max(0, a), min(n - 1, b)
        if b - a < k:
            return None
        return float(np.abs(thumbs[b].astype(np.float32) - thumbs[a].astype(np.float32)).mean()) * (2 * k) / (b - a)

    mid = rate(i - k, i + k)
    around = [r for r in (rate(i - 3 * k, i - k), rate(i + k, i + 3 * k)) if r is not None]
    return bool(mid and around and mid < 1.3 * max(around))


def _motion_explains(a: np.ndarray, b: np.ndarray) -> bool:
    """b is mostly a warped copy of a (camera or subject motion), not new content fading in."""
    import cv2

    a8, b8 = np.clip(a, 0, 255).astype(np.uint8), np.clip(b, 0, 255).astype(np.uint8)
    flow = cv2.calcOpticalFlowFarneback(a8, b8, None, 0.5, 3, 9, 3, 5, 1.1, 0)
    h, w = a8.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    warped = cv2.remap(b8, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    before = float(np.abs(b8.astype(np.float32) - a8.astype(np.float32))[4:-4, 4:-4].mean())
    after = float(np.abs(warped.astype(np.float32) - a8.astype(np.float32))[4:-4, 4:-4].mean())
    return after < 0.45 * before


def _flash(luma: np.ndarray, start: int, end: int, fps: float) -> float:
    """A cut opening on a bright flash that decays into the shot: seconds until it settles, or 0."""
    if start < 1 or end - start < 3:
        return 0.0
    before = float(np.median(luma[max(0, start - 3):start]))
    settle = float(np.median(luma[start + 1:end][-max(1, min(end - start - 1, int(fps))):]))
    jump = float(luma[start]) - max(before, settle)
    if jump < 40:
        return 0.0
    k = start
    while k + 1 < end and luma[k + 1] - settle > 0.2 * jump:
        k += 1
    if k == start or k - start > fps * 1.5:
        return 0.0
    return (k + 1 - start) / fps


def boundaries(track: dict, fps: float, *, min_shot: float = 0.35, threshold: float = 0.30) -> list[dict]:
    """[{start_frame, end_frame, transition_in, score}] from a frame track."""
    n = len(track['luma'])
    score = 0.6 * track['hist'] + 0.4 * np.clip(track['pix'] * 4.0, 0, 1)
    score[0] = 0.0
    radius = max(4, int(round(fps * 0.5)))
    base = _local_median(score, radius)
    chg = track['chg']
    chg_base = _local_median(chg, radius)
    found: dict[int, tuple[str, float]] = {}
    for i in range(1, n):  # hard cuts: a spike well above local activity that changes most of the frame
        s = score[i]
        strong = s > threshold and s > base[i] * 2.5 + 0.06 and chg[i] > 0.25
        relative = chg[i] > 0.45 and chg[i] > chg_base[i] * 3 + 0.2 and s > base[i] * 4 + 0.03
        still_swap = (s > threshold and track['hist'][i] > 0.25 and chg[i] > 0.18 and chg_base[i] < 0.01
                      and s > base[i] * 5 + 0.1 and i >= 3 and float(track['pix'][i - 3:i].max()) < 1e-3)
        if strong or relative or still_swap:
            found[i] = ('cut', float(max(s, chg[i])))
    luma = track['luma']  # fades through black / white
    low = min(10.0, float(np.percentile(luma, 50)) * 0.35)
    extreme = (luma < low) | (luma > 248)
    ramps: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if extreme[i]:
            j = i
            while j < n and extreme[j]:
                j += 1
            flat = min(float(track['thumbs'][k].std()) for k in range(i, j)) < 4.0
            fade = 0 < i and j < n and (j - i) <= fps * 2.5 and flat
            # a fade in from or out to black at either end of the video
            end_fade = flat and (i == 0 or j == n) and luma[i] < low
            # the ramp into and out of the extreme: a fast fade's steps look like cuts, and its frames like a dissolve
            a, z = i, j
            while a > 0 and abs(luma[a - 1] - luma[i]) > abs(luma[a] - luma[i]) + 0.5 and i - a < fps * 2:
                a -= 1
            while z < n - 1 and abs(luma[z + 1] - luma[j - 1]) > abs(luma[z] - luma[j - 1]) + 0.5 and z - j < fps * 2:
                z += 1
            # A dip of up to a second is one transition, so its ramps are no dissolve. A longer black or white stretch can
            # hold a shot of its own (text appearing on white), and the ramp into it stays a boundary
            if (fade and (j - i) <= fps) or end_fade:
                ramps.append((a, z))
            if fade:
                for b in [b for b, kind in found.items() if kind[0] == 'cut' and a < b <= z]:
                    del found[b]
                if not any(abs(j - b) < radius for b in found):
                    found[j] = ('fade', 0.5)
            i = j
        else:
            i += 1
    if n > 2 * fps:  # dissolves: the middle frame is a linear blend of the frames k before and k after
        for k in sorted({max(2, int(round(fps * 0.12))), max(3, int(round(fps * 0.2))), max(5, int(round(fps * 0.45)))}):
            cand = []
            for i in range(k, n - k):
                A, B, M = (track['thumbs'][j].astype(np.float32) for j in (i - k, i + k, i))
                D = B - A
                far = float((np.abs(D) > max(6.0, 0.35 * float(min(A.std(), B.std())))).mean())
                if far < 0.15:
                    continue
                denom = float((D * D).sum())
                if denom <= 0:
                    continue
                alpha = float(((M - A) * D).sum() / denom)
                if not 0.2 <= alpha <= 0.8:
                    continue
                resid = float(np.abs(M - (A + alpha * D)).mean() / (np.abs(D).mean() + 1e-6))
                if resid < 0.20 and not _steady_morph(track['thumbs'], i, k) and not _motion_explains(A, B):
                    cand.append((i, resid))
            for i, resid in sorted(cand, key=lambda t: t[1]):
                if any(a <= i <= z for a, z in ramps):  # inside a fade through black / white
                    continue
                near = [b for b in found if abs(i - b) <= 2 * k]
                # A fast dissolve between very different pictures changes enough on one frame to score as a cut. A hard
                # cut has no frame that blends the two sides, so a blend there means the cut was a dissolve
                if all(found[b][0] == 'cut' for b in near):
                    for b in near:
                        del found[b]
                    found[i] = ('dissolve', round(1 - resid, 3))
    min_len = max(1, int(round(min_shot * fps)))
    shots, start, trans, sc = [], 0, None, None
    for b in sorted(found) + [n]:
        if b - start < min_len and b != n:
            continue
        if b == n and shots and b - start < min_len:
            shots[-1]['end_frame'] = n
            break
        shots.append({'start_frame': start, 'end_frame': b, 'transition_in': trans, 'score': sc})
        if b < n:
            trans, sc = found[b][0], round(found[b][1], 3)
        start = b
    for s in shots:
        if s['transition_in'] == 'cut':
            seconds = _flash(luma, s['start_frame'], s['end_frame'], fps)
            if seconds:
                s['transition_in'], s['flash_s'] = 'flash', round(seconds, 3)
    return shots


# ---------------------------------------------------------------- per-shot measurements
def _noise_sigma(gray) -> float:
    """Immerkaer's fast noise estimate on the smoother 60% of the frame."""
    import cv2

    g = gray.astype(np.float32)
    if g.shape[0] < 8 or g.shape[1] < 8:
        return 0.0
    conv = np.abs(cv2.filter2D(g, -1, _IMMERKAER))[1:-1, 1:-1]
    mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0)[1:-1, 1:-1], cv2.Sobel(g, cv2.CV_32F, 0, 1)[1:-1, 1:-1])
    mask = mag < np.percentile(mag, 60)
    if mask.sum() < 100:
        mask = np.ones_like(mag, dtype=bool)
    return float(math.sqrt(math.pi / 2.0) * conv[mask].mean() / 6.0)


def _texture(bgr) -> dict:
    import cv2

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    bh, bw = h // 8, w // 8
    blocks = gray[: bh * 8, : bw * 8].reshape(bh, 8, bw, 8).astype(np.float32)
    bstd, bmean = blocks.std(axis=(1, 3)), blocks.mean(axis=(1, 3))
    dark = bmean < 28
    q = (bgr >> 4).astype(np.int32)
    counts = np.sort(np.bincount(((q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]).ravel(), minlength=4096))[::-1]
    palette95 = int(np.searchsorted(np.cumsum(counts) / max(1, counts.sum()), 0.95) + 1)
    gx, gy = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    strong = mag > 80
    ang = np.degrees(np.arctan2(gy[strong], gx[strong])) % 90.0
    axis = float(((ang < 4) | (ang > 86)).mean()) if strong.sum() > 50 else 0.0
    gf = gray.astype(np.float32)
    lit = gray > 50
    peaks = (gf >= cv2.dilate(gf, np.ones((3, 3), np.uint8))) & (gf - cv2.blur(gf, (7, 7)) > 18) & lit
    inv = (255 - gray).astype(np.float32)
    ink = inv > 50
    ink_peaks = (inv >= cv2.dilate(inv, np.ones((3, 3), np.uint8))) & (inv - cv2.blur(inv, (7, 7)) > 18) & ink
    dn = cv2.medianBlur(gray, 5).astype(np.float32)
    hp = dn - cv2.GaussianBlur(dn, (0, 0), 1.5)
    hp_e = np.sqrt(np.maximum(cv2.blur(hp * hp, (5, 5)), 0))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]
    n_lit, n_ink = int(lit.sum()), int(ink.sum())
    return {
        'flat_blocks': float((bstd < 0.6).mean()), 'palette95': palette95, 'straight_edges': axis,
        'noise_sigma': _noise_sigma(gray), 'fine_texture': float(((hp_e > 0.6) & (hp_e < 6)).mean()),
        'saturation': float(sat.mean() / 255.0), 'saturation_p90': float(np.percentile(sat, 90)) / 255.0,
        'dark_share': float(dark.mean()), 'dark_flat': float((bstd[dark] < 0.6).mean()) if dark.any() else 0.0,
        'light_share': float((bmean > 227).mean()),
        'flat_not_dark': float((bstd[~dark] < 0.6).mean()) if (~dark).any() else 0.0,
        'gentle_slopes': float(((mag > 2) & (mag < 30)).mean()),
        'vivid_share': float(((sat > 90) & (val > 60)).mean()),
        'lit_share': n_lit / float(h * w), 'lit_smooth': float(((mag < 40) & lit).sum()) / n_lit if n_lit else 1.0,
        'ink_share': n_ink / float(h * w), 'ink_smooth': float(((mag < 40) & ink).sum()) / n_ink if n_ink else 1.0,
        'bright_detail_on_dark': 100.0 * float(peaks.sum()) / n_lit if n_lit else 0.0,
        'dark_detail_on_light': 100.0 * float(ink_peaks.sum()) / n_ink if n_ink else 0.0,
    }


def _rigid(prev, nxt) -> dict | None:
    """Similarity transform prev -> nxt from tracked corners, and what is left after warping."""
    import cv2

    pts = cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01, minDistance=7, blockSize=7)
    if pts is None or len(pts) < 15:
        return None
    nxt_pts, st, _ = cv2.calcOpticalFlowPyrLK(prev, nxt, pts, None, winSize=(21, 21), maxLevel=3)
    back, st2, _ = cv2.calcOpticalFlowPyrLK(nxt, prev, nxt_pts, None, winSize=(21, 21), maxLevel=3)
    if nxt_pts is None or back is None:
        return None
    ok = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm(pts - back, axis=2).ravel() < 1.0)
    p0, p1 = pts.reshape(-1, 2)[ok], nxt_pts.reshape(-1, 2)[ok]
    if len(p0) < 12:
        return None
    M, _ = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC, ransacReprojThreshold=1.0, maxIters=2000)
    if M is None:
        return None
    resid = np.linalg.norm(p0 @ M[:, :2].T + M[:, 2] - p1, axis=1)
    h, w = prev.shape
    warped = cv2.warpAffine(prev, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    mh, mw = int(h * 0.08), int(w * 0.08)
    diff = cv2.absdiff(warped, nxt)[mh:h - mh, mw:w - mw].astype(np.float32)
    bh, bw = diff.shape[0] // 16, diff.shape[1] // 16
    blocks = diff[: bh * 16, : bw * 16].reshape(bh, 16, bw, 16).mean(axis=(1, 3)) if bh and bw else diff
    # How far the frame's corners move: the size of the whole-frame motion, zoom and pan alike (a zoom about the centre
    # has almost no translation, so its translation alone is noise)
    corners = np.array([[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]], dtype=np.float64)
    corner = float(np.linalg.norm(corners @ M[:, :2].T + M[:, 2] - corners, axis=1).mean())
    return {'scale': math.hypot(float(M[0, 0]), float(M[1, 0])), 'tx': float(M[0, 2]), 'ty': float(M[1, 2]), 'corner': corner,
            'flow': float(np.median(np.linalg.norm(p1 - p0, axis=1))), 'resid': float(np.median(resid)),
            'inliers': float((resid < 0.5).mean()), 'local': float((resid > 1.5).mean()), 'left': float(np.percentile(blocks, 98))}


def _motion(fits: list[dict], fps: float, px_scale: float) -> dict:
    if not fits:
        return {'pairs': 0, 'rigid_fit': 0.0, 'zoom_per_s': 0.0, 'pan_px_per_s': 0.0, 'pan_dir_deg': 0.0, 'local_motion': 0.0}
    arr = {k: np.array([m[k] for m in fits]) for k in fits[0]}
    zoom = (np.median(arr['scale']) - 1.0) * fps
    tx, ty = np.median(arr['tx']) * fps, np.median(arr['ty']) * fps
    speed = float(math.hypot(tx, ty))
    flow, resid = float(np.median(arr['flow'])), float(np.median(arr['resid']))
    inl, local = float(np.median(arr['inliers'])), float(np.median(arr['local']))
    steadiness = float(np.std(arr['corner']) / (np.mean(arr['corner']) + 1e-6))
    left = float(np.median(arr['left']))
    fit = 0.0
    if (abs(zoom) > 0.004 or speed > 1.5) and flow > 0.08:  # the whole frame moves as one rigid plane
        fit = float(np.clip((inl - 0.80) / 0.17, 0, 1) * np.clip((0.35 - resid) / 0.25, 0, 1) * np.clip((0.06 - local) / 0.05, 0, 1)
                    * np.clip(1.2 - steadiness, 0, 1) ** 0.5 * np.clip((9.0 - left) / 4.0, 0, 1))
    return {'pairs': len(fits), 'rigid_fit': round(fit, 3), 'zoom_per_s': round(float(zoom), 4),
            'pan_px_per_s': round(speed * px_scale, 1), 'pan_dir_deg': round(math.degrees(math.atan2(ty, tx)), 1) if speed > 0 else 0.0,
            'local_motion': round(local, 3), 'rigid_residual_px': round(resid * px_scale, 3)}


FACE_MIN_AREA = 0.02  # a face counts when its box covers at least this share of the picture
_detectors: dict = {}


def _faces(sampled: list, model: str) -> dict:
    """Up to 4 of the sampled frames: in how many a face covers at least FACE_MIN_AREA of the picture, and the largest."""
    import cv2

    detector = _detectors.get(model) or _detectors.setdefault(model, cv2.FaceDetectorYN.create(model, '', (320, 320), 0.6))
    checked = sampled[:: max(1, len(sampled) // 4)]
    largest = []
    for bgr in checked:
        h, w = bgr.shape[:2]
        detector.setInputSize((w, h))
        _, found = detector.detect(bgr)
        largest.append(max((float(row[2] * row[3]) for row in found), default=0.0) / float(w * h) if found is not None else 0.0)
    return {'frames_with_face': round(sum(a >= FACE_MIN_AREA for a in largest) / float(len(checked)), 3) if checked else 0.0,
            'largest_area': round(max(largest, default=0.0), 4), 'checked': len(checked)}


def _measure(job: tuple) -> dict:
    """One shot's measurements (runs in a worker process)."""
    import cv2

    cv2.setNumThreads(1)  # one worker per CPU already
    path, display, shot, fps, face_model = job
    total = shot['end_frame'] - shot['start_frame']
    trim = 2 if total > 12 else 0
    lo, hi = trim, max(trim + 1, total - trim)
    keep = set(np.linspace(lo, hi - 1, num=min(12, hi - lo)).round().astype(int).tolist())
    pairs = set(np.linspace(lo + 1, hi - 1, num=min(20, max(hi - lo - 1, 0))).round().astype(int).tolist()) if hi - lo > 1 else set()
    sampled, mads, zeros, tnoise, fits = [], [], [], [], []
    prev = None
    width = None
    for i, bgr in enumerate(_frames(path, display, ANALYSIS_W, start=shot['start'], duration=shot['end'] - shot['start'])):
        if i >= total:
            break
        width = bgr.shape[1]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        if lo <= i < hi:
            if i in keep:
                sampled.append(bgr.copy())
            if prev is not None and i - 1 >= lo:
                d = cv2.absdiff(gray, prev)
                mads.append(float(d.mean()))
                zeros.append(float((d == 0).mean()))
                still = d < 6
                tnoise.append(float(np.mean(d[still])) if still.any() else 0.0)
                if i in pairs and (fit := _rigid(prev, gray)):
                    fits.append(fit)
        prev = gray
    tex = [_texture(f) for f in sampled] or [_texture(np.zeros((32, 32, 3), np.uint8))]
    texture = {k: round(float(np.median([t[k] for t in tex])), 4) for k in tex[0]}
    texture['palette95'] = int(texture['palette95'])
    mad = np.array(mads) if mads else np.zeros(1)
    same = mad < 0.15
    idx = [i for i, x in enumerate(same.tolist()) if x]
    gaps = np.diff(idx) if len(idx) >= 3 else np.array([])
    if len(gaps):
        values, counts = np.unique(gaps, return_counts=True)
        period, regularity = int(values[np.argmax(counts)]), round(float(counts.max() / len(gaps)), 3)
    else:
        period, regularity = None, 0.0
    faces = _faces(sampled, face_model) if face_model else None
    first_last = 0.0
    if len(sampled) >= 2:
        a, b = (cv2.cvtColor(x, cv2.COLOR_BGR2GRAY).astype(np.float32) for x in (sampled[0], sampled[-1]))
        first_last = float(np.abs(a - b).mean())
    return {
        'still': {'identical_pairs': round(float(same.mean()), 3), 'moving_pairs': round(float((mad > 0.6).mean()), 3),
                  'pair_diff_p90': round(float(np.percentile(mad, 90)), 3), 'first_last_diff': round(first_last, 2)},
        'cadence': {'repeat_share': round(float(same.mean()), 3), 'repeat_period': period, 'repeat_regularity': regularity},
        'motion': _motion(fits, fps, display[0] / float(width or display[0])),
        'texture': texture,
        'pixels': {'unchanged': round(float(np.median(zeros)), 4) if zeros else 0.0,
                   'still_noise': round(float(np.median(tnoise)), 3) if tnoise else 0.0},
        **({'faces': faces} if faces is not None else {}),
    }


def shots_command(path: str, *, min_shot: float = 0.35, threshold: float = 0.30, jobs: int | None = None,
                  sheet: str | None = None, max_kb: int = 125, faces: bool = False, face_model: str | None = None) -> dict:
    import cv2  # noqa: F401  (extra "shots"; a missing module fails here, before any decoding, with the install hint)

    info = probe(path)
    video = info['video']
    if not video or video['still']:
        raise RnError(f'{path}: not a video')
    fps_frac = source_fps(info)
    fps = float(fps_frac)
    width, height = video['width'], video['height']
    if abs(int(video.get('rotation') or 0)) % 180 == 90:
        width, height = height, width
    display = (int(width), int(height))
    track = _track(path, display)
    found = boundaries(track, fps, min_shot=min_shot, threshold=threshold)
    n = len(track['luma'])
    for s in found:
        s['start'], s['end'] = round(s['start_frame'] / fps, 4), round(s['end_frame'] / fps, 4)
    model = None
    if faces:
        from .faces import face_model_path
        model = face_model_path(face_model)
    work = [(path, display, s, fps, model) for s in found]
    workers = max(1, min(jobs or usable_cpus(), len(work)))
    if workers == 1:
        measured = [_measure(job) for job in work]
    else:
        # spawn, not fork: the parent has already run OpenCV, and a forked child can hang on a lock its thread pool held
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn')) as pool:
            measured = list(pool.map(_measure, work))
    shots = []
    for k, (s, m) in enumerate(zip(found, measured)):
        shot = {'shot': k, 'start_frame': s['start_frame'], 'end_frame': s['end_frame'], 'start': s['start'], 'end': s['end'],
                'duration': round((s['end_frame'] - s['start_frame']) / fps, 4), 'transition_in': s['transition_in']}
        if s.get('score') is not None:
            shot['boundary_score'] = s['score']
        if s.get('flash_s'):
            shot['flash_s'] = s['flash_s']
        shots.append({**shot, **m})
    result = {'path': path, 'fps': str(fps_frac), 'frames': n, 'size': list(display), 'workers': workers, 'shots': shots,
              'transitions': {t: sum(1 for s in shots if s['transition_in'] == t) for t in ('cut', 'flash', 'dissolve', 'fade')}}
    if sheet:
        from .frames import sheet_command
        # first, middle and last frame of every shot, one row per shot
        indices = sorted({i for s in shots for i in (s['start_frame'], (s['start_frame'] + s['end_frame']) // 2, s['end_frame'] - 1)})
        result['sheet'] = sheet_command([path], out=sheet, every=None, rate=None, indices=indices, seconds=None, count=None,
                                        width=192, columns=6, max_kb=max_kb)['pages']
    return result


def summary_lines(result: dict) -> list[str]:
    t = result['transitions']
    lines = [f'{len(result["shots"])} shots ({t["cut"]} cuts, {t["flash"]} flash cuts, {t["dissolve"]} dissolves, {t["fade"]} fades), '
             f'measured with {result["workers"]} worker(s)']
    for s in result['shots']:
        m, tx = s['motion'], s['texture']
        lines.append(f'  shot {s["shot"]}: frames {s["start_frame"]}..{s["end_frame"] - 1}  {s["start"]}-{s["end"]}s  '
                     f'in={s["transition_in"] or "start"}  still {s["still"]["identical_pairs"]:.0%}  '
                     f'rigid {m["rigid_fit"]:.2f} zoom {m["zoom_per_s"] * 100:+.1f}%/s pan {m["pan_px_per_s"]:.0f}px/s  '
                     f'flat {tx["flat_blocks"]:.0%} colours {tx["palette95"]} edges {tx["straight_edges"]:.0%} noise {tx["noise_sigma"]:.2f}')
    lines += [f'sheet {page["path"]}' for page in result.get('sheet', [])]
    lines.append('numbers only: what a shot is and how to remake it is yours to judge (look at --sheet)')
    return lines
