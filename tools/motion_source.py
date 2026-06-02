"""Deterministic MOVING H.264 test source for autonomous device-decode testing.

Generates a detailed pattern that scrolls fast (stresses motion compensation),
with a big frame number + an elapsed-ms timestamp burned in, encodes it with the
same low-latency settings as the real stream, and serves it over TCP. The frame
number lets us spot dropped/duplicated/torn frames; the ms timestamp (the Mac's
own clock, read back on the Mac) measures end-to-end latency with no clock sync.

    python3.13 tools/motion_source.py [port] [slices]
        slices: 1 = single slice/frame, 0/omitted = multi-slice (default, like the robot)
"""
import os
import socket
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

# encoder knobs (env) so we can sweep what the Cedar VPU decodes cleanly
E_PRESET = os.environ.get("PRESET", "ultrafast")
E_PROFILE = os.environ.get("PROFILE", "baseline")
E_GOP = os.environ.get("GOP", "")          # "" -> use FPS
E_EXTRA = os.environ.get("EXTRA", "")      # extra -x264-params, e.g. "ref=1:deblock=1:0:0"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 49601
SINGLE_SLICE = len(sys.argv) > 2 and sys.argv[2] == "1"
SPEED = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0   # motion scale: 1.0=extreme stress, ~0.25=realistic
W = int(os.environ.get("MW", "1280"))
H = int(os.environ.get("MH", "720"))
FPS = 30


def make_bg():
    W2 = W * 2
    bg = np.zeros((H, W2, 3), np.uint8)
    xs = np.linspace(0, 255, W2).astype(np.uint8)
    bg[:, :, 0] = xs[None, :]
    bg[:, :, 2] = 255 - xs[None, :]
    bg[:, :, 1] = 90
    for i in range(0, W2, 64):
        cv2.line(bg, (i, 0), (i, H), (255, 255, 255), 2)
    for i in range(0, H, 64):
        cv2.line(bg, (0, i), (W2, i), (0, 0, 0), 2)
    rng = np.random.default_rng(42)
    for _ in range(90):
        x, y = int(rng.integers(0, W2)), int(rng.integers(0, H))
        r = int(rng.integers(12, 48))
        c = tuple(int(v) for v in rng.integers(0, 256, 3))
        cv2.circle(bg, (x, y), r, c, -1)
    return bg


BG = make_bg()


def serve(conn):
    gop = E_GOP if E_GOP else str(FPS)
    base = ["ffmpeg", "-nostdin", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-video_size", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0"]
    if os.environ.get("ENC") == "vt":      # Mac hardware H.264 (VideoToolbox) — NVENC-like bitstream
        cmd = base + [
            "-c:v", "h264_videotoolbox", "-realtime", "1", "-profile:v", E_PROFILE,
            "-pix_fmt", "yuv420p", "-g", gop, "-b:v", os.environ.get("VBR", "12M"),
        ]
    else:
        cmd = base + [
            "-c:v", "libx264", "-preset", E_PRESET, "-tune", "zerolatency",
            "-profile:v", E_PROFILE, "-pix_fmt", "yuv420p", "-g", gop, "-bf", "0",
        ]
        if E_EXTRA:
            cmd += ["-x264-params", E_EXTRA]
        if SINGLE_SLICE:
            cmd += ["-threads", "1"]
    cmd += ["-flush_packets", "1", "-f", "h264", "pipe:1"]
    print("ENC:", " ".join(cmd[cmd.index("-c:v"):]), flush=True)
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stop = threading.Event()

    # foreground "hands": fast, localized, opaque blocks that move chaotically —
    # this is what a waving hand does and what actually stresses motion-comp /
    # exposes slice-boundary corruption (uniform scroll alone is too compressible).
    rng2 = np.random.default_rng(7)
    NHAND = 6
    hx = rng2.integers(0, W, NHAND).astype(float)
    hy = rng2.integers(0, H, NHAND).astype(float)
    hvx = rng2.uniform(-55, 55, NHAND) * SPEED
    hvy = rng2.uniform(-40, 40, NHAND) * SPEED
    hcol = [tuple(int(v) for v in rng2.integers(40, 230, 3)) for _ in range(NHAND)]
    hsz = rng2.integers(70, 150, NHAND)

    def gen():
        nonlocal hx, hy, hvx, hvy
        t0 = time.monotonic()
        n = 0
        while not stop.is_set():
            shift = int(n * 16 * SPEED) % W       # horizontal scroll (scaled by SPEED)
            frame = BG[:, shift:shift + W].copy()
            # move + draw the chaotic foreground blocks
            hx += hvx; hy += hvy
            for k in range(NHAND):
                if hx[k] < 0 or hx[k] > W: hvx[k] = -hvx[k]; hx[k] = min(max(hx[k], 0), W)
                if hy[k] < 0 or hy[k] > H: hvy[k] = -hvy[k]; hy[k] = min(max(hy[k], 0), H)
                x, y, s = int(hx[k]), int(hy[k]), int(hsz[k])
                cv2.rectangle(frame, (x - s, y - s), (x + s, y + s), hcol[k], -1)
                cv2.rectangle(frame, (x - s, y - s), (x + s, y + s), (255, 255, 255), 3)
            ms = int((time.monotonic() - t0) * 1000)
            fsc = 3.5 * H / 720.0
            for txt, y, col in [(f"F {n}", int(130 * H / 720), (0, 255, 0)),
                                (f"{ms} ms", int(250 * H / 720), (0, 255, 255))]:
                cv2.putText(frame, txt, (20, y), cv2.FONT_HERSHEY_SIMPLEX, fsc, (0, 0, 0), 16)
                cv2.putText(frame, txt, (20, y), cv2.FONT_HERSHEY_SIMPLEX, fsc, col, max(2, int(7 * H / 720)))
            try:
                ff.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                break
            n += 1
            nxt = t0 + n / FPS                   # pace to real-time FPS
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)

    threading.Thread(target=gen, daemon=True).start()
    try:
        while True:
            data = ff.stdout.read(4096)
            if not data:
                break
            conn.sendall(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        stop.set()
        try:
            ff.kill()
        except Exception:
            pass
        conn.close()


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(1)
    print(f"motion source on tcp/{PORT} ({'single' if SINGLE_SLICE else 'multi'}-slice, "
          f"scroll 16px/frame, frame# + ms burned in)", flush=True)
    while True:
        conn, addr = s.accept()
        print(f"client {addr}", flush=True)
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
