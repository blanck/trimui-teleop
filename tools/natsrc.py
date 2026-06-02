"""Natural-content H.264 test source: ffmpeg mandelbrot (smooth detail + coherent
continuous motion, much closer to real camera video than hard synthetic blocks).
Same low-latency single-slice encode as the robot. Serves over TCP/49601.

    python3 robot_sim/natsrc.py [port] [src]
        src: mandelbrot (default) | smptebars | rgbtestsrc | a /path/to/video.mp4
"""
import socket
import subprocess
import sys
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 49601
SRC = sys.argv[2] if len(sys.argv) > 2 else "mandelbrot"
W, H, FPS = 1280, 720, 30


def serve(conn):
    if SRC.startswith("/"):
        ins = ["-stream_loop", "-1", "-re", "-i", SRC]
    else:
        ins = ["-f", "lavfi", "-i", f"{SRC}=size={W}x{H}:rate={FPS}"]
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *ins,
           "-vf", f"scale={W}:{H},fps={FPS}",
           "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
           "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", str(FPS), "-bf", "0",
           "-threads", "1", "-flush_packets", "1", "-f", "h264", "pipe:1"]
    ff = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            d = ff.stdout.read(4096)
            if not d:
                break
            conn.sendall(d)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            ff.kill()
        except Exception:
            pass
        conn.close()


s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", PORT))
s.listen(1)
print(f"natural source ({SRC}) on tcp/{PORT}", flush=True)
while True:
    conn, _ = s.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
