# Hardware H.264 decode on the TrimUI Smart Pro (A133P) — build spec

Goal: replace the Python/PyAV **software** H.264 decode with the A133P's **Cedar
VPU hardware decoder**, so we can run 720p@30/60 at ~0% CPU and lower latency.

## Status: Phase 1 DONE — hardware decode works (720p@30, 3.4% CPU)

`hwdecode/hwdec_test.c` cross-compiles with `hwdecode/build.sh` and decodes the
TCP H.264 stream on the Cedar VPU: 1280x720 @ ~30fps, **3.4% CPU**, output
PIXEL_FORMAT fmt=1 (planar YUV420), physical addrs in `VideoPicture.phyYBufAddr/
phyCBufAddr`. Key gotchas solved: call `AddVDPlugin()` first; set
`nVbvBufferSize`; feed **one complete H.264 access unit per `SubmitVideoStreamData`**
(split the stream at VCL-NAL picture boundaries — arbitrary TCP chunks → NO_BITSTREAM).
Next: Phase 2 disp2 video-layer display from those phys addrs.

### ✅ De-risked / proven
- **Cross-compile toolchain**: `zig cc -target aarch64-linux-gnu.2.33` on macOS
  produces binaries that run on the device (verified with a hello-world).
  zig at `/opt/homebrew/bin/zig` (0.16.0).
- **Device target**: glibc 2.33, kernel 4.9.191 (Allwinner BSP, NOT mainline →
  no V4L2 codec node; must use vendor CedarX).
- **Codec stack present**: `/dev/cedar_dev` + `/dev/ion`; libs in /usr/lib:
  libvdecoder, libvideoengine, libVE, libMemAdapter, libcdc_base, libcdx_*.
  CedarC **v1.3.0** (A133P BSP).
- **Headers**: `aodzip/libcedarc` matches — pulled vdecoder.h, typedef.h,
  sc_interface.h, veInterface.h into /tmp/cedar/include (re-fetch if /tmp wiped:
  raw.githubusercontent.com/aodzip/libcedarc/master/include/<h>).
- **Display path**: `/dev/disp` (Allwinner disp2 engine) — hardware video-layer
  overlay, consumes the decoded frame's **physical addresses** (zero-copy).

### Init recipe (the finicky part — now known)
```c
VeOpsS*       veOps  = GetVeOpsS();              // libVE.so
struct ScMemOpsS* memops = MemAdapterGetOpsS();  // libMemAdapter.so (ION); also __GetIonMemOpsS
VConfig vc; memset(&vc,0,sizeof vc);
vc.memops = memops; vc.veOpsS = veOps; vc.pVeOpsSelf = /* VE self, see CdcVeInit */;
vc.eOutputPixelFormat = /* NV12 */; vc.bDisable3D = 1; vc.bNoBFrames = 1;
vc.nFrameBufferNum = ...; vc.nVbvBufferSize = ...;
VideoStreamInfo si; memset(&si,0,sizeof si); si.eCodecFormat = VIDEO_CODEC_FORMAT_H264;
VideoDecoder* dec = CreateVideoDecoder();
InitializeVideoDecoder(dec, &si, &vc);
// loop:
//   RequestVideoStreamBuffer(dec, len, &buf, &bufSz, &ringBuf, &ringBufSz, 0)
//   memcpy(buf, nalData, len)  (handle ring-buffer wrap)
//   VideoStreamDataInfo di = {.pData=buf,.nLength=len,.bIsFirstPart=1,.bIsLastPart=1,.nPts=...};
//   SubmitVideoStreamData(dec, &di, 0);
//   DecodeVideoStream(dec, 0,0,0, 0);
//   if (ValidPictureNum(dec,0)>0){ VideoPicture* p = RequestPicture(dec,0);
//        /* disp2: show p->phyYBufAddr / p->phyCBufAddr, p->nWidth/nHeight/nLineStride */
//        ReturnPicture(dec,p); }
```
`VideoPicture` (vdecoder.h:337) exposes `phyYBufAddr`, `phyCBufAddr`, `nBufFd`
(dmabuf), `nWidth/nHeight/nLineStride`, `ePixelFormat` — everything disp2 needs.

## Phase 2 inputs gathered (display)
- Header: `sunxi_display2.h` (H6 BSP 4.9 mirror) in /tmp/cedar/include.
- ioctl `DISP_LAYER_SET_CONFIG = 0x47` (non-2) / `DISP_LAYER_SET_CONFIG2 = 0x49`.
  Arg convention: `unsigned long ub[4] = {screen_id, (ulong)&cfg, layer_count, 0}`.
- `disp_fb_info.addr[3]` = **physical** Y/U/V (matches decoder phyYBufAddr; planar
  YUV420 → format `DISP_FORMAT_YUV420_P=0x48`). config2 uses a dmabuf `fd`
  (VideoPicture.nBufFd) instead.
- **Live display state (`/sys/class/disp/disp/attr/sys`):** panel `mgr0: 720x1280`
  portrait, colorspace `0x204` (BT601 full). **MainUI layer = ch1, lyr0, z16,
  global alpha 255, addr fb800000.**
- **ROTATION**: content is 1280x720 landscape but panel is 720x1280 portrait → use
  decoder rotation (`VConfig.bRotationEn=1; nRotateDegree=90`) so output is 720x1280
  and drops straight into a fullscreen disp layer. Put video on **ch0** (below UI)
  or stop MainUI for a fullscreen test. crop is `disp_rect64` (32.32 fixed: val<<32).
- color_space `DISP_BT601`, layer mode `LAYER_MODE_BUFFER=0`, alpha_mode 1 / value 255.
- Cache: decoder frame is VPU-written; for disp (which DMAs from phys addr) it should
  be coherent, but watch for needing CdcMemFlushCache before display.

## Remaining work (the actual build)
1. **Phase 1 — decode test**: C prog: connect TCP 49601 → split NAL/access units →
   feed CedarX → count `RequestPicture` frames + measure fps/CPU. Validates init
   + ABI (struct layouts). Expect several on-device debug cycles here.
2. **Phase 2 — disp2 display**: open `/dev/disp`, `DISP_LAYER_SET_CONFIG` a scaler
   video layer from the YUV phys addrs, fullscreen, hw-scaled. `ReturnPicture`
   after the frame is shown. (Need sunxi_display2.h uapi — from the BSP.)
3. **Phase 3 — integrate**: UDP steering + HUD. Either a 2nd disp UI layer
   composited above the video layer, or fold steering into the C engine and keep
   a minimal pygame HUD layer.

## Risks / unknowns
- **ABI match**: aodzip headers vs the device's exact v1.3.0 structs. Phase 1
  flushes this out (garbage/crash if a struct is off). Mitigate by checking
  sizeof and key field offsets against decoder behavior.
- **disp2 layer config**: many fields; getting the format/zorder/scaler right is
  fiddly. The stock UI uses it, so it's possible.
- **Toolchain symbol resolution**: link against the pulled .so in /tmp/cedar/libs
  (`-L/tmp/cedar/libs -lvdecoder -lVE -lMemAdapter ...`); confirm exact memops
  getter symbol with `nm -D` (zig ships llvm-nm).

## Phase 3 progress (hwdecode/hwdec_test.c — the integrated standalone)
WORKS (proven):
- Hardware decode + **disp2 display on ch0** (z20, above MainUI's ch1) — video shows
  on screen, correct orientation (source transposed: server arg `transpose=2`).
- **On-screen HUD** (CPU% + FPS) drawn white onto the Y plane, rotated 90° + drawn
  back-to-front so it reads upright in landscape; black bg box prevents smear.
  Verified via save-mode capture (`/tmp/hud_landscape.png`). memops for the cache
  flush comes from `MemAdapterGetOpsS()` (vc.memops is nil).
- **Gamepad quit**: reads /dev/input/event3 (TRIMUI Player1); any button → clean exit.
- **fb0 screenshot** of the UI works: `dd if=/dev/fb0` → 1280x720 BGRA (UI/ch1 only,
  NOT the ch0 video overlay).

NOT SOLVED (the tar pit — compounding platform bugs, do NOT keep patching blind):
- **Relaunch**: 1st launch good; 2nd launch shows black/white stripes (disp ch0
  re-enable glitch). Decode itself is fine on relaunch (save-mode gives a clean
  image). shadow-protect made it FREEZE instead — reverted.
- **Clean exit**: flaky — sometimes freezes MainUI / grey screen, needs reboot.
  Root issue: raw-disp from a standalone process races MainUI's mali display stack.
- **Composite screen capture** (DISP_CAPTURE_START/COMMIT=0x140/0x142, ARGB into a
  VideoDecoderPallocIonBuf buffer) — `ion_alloc ... size 0` failure; not working yet,
  so can't autonomously verify the overlay/relaunch.
- Every failed display attempt → stuck → reboot. NOT converging incrementally.

Recommended next approach (dedicated effort, not blind patching): either the vendor
display SDK, or a persistent decode service so the decoder is never re-created
(sidesteps relaunch + clean-exit), or proper SDL/mali-composited integration.

## ✅ SOLVED — persistent-daemon architecture (rock solid: start/exit/restart, no reboot)

The hw decoder now runs as a **persistent daemon created ONCE and never destroyed**.
This is the whole solution — verified end-to-end on-device: launch shows video
(~30fps, ~8% CPU), sticks steer (UDP to robot, watchdog-safe), Select+Start returns
to the menu, and **relaunch is instant with no grey screen and no reboot**.

### Why a persistent daemon (the hard-won reasons)
- **`DestroyVideoDecoder()` reliably HANGS** on this A133P CedarC build (VE teardown
  bug) — a clean per-launch exit wedges the process, holding the VE.
- **Create→destroy→recreate corrupts the decoder's ion/GPU buffers**: the 2nd launch
  decodes fine (fps climbs) but the displayed buffer is stale → **grey screen**. This
  is inherent to per-launch decoder lifecycle; only "never destroy" avoids it.
- **`kill -9` of a decoder corrupts the VE** → needs a reboot. NEVER -9 the daemon.

### How it works (`hwdecode/hwdec_test.c` + `HWVideo/launch.sh`)
- Mode `daemon`: forks + `setsid()` (self-daemonizes) BEFORE any cedar init, so the
  decoder outlives `launch.sh`. Decoder created once; loop decodes forever.
- Display toggled by the flag file **`/tmp/hwshow`**: present → `disp_show` ch0;
  absent → `disp_hide` (keeps decoding). Relaunch = re-touch the flag = instant.
- `launch.sh`: `pidof hwdec_test || (start it; sleep 4)`, then `touch /tmp/hwshow`,
  then `while [ -f /tmp/hwshow ]; do sleep 0.3; done`. **`pidof`, not `pgrep -x`**
  (busybox `pgrep -x` is broken here). Daemon start needs **`</dev/null`** or the
  caller's shell hangs on the child's inherited stdin (the C also re-points stdin).
- **Exit = Select+Start** → in daemon mode `unlink("/tmp/hwshow")` (hide, daemon lives
  on); `launch.sh`'s wait-loop ends → back to menu. SIGTERM path does `disp_hide` +
  `_exit(0)` (skips the hanging DestroyVideoDecoder); used only for dev redeploys.

### CrossMix / input facts (non-obvious, cost a lot to learn)
- CrossMix **"Apps" launch as overlays**: MainUI keeps running (its ch1 stays up),
  our video draws on **ch0 z20** over it. So on exit the menu is already underneath.
- The gamepad **IS readable by the app** concurrently — `event3` (evdev) codes
  A=0x130 B=0x131 Select=0x13a Start=0x13b, axes ABS_Y=1 (drive) ABS_RX=3 (turn);
  js0 also works (btn nums 0/1/6/7). `trimui_inputd` does NOT exclusively grab it.
  **Never kill `trimui_inputd`** — it freezes MainUI's menu and won't cleanly restart
  without a reboot.
- Redeploying the binary needs a reboot (can't stop the wedged daemon without -9).
- `ionAlloc ... can not alloc size 0` warnings are **chronic but benign** — video is
  clean through them.

### Source the device decodes
`robot_sim/h264_server.py <cam> 1280 720 30 49601 30 none 2` — H.264/TCP, source
`transpose=2` for the portrait panel. Cameras: Insta360 Link / index, `test` =
synthetic `testsrc` (add `-re`). MacBook built-in camera won't open headless.
**AU assembly:** group all slices of a frame into ONE `SubmitVideoStreamData`
(boundary = VCL NAL with `first_mb_in_slice==0`, i.e. high bit of the byte after the
NAL header). Splitting per-slice in `bIsFramePackage` mode = each slice decoded as a
frame = tearing + 6× CPU.

## Software fallback (still available)
Software decode (PyAV) at **960×540@30** — see `src/video_h264tcp.py` +
`robot_sim/h264_server.py`.

## Motion "breaking up" / pixelation — root cause & fix (2026-05)

Symptom: video pixelated / broke into bands & diagonal "staircase" smears **on
motion**, worst when something moves fast in frame (hand wave, fast pan).

Diagnosed end-to-end with an **autonomous harness** (`tools/motion_source.py`
deterministic moving pattern → `autorun.py` adb-driven atomic snapshots via
`snap.py` → `score.py` conversion-invariant corruption metric). Findings:

1. **It is NOT the encoder/network.** Dumped the exact bytes we hand Cedar
   (`DUMP=1 ./hwdec_shmem`) and decoded them with **ffmpeg → flawless**. The
   bitstream is valid; Cedar silently corrupts high-motion frames while reporting
   success (`bFrameErrorFlag==0`). Encoder preset/profile/GOP sweep made **no**
   difference (fancier presets were slightly worse — bigger motion vectors).

2. **Multi-slice frames are the #1 cause.** `-tune zerolatency` makes x264 use
   *sliced-threads* → several slices per frame → heavy Cedar band corruption.
   **Fix: encode single-slice (`-threads 1`).** Huge improvement. This was missing
   from the production `wheelbase/h264stream.py` — now added there.

3. **Hold-while-converting was the #2 cause.** We held the Cedar `VideoPicture`
   through the ~25 ms scalar YUV→RGB; under motion the VPU reused that buffer
   mid-read → partial-frame staircase. **Fix: memcpy the 3 planes out, then
   `ReturnPicture` immediately, then convert from our own copy** (hold window
   ~25 ms → ~2 ms). See `hwdec_shmem.c`.

4. Residual: at **adversarial** motion (hard-edged solid blocks at 55 px/frame in
   random directions) Cedar still corrupts some frames — a genuine hardware limit,
   not fixable from our side. But **real content decodes clean**: mandelbrot zoom,
   and a real screen-recording of a fast-moving 3D character on textured floor,
   both flawless. Hard-edged synthetic blocks are a pathological worst case that
   doesn't occur in camera video.

Also tried (no/negative effect, reverted or kept cheap): explicit
`nFrameBufferNum`/holding buffers (**broke decode** — ION pool too small, stalls),
`bDispErrorFrame=0` (kept; Cedar doesn't flag these anyway), cache-invalidate of
the VPU output planes (kept as correct practice).

**Bottom line for the robot side:** encode **single-slice** (`-threads 1`, or
`-x264-params sliced-threads=0` to keep multi-core at +1 frame latency). That plus
copy-then-return makes real video decode cleanly.

## Deep investigation — is the hardware motion-corruption fixable? (conclusion: NO, it's the chip)

After single-slice + copy-then-return removed the *systematic* corruption, a residual
remained: the Cedar VPU mis-decodes some genuinely fast-motion frames (a few bad 16x16
blocks), while reporting success. Exhaustively chased it:

- Restored the real `libcedarc` source (vendored in `hwdecode/cedar/`, also aodzip/libcedarc
  on GitHub) and compared our decoder **line-by-line to the canonical reference** —
  `openmax/vdec/src/omx_vdec_aw_decoder_linux.c`. Our init + decode/submit loop **match it**.
- Swept every VConfig lever objectively (`tools/score.py` = conversion-invariant
  worst-block error vs ffmpeg ground truth):
  - `nDecodeSmoothFrameBufferNum` 0/1/2/3 — no change (3 = ref; >0 fine at align=0).
  - `nFrameBufferNum` > 0 — **breaks decode** (ION pool too small on this device; ref leaves 0).
  - `nAlignStride=16` (ref value) — **breaks decode here** (`CdcIonShare` fail); 0 is required.
  - `bIsFramePackage` 1 vs 0 (frame-package vs stream parsing) — no change.
  - Output pixel format: we request NV12, VPU always returns `fmt=1` (I420 planar); we convert
    it correctly. `nVeFreq` only matters for H265.
- Resolution 720p -> 540p: **still corrupts** (not a decode-throughput limit).
- **Decisive test:** re-encoded with the Mac's **hardware** H.264 encoder (VideoToolbox,
  same class as Moonlight's NVENC) instead of x264 -> **corruption identical (122 vs 121).**
  So it is NOT an encoder quirk.
- No alternative decoder on this kernel (Tina BSP 4.9.191, `/dev/cedar_dev` only, no V4L2
  cedrus `/dev/video*`).

**Conclusion:** with the integration matching the reference, the corruption encoder-independent,
every config lever swept, and no other decoder available, this is a **genuine decode-accuracy
limit of the A133 Cedar VPU on fast-motion H.264** — not a config bug. ffmpeg/PyAV software
decode is simply more accurate. Don't re-chase the hardware path; the lever space is exhausted.

## The two video backends (settings switch)

`settings.json` -> `"video": { "backend": ... }`:
- `"sw"` (DEFAULT) -> `src/sw_decode.py` (PyAV) writes /tmp/hwframe. Clean, no motion glitches,
  ~1 CPU core, single-threaded for low latency (`SW_THREADS=AUTO` env to multi-thread).
- `"shmem"` -> `hwdec_shmem` (Cedar VPU). Lowest latency/CPU, glitches on fast motion.

Both write the identical /tmp/hwframe layout so teleop.py is unchanged; the HUD shows which is
active. `launch.sh` reads the setting and starts the right decoder. Flip with the device
helpers `/mnt/UDISK/use_sw.sh` / `use_hw.sh` (then relaunch). Default is `sw` because the robot
is usually moving (hardware would glitch most of the time).
