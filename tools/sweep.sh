#!/bin/bash
# Objectively sweep encoder configs through the Cedar decoder.
# For each config: stream (extreme motion, single-slice) -> capture -> score worst block.
cd "$(dirname "$0")/.."
SPEED=${SPEED:-1.0}; NF=${NF:-10}
OUT=/tmp/sweep_results.txt; : > "$OUT"

cap_stream() {                  # capture ~12s of the running source to /tmp/stream.h264
  python3.13 - <<'EOF'
import socket,time
s=socket.socket(); s.connect(("127.0.0.1",8090))
f=open("/tmp/stream.h264","wb"); t0=time.time()
while time.time()-t0<12:
    d=s.recv(65536)
    if not d: break
    f.write(d)
f.close(); s.close()
EOF
}

run() {
  local label="$1"
  pkill -f motion_source.py 2>/dev/null; sleep 1
  nohup python3.13 tools/motion_source.py 8090 1 "$SPEED" >/tmp/motion.log 2>&1 &
  sleep 2
  cap_stream
  python3.13 tools/autorun.py "$NF" >/dev/null 2>&1
  local w=$(python3.13 tools/score.py 2>/dev/null | grep WORST | grep -oE 'WORST = [0-9.]+' | grep -oE '[0-9.]+')
  printf "%-26s WORST=%s\n" "$label" "$w" | tee -a "$OUT"
}

export PRESET=ultrafast PROFILE=baseline GOP= EXTRA=;                         run "A_ultrafast_base"
export PRESET=veryfast  PROFILE=baseline GOP= EXTRA=;                         run "B_veryfast_base"
export PRESET=medium    PROFILE=baseline GOP= EXTRA=;                         run "C_medium_base"
export PRESET=ultrafast PROFILE=main     GOP= EXTRA=;                         run "D_ultrafast_main"
export PRESET=ultrafast PROFILE=baseline GOP= EXTRA="no-fast-pskip=1:no-dct-decimate=1"; run "E_uf_noskip"
export PRESET=ultrafast PROFILE=baseline GOP=12 EXTRA=;                       run "F_uf_gop12"
export PRESET=veryfast  PROFILE=baseline GOP= EXTRA="me=hex:subme=4:ref=2";   run "G_vf_me"
echo "done" | tee -a "$OUT"
