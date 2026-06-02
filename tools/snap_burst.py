"""Capture N CONSECUTIVE decoded frames from /tmp/hwframe on the device, each at a
new seq (verified-read), saved as /tmp/burst_<i>.ppm. Shows true frame-to-frame
motion smoothness (no per-frame adb round-trip between them).

    python3 snap_burst.py [N]
"""
import mmap
import os
import struct
import sys
import time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
NBUF, HDR = 8, 4096
fd = os.open("/tmp/hwframe", os.O_RDONLY)
mm = mmap.mmap(fd, os.fstat(fd).st_size, prot=mmap.PROT_READ)

last = -1
got = 0
deadline = time.time() + 10
while got < N and time.time() < deadline:
    for _try in range(8):
        _, seq, w, h, bs, _ = struct.unpack_from("<6I", mm, 0)
        if not (w and h) or seq == last:
            break
        off = HDR + (seq & (NBUF - 1)) * bs
        cand = mm[off:off + w * h * 3]
        _, seq2, _, _, _, _ = struct.unpack_from("<6I", mm, 0)
        if ((seq2 - seq) & 0xffffffff) < NBUF:
            with open(f"/tmp/burst_{got}.ppm", "wb") as f:
                f.write(b"P6\n%d %d\n255\n" % (w, h))
                f.write(cand)
            sys.stderr.write("burst %d seq=%d\n" % (got, seq))
            last = seq
            got += 1
            break
    time.sleep(0.012)
sys.stderr.write("done %d frames\n" % got)
