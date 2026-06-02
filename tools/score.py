"""Objective Cedar-corruption scorer (conversion-invariant).

H.264 luma/chroma decode is normative, so a CORRECT Cedar frame is bit-identical
to ffmpeg's. The earlier mistake was comparing Cedar's RGB to ffmpeg's RGB — the
two use different YUV->RGB, so every edge differed. Here we decode the same stream
with ffmpeg to planar YUV420 and apply the decoder's EXACT integer BT601 conversion
(replicated below), so the conversion cancels and any residual is real corruption.

Match is by content: the synthetic source repeats deterministically per frame#, so
each Cedar capture is compared against the whole ffmpeg bank and we take the best
match. Clean -> ~0; corrupt staircase -> a few 16x16 blocks with large error.

    python3.13 tools/score.py [/tmp/stream.h264]
"""
import glob
import subprocess
import sys

import cv2
import numpy as np
import glob as _glob

# auto-detect resolution from a captured frame (so any source size works)
_caps = sorted(_glob.glob("/tmp/auto_*.png"))
if _caps:
    _im = cv2.imread(_caps[0])
    H, W = _im.shape[0], _im.shape[1]
else:
    W, H = 1280, 720


def yuv_to_rgb(Y, U, V):                 # EXACT replica of hwdec_shmem.c yuv420_to_rgb
    c = Y.astype(np.int32)
    d = (U.astype(np.int32) - 128).repeat(2, 0).repeat(2, 1)[:H, :W]
    e = (V.astype(np.int32) - 128).repeat(2, 0).repeat(2, 1)[:H, :W]
    r = c + ((1436 * e) >> 10)
    g = c - ((352 * d + 731 * e) >> 10)
    b = c + ((1814 * d) >> 10)
    return np.clip(np.stack([b, g, r], -1), 0, 255).astype(np.uint8)  # BGR for cv2


def block_worst(diff, bs=16):
    d = np.abs(diff).mean(2)
    d[0:int(H * 0.45), 0:int(W * 0.5)] = 0   # mask burned-in F#/ms text (differs per connection)
    h2, w2 = H // bs * bs, W // bs * bs
    return d[:h2, :w2].reshape(h2 // bs, bs, w2 // bs, bs).mean(axis=(1, 3)).max()


def main():
    stream = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stream.h264"
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "h264",
                          "-i", stream, "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         capture_output=True).stdout
    fsz = W * H * 3 // 2
    refs = []
    for i in range(len(raw) // fsz):
        b = raw[i * fsz:(i + 1) * fsz]
        Y = np.frombuffer(b[:W * H], np.uint8).reshape(H, W)
        U = np.frombuffer(b[W * H:W * H + W * H // 4], np.uint8).reshape(H // 2, W // 2)
        V = np.frombuffer(b[W * H + W * H // 4:], np.uint8).reshape(H // 2, W // 2)
        refs.append(yuv_to_rgb(Y, U, V).astype(np.int16))
    if not refs:
        print("no reference frames"); return
    worst = 0.0
    for cf in sorted(glob.glob("/tmp/auto_*.png")):
        c = cv2.imread(cf).astype(np.int16)
        best = min(block_worst(c - r) for r in refs)
        worst = max(worst, best)
        print(f"{cf.split('/')[-1]}: worst-block err = {best:6.1f}")
    print(f"=> WORST = {worst:.1f}  (clean <~8, corrupt >40)")


if __name__ == "__main__":
    main()
