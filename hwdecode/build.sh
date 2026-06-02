#!/bin/sh
# Cross-compile the Cedar hardware H.264 decoder (hwdec_shmem) for the TrimUI
# Smart Pro: aarch64, glibc 2.33.
#
# Needs:
#   - zig            https://ziglang.org  (used as the cross C compiler)
#   - cedar/include  vendored Allwinner libcedarc headers (in this repo)
#   - cedar/libs     the device .so blobs — NOT in the repo; see cedar/README.md
#
#   ZIG=/path/to/zig ./build.sh      # output: build/hwdec_shmem
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ZIG="${ZIG:-zig}"
INC="$DIR/cedar/include"
LIBS="$DIR/cedar/libs"
OUT="$DIR/build"
mkdir -p "$OUT"

"$ZIG" cc -target aarch64-linux-gnu.2.33 -I"$INC" -O2 \
  -o "$OUT/hwdec_shmem" "$DIR/hwdec_shmem.c" \
  -L"$LIBS" -lvdecoder -lVE -lMemAdapter -lvideoengine -lcdc_base \
  -Wl,--allow-shlib-undefined -lpthread -ldl -lm

echo "built $OUT/hwdec_shmem"
