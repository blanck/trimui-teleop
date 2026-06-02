"""Autonomous device-decode test: start the decoder over adb, pull its decoded
frames from shared memory, write them as PNGs, and print frame#/seq so we can
spot corruption, skips, and latency — all with zero device interaction.

    python3.13 tools/autorun.py [n_frames]
"""
import os
import struct
import subprocess
import sys
import time

import cv2
import numpy as np

ADB = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
DEV_IP = "10.0.0.117"
PORT = 49601
NBUF = 8
HDR = 4096
N = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def adb(*args, t=30):
    return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=t)


def sh(cmd, t=30):
    return adb("shell", cmd, t=t)


def main():
    subprocess.run([ADB, "wait-for-device"], timeout=40)
    # fresh decoder
    sh("kill -9 $(pidof hwdec_shmem) 2>/dev/null; rm -f /tmp/hwframe; sleep 1")
    # NOTE: the trailing `sleep 4` keeps the adb shell session alive while the
    # setsid'd decoder execs + connects. Without it adb tears down the session
    # immediately and the backgrounded decoder never comes up.
    sh("cd /mnt/UDISK; setsid env LD_LIBRARY_PATH=/usr/lib:/usr/trimui/lib "
       f"./hwdec_shmem {DEV_IP} {PORT} /tmp/hwframe >/tmp/hwdec.log 2>&1 </dev/null & sleep 4",
       t=12)
    started = sh("echo pid:[$(pidof hwdec_shmem)]").stdout
    print(started.strip().splitlines()[-1] if started.strip() else "pid:[?]")
    os.system("rm -f /tmp/auto_*.png /tmp/snap_*.ppm")
    # push the device-side atomic snapshot reader
    here = os.path.dirname(os.path.abspath(__file__))
    adb("push", os.path.join(here, "snap.py"), "/tmp/snap.py")
    seqs = []
    for i in range(N):
        # snapshot ON the device (verified-read of one buffer) then pull the
        # small stable PPM — no ring-lapping tearing from the transfer.
        r = sh("/root/.venv/bin/python3 /tmp/snap.py /tmp/snap.ppm 2>&1; echo done", t=15)
        adb("pull", "/tmp/snap.ppm", f"/tmp/snap_{i}.ppm", t=15)
        time.sleep(0.25)
    log = sh("tail -3 /tmp/hwdec.log").stdout
    sh("kill $(pidof hwdec_shmem) 2>/dev/null")
    for i in range(N):
        try:
            buf = cv2.imread(f"/tmp/snap_{i}.ppm", cv2.IMREAD_COLOR)  # PPM is RGB->cv2 BGR? handled below
            if buf is None:
                print(f"frame {i}: no PPM")
                continue
            # our PPM bytes are RGB (decoder writes RGB24); cv2.imread reads PPM as RGB->stored BGR already
            cv2.imwrite(f"/tmp/auto_{i}.png", buf)
            print(f"frame {i}: -> /tmp/auto_{i}.png")
        except Exception as e:
            print(f"frame {i}: err {e}")
    print("=== decoder log ===")
    print("\n".join(l for l in log.splitlines() if "fps" in l or "exit" in l or "fault" in l))


if __name__ == "__main__":
    main()
