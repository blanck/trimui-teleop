"""Device-side atomic snapshot of /tmp/hwframe.

Runs ON the TrimUI. Reads the shared-memory frame the SAME way the pygame app
does (verified-read: grab seq, copy buf[seq&(NBUF-1)], re-check seq didn't lap),
so the snapshot reflects exactly what the app would display — not the torn mess
you get from `adb pull`ing the whole 22 MB ring while the decoder laps it.

Writes a binary PPM to argv[1] (default /tmp/snap.ppm). Stdlib only (no numpy).

    python3 snap.py /tmp/snap.ppm
"""
import mmap
import os
import struct
import sys
import time

PATH = "/tmp/hwframe"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/snap.ppm"
NBUF = 8
HDR = 4096

fd = os.open(PATH, os.O_RDONLY)
sz = os.fstat(fd).st_size
mm = mmap.mmap(fd, sz, prot=mmap.PROT_READ)

# wait for a live frame
for _ in range(200):
    magic, seq, w, h, bs, fmt = struct.unpack_from("<6I", mm, 0)
    if w and h and seq:
        break
    time.sleep(0.02)

fb = None
for _try in range(8):                      # verified read, same as teleop.py
    magic, seq, w, h, bs, fmt = struct.unpack_from("<6I", mm, 0)
    off = HDR + (seq & (NBUF - 1)) * bs
    cand = mm[off:off + w * h * 3]          # bytes COPY (snapshot this buffer)
    _, seq2, _, _, _, _ = struct.unpack_from("<6I", mm, 0)
    if ((seq2 - seq) & 0xffffffff) < NBUF:  # writer didn't lap us mid-copy
        fb = cand
        break
    time.sleep(0.005)

if fb is None:
    sys.stderr.write("snap: could not get a stable frame\n")
    sys.exit(1)

with open(OUT, "wb") as f:
    f.write(b"P6\n%d %d\n255\n" % (w, h))
    f.write(fb)
sys.stderr.write("snap seq=%d %dx%d -> %s\n" % (seq, w, h, OUT))
