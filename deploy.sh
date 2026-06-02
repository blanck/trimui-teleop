#!/bin/bash
# Deploy Teleop to a USB-connected TrimUI Smart Pro.
#
# Prereqs (one time): the Python venv at /mnt/UDISK/rtvenv with the device's mali SDL
# (see README "Deploy") and, for the hardware decoder, build it first:
#   ZIG=$(command -v zig) ./hwdecode/build.sh
#
#   ADB=/path/to/adb ./deploy.sh     # ADB defaults to `adb` on PATH
set -e
ADB="${ADB:-adb}"
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
command -v "$ADB" >/dev/null || { echo "adb not found — set ADB=/path/to/adb"; exit 1; }

"$ADB" wait-for-device
"$ADB" shell 'mkdir -p /mnt/UDISK/src /mnt/UDISK/res /mnt/SDCARD/Apps/Teleop'

# app runtime -> /mnt/UDISK (fast internal storage)
"$ADB" push src/teleop.py src/sw_decode.py src/discovery.py src/robot_link.py \
            src/config.py src/controller.py /mnt/UDISK/src/
"$ADB" push res/ShareTechMono.ttf res/icon.png /mnt/UDISK/res/
if [ -f hwdecode/build/hwdec_shmem ]; then
  "$ADB" push hwdecode/build/hwdec_shmem /mnt/UDISK/hwdec_shmem
  "$ADB" shell 'chmod +x /mnt/UDISK/hwdec_shmem'
else
  echo "note: no hwdecode/build/hwdec_shmem — the SOFTWARE backend still works."
  echo "      run ./hwdecode/build.sh to add the hardware (Cedar) backend."
fi

# launcher tile -> /mnt/SDCARD/Apps/Teleop
"$ADB" push app/config.json app/launch.sh app/icon.png /mnt/SDCARD/Apps/Teleop/
"$ADB" shell 'chmod +x /mnt/SDCARD/Apps/Teleop/launch.sh'

echo
echo "Deployed. First time, set up the venv on the device (see README 'Deploy'):"
echo "  /mnt/UDISK/rtvenv/bin/python3.11 -m pip install pygame numpy av"
echo "  (and swap pygame's bundled libSDL2 for the device's mali one — see README)."
echo "Then launch 'Teleop' from the Apps menu (it finds the robot automatically)."
