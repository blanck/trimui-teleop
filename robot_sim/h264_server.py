#!/usr/bin/env python3
"""Low-latency raw-H.264-over-TCP server for the TrimUI teleop client.

Always listens on PORT so the device never hits "connection refused". When a
client connects, spawns ffmpeg to capture the camera and encode zerolatency
baseline H.264 (Annex B), piping the raw bytes straight to the socket.

    python robot_sim/h264_server.py [cam] [width] [height] [fps] [port]
    python robot_sim/h264_server.py 0 1280 720 20 49601              # Mac avfoundation index
    python robot_sim/h264_server.py /dev/video2 1280 720 15 49601  # Linux Arducam H264 passthrough
    python robot_sim/h264_server.py /dev/video0 1280 720 15 49601 none  # Linux MJPEG encode
    python robot_sim/h264_server.py test 1280 720 20 49601          # synthetic test pattern

On Linux, /dev/video2 tries hardware H264 passthrough first and falls back to
/dev/video0 MJPEG encode when capture fails.
"""
import os
import select
import socket
import subprocess
import sys
import threading

CAM = sys.argv[1] if len(sys.argv) > 1 else ("0" if sys.platform == "darwin" else "/dev/video2")
WIDTH = sys.argv[2] if len(sys.argv) > 2 else "1280"
HEIGHT = sys.argv[3] if len(sys.argv) > 3 else "720"
FPS = sys.argv[4] if len(sys.argv) > 4 else "20"
PORT = int(sys.argv[5]) if len(sys.argv) > 5 else 49601
GOP = sys.argv[6] if len(sys.argv) > 6 else FPS
SCALE = sys.argv[7] if len(sys.argv) > 7 else None
if SCALE in ("none", "-", ""):
    SCALE = None
TRANSPOSE = sys.argv[8] if len(sys.argv) > 8 else None
FALLBACK_DEVICE = os.environ.get("FALLBACK_DEVICE", "/dev/video0")
FALLBACK_SCALE = os.environ.get("FALLBACK_SCALE", "960x540")
FFMPEG_START_SECONDS = 3.0
TCP_SEND_BUFFER = 65536
LOW_DELAY_INPUT = ["-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0"]
PRESET = os.environ.get("PRESET", "veryfast")
PROFILE = os.environ.get("PROFILE", "main")
CRF = os.environ.get("CRF", "18")


_active = {"ff": None, "conn": None}
_lock = threading.Lock()


def camera_device():
    if CAM == "test":
        return CAM
    if sys.platform != "darwin" and CAM.isdigit():
        return f"/dev/video{CAM}"
    return CAM


def use_camera_h264():
    if sys.platform == "darwin" or CAM == "test":
        return False
    device = camera_device()
    return device == "/dev/video2" or os.environ.get("H264_PASSTHROUGH", "").lower() in ("1", "true", "yes")


def tune_tcp(conn):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, TCP_SEND_BUFFER)


def capture_input_cmd(device, passthrough, scale):
    if CAM == "test":
        return [
            "ffmpeg", "-nostdin", "-re",
            "-f", "lavfi", "-i", f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}",
            "-r", FPS,
        ], False
    if sys.platform == "darwin":
        return [
            "ffmpeg", "-nostdin",
            "-f", "avfoundation", "-framerate", "30",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-i", CAM,
            "-r", FPS,
        ], False
    if passthrough:
        return [
            "ffmpeg", "-nostdin", *LOW_DELAY_INPUT,
            "-f", "v4l2", "-input_format", "h264",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", FPS,
            "-i", device,
        ], True
    return [
        "ffmpeg", "-nostdin", *LOW_DELAY_INPUT,
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", FPS,
        "-i", device,
        "-r", FPS,
    ], False


def append_output_cmd(cmd, passthrough, scale):
    if passthrough:
        cmd += ["-c", "copy", "-flush_packets", "1", "-f", "h264", "pipe:1"]
        return cmd
    filters = []
    if TRANSPOSE:
        filters.append(f"transpose={TRANSPOSE}")
    if scale:
        width, height = scale.lower().split("x")
        filters.append(f"scale={width}:{height}")
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", GOP, "-bf", "0",
        "-threads", "1",
        "-flush_packets", "1",
        "-f", "h264", "pipe:1",
    ]
    return cmd


def capture_attempts():
    if CAM == "test" or sys.platform == "darwin":
        return [(camera_device(), False, SCALE)]
    if use_camera_h264():
        return [(camera_device(), True, None), (FALLBACK_DEVICE, False, FALLBACK_SCALE)]
    return [(camera_device(), False, SCALE)]


def stream_ffmpeg(conn, cmd):
    ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=open("/tmp/ff_client.err", "ab"))
    with _lock:
        _active["ff"] = ff
    try:
        readable, _, _ = select.select([ff.stdout], [], [], FFMPEG_START_SECONDS)
        if not readable:
            return False
        chunk = ff.stdout.read(4096)
        if not chunk or ff.poll() is not None:
            return False
        conn.sendall(chunk)
        while True:
            chunk = ff.stdout.read(4096)
            if not chunk:
                break
            conn.sendall(chunk)
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return True
    finally:
        with _lock:
            if _active["ff"] is ff:
                _active["ff"] = None
        try:
            ff.kill()
        except Exception:
            pass


def serve_client(conn, addr):
    tune_tcp(conn)
    print(f"client connected {addr} -> starting ffmpeg", flush=True)
    with _lock:
        old_ff, old_conn = _active["ff"], _active["conn"]
        if old_ff:
            try:
                old_ff.kill()
                old_ff.wait(timeout=2)
            except Exception:
                pass
        if old_conn is not None and old_conn is not conn:
            try:
                old_conn.close()
            except Exception:
                pass
        _active["ff"], _active["conn"] = None, conn
    try:
        for device, passthrough, scale in capture_attempts():
            mode = "h264 passthrough" if passthrough else "mjpeg encode"
            print(f"trying {mode} from {device}", flush=True)
            cmd, passthrough = capture_input_cmd(device, passthrough, scale)
            cmd = append_output_cmd(cmd, passthrough, scale)
            if stream_ffmpeg(conn, cmd):
                print(f"streaming {mode} from {device}", flush=True)
                return
            print(f"{mode} from {device} failed, trying fallback", flush=True)
    finally:
        conn.close()
        print(f"client gone {addr} -> ffmpeg stopped", flush=True)


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(1)
    mode = "h264 passthrough" if use_camera_h264() else "encode"
    print(f"h264 server listening on tcp/{PORT} ({mode}, cam {camera_device()} {WIDTH}x{HEIGHT}@{FPS})", flush=True)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=serve_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
