"""Software H.264 decoder (PyAV) — the 'sw' video backend.

Decodes the same TCP H.264 stream as the C/Cedar decoder, but in software (PyAV
== ffmpeg), and writes the RGB frames into /tmp/hwframe in the EXACT same shared-
memory layout as hwdec_shmem.c. So teleop.py displays it without any change — the
only thing that differs is which process fills the buffer.

Purpose: A/B the Cedar hardware decode (fast, but corrupts on fast motion) against
a clean software decode (no corruption, higher CPU). Pick it with video.backend
= "sw" in settings.json; launch.sh starts this instead of hwdec_shmem.

    sw_decode.py tcp://HOST:PORT [/tmp/hwframe]
"""
import mmap
import os
import struct
import sys
import time

MAXW, MAXH = 1280, 720
NBUF, HDR = 8, 4096                 # must match hwdec_shmem.c / teleop.py
BUFSZ = MAXW * MAXH * 3
TOTAL = HDR + NBUF * BUFSZ
MAGIC = 0x48574D46                  # 'HWMF'

url = sys.argv[1] if len(sys.argv) > 1 else "tcp://127.0.0.1:49601"
path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/hwframe"

fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
os.ftruncate(fd, TOTAL)
mm = mmap.mmap(fd, TOTAL)
struct.pack_into("<6I", mm, 0, MAGIC, 0, 0, 0, 0, 0)
sys.stderr.write("sw_decode shmem %s (%d bytes)\n" % (path, TOTAL))
sys.stderr.flush()

import av  # noqa: E402

OPTS = {"fflags": "nobuffer", "flags": "low_delay", "analyzeduration": "0", "probesize": "8192"}


def run():
    seq = 0
    while True:
        try:
            sys.stderr.write("sw_decode connecting %s\n" % url); sys.stderr.flush()
            container = av.open(url, format="h264", options=OPTS, timeout=8)
            stream = container.streams.video[0]
            # AUTO = multi-core decode. One core is right at ~30fps capacity for real
            # 720p content, so NONE (single-thread) has no headroom and any backlog
            # never drains -> latency creeps up. AUTO gives headroom to stay live.
            # (SW_THREADS=NONE forces single-thread if you ever want to test it.)
            stream.thread_type = os.environ.get("SW_THREADS", "AUTO")
            sys.stderr.write("sw_decode connected\n"); sys.stderr.flush()
            n0 = seq; tr = time.monotonic()
            for frame in container.decode(stream):
                rgb = frame.to_ndarray(format="rgb24")     # (h, w, 3) uint8, contiguous
                h, w = rgb.shape[0], rgb.shape[1]
                if w > MAXW or h > MAXH:
                    rgb = rgb[:MAXH, :MAXW]; h, w = rgb.shape[0], rgb.shape[1]
                off = HDR + (seq & (NBUF - 1)) * BUFSZ
                data = rgb.tobytes()                        # w*h*3, row stride w*3
                mm[off:off + len(data)] = data
                struct.pack_into("<4I", mm, 8, w, h, BUFSZ, 0)   # w,h,bufstride,fmt
                struct.pack_into("<I", mm, 4, seq)               # publish seq LAST
                seq += 1
                t = time.monotonic()
                if t - tr >= 1.0:
                    sys.stderr.write("sw_decode fps=%.1f total=%d %dx%d\n"
                                     % ((seq - n0) / (t - tr), seq, w, h))
                    sys.stderr.flush()
                    n0 = seq; tr = t
            container.close()
        except Exception as e:
            sys.stderr.write("sw_decode err: %s\n" % e); sys.stderr.flush()
            time.sleep(1)


run()
