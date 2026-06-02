#!/bin/sh
# One-time setup on the TrimUI Smart Pro (run via `adb shell` or a device terminal).
#
# Deployment model: the app runs from /mnt/UDISK (fast internal storage) with a
# small launcher tile under /mnt/SDCARD/Apps/Robot. This installs the Python deps
# into the venv the launcher uses.
#
#   adb push src res /mnt/UDISK/             # app code + config
#   adb push hwdecode/build/hwdec_shmem /mnt/UDISK/    # the built C decoder
#   adb push res /mnt/UDISK/                           # HUD font
#   adb push app /mnt/SDCARD/Apps/Robot                # the launcher tile
#   adb shell sh /mnt/UDISK/install_on_device.sh

VENV=/mnt/UDISK/rtvenv
PY="$VENV/bin/python3.11"

# IMPORTANT (display): pip's pygame ships a generic SDL2 whose only video driver
# on this device is "offscreen" -> blank screen. The launcher runs with
# SDL_VIDEODRIVER=mali, which exists only in the *device's* SDL 2.26.5. If you
# build the venv from scratch, replace the libSDL2 bundled inside pygame with the
# device's /usr/trimui/lib/libSDL2-2.0.so.0 (the working setup does exactly this).

"$PY" -m pip install --upgrade pip
# pygame: HUD/display.  numpy+av: only for the SOFTWARE video backend (sw_decode.py).
"$PY" -m pip install pygame numpy av \
  || echo "PyAV (av) may need an aarch64 wheel; the HARDWARE backend works without it."

echo "install_on_device.sh done."
