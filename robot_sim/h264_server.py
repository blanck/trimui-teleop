#!/usr/bin/env python3
"""Low-latency raw-H.264-over-TCP server (Mac side).

Always listens on PORT so the device never hits "connection refused". When a
client connects, spawns ffmpeg to capture the camera and encode zerolatency
baseline H.264 (Annex B), piping the raw bytes straight to the socket. Mirrors
the proven WSAvcPlayer server, but raw TCP for a PyAV client.

    python robot_sim/h264_server.py [cam_index] [width] [height] [fps] [port]
    python robot_sim/h264_server.py 0 1280 720 20 49601
"""
import os
import socket
import subprocess
import sys
import threading

CAM = sys.argv[1] if len(sys.argv) > 1 else "0"        # avfoundation device index
WIDTH = sys.argv[2] if len(sys.argv) > 2 else "1280"
HEIGHT = sys.argv[3] if len(sys.argv) > 3 else "720"
FPS = sys.argv[4] if len(sys.argv) > 4 else "20"
PORT = int(sys.argv[5]) if len(sys.argv) > 5 else 49601
GOP = sys.argv[6] if len(sys.argv) > 6 else FPS   # short GOP = frequent keyframes = low live-edge latency
SCALE = sys.argv[7] if len(sys.argv) > 7 else None  # e.g. "960x540" to downscale before encode (eases device decode)
if SCALE in ("none", "-", ""):
    SCALE = None
TRANSPOSE = sys.argv[8] if len(sys.argv) > 8 else None  # ffmpeg transpose (1=90°CW, 2=90°CCW) for portrait panels
PRESET = os.environ.get("PRESET", "veryfast")           # x264 preset: better compression than ultrafast, same latency
PROFILE = os.environ.get("PROFILE", "main")             # main (CABAC) compresses better than baseline at same bits
CRF = os.environ.get("CRF", "18")                       # quality target: lower = sharper (more bits) on motion


def serve_client(conn, addr):
    print(f"client connected {addr} -> starting ffmpeg", flush=True)
    if CAM == "test":                    # reliable synthetic moving source (no camera flakiness)
        cmd = [
            "ffmpeg", "-nostdin", "-re",   # -re: emit at realtime (lavfi otherwise floods as fast as it can encode)
            "-f", "lavfi", "-i", f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}",
            "-r", FPS,
        ]
    else:
        cmd = [
            "ffmpeg", "-nostdin",
            "-f", "avfoundation", "-framerate", "30",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-i", CAM,
            "-r", FPS,                   # encode rate (native 30 keeps the camera in its sweet spot)
        ]
    filters = []
    if TRANSPOSE:                        # rotate at source for the portrait panel
        filters.append(f"transpose={TRANSPOSE}")
    if SCALE:                            # downscale before encode so the device can decode it at full fps
        w, h = SCALE.lower().split("x")
        filters.append(f"scale={w}:{h}")
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", GOP, "-bf", "0",
        "-threads", "1",                # REQUIRED: 1 slice/frame. zerolatency otherwise
                                        # slices each frame -> Cedar VPU band corruption.
        "-flush_packets", "1",          # send each packet immediately, no output buffering
        "-f", "h264", "pipe:1",
    ]
    ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=open("/tmp/ff_client.err", "wb"))
    try:
        while True:
            chunk = ff.stdout.read(4096)
            if not chunk:
                break
            conn.sendall(chunk)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            ff.kill()
        except Exception:
            pass
        conn.close()
        print(f"client gone {addr} -> ffmpeg stopped", flush=True)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(1)
    print(f"h264 server listening on tcp/{PORT} (cam {CAM} {WIDTH}x{HEIGHT}@{FPS})", flush=True)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=serve_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
