#!/bin/sh
# TrimUI Teleop launcher.
#
# The app (src/teleop.py) discovers the robot on the LAN by itself and owns the
# video decoder process (hardware or software, per settings.json / the in-app
# menu), so this script just runs it. MainUI blocks here while it runs.
cd /mnt/UDISK

# clear any stray decoder from a previous run
kill $(pidof hwdec_shmem) 2>/dev/null; pkill -9 -f src/sw_decode.py 2>/dev/null

SDL_VIDEODRIVER=mali LD_LIBRARY_PATH=/usr/trimui/lib HOME=/root \
  ./rtvenv/bin/python3.11 src/teleop.py --settings /mnt/UDISK/settings.json \
  >/tmp/teleop.log 2>&1

# cleanup on exit
kill $(pidof hwdec_shmem) 2>/dev/null; pkill -9 -f src/sw_decode.py 2>/dev/null
