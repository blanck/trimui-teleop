# Cedar VPU decode dependencies

The hardware H.264 decoder (`../hwdec_shmem.c`) links against the Allwinner Cedar
VPU userspace libraries.

## `include/` — headers (committed)
Interface headers from [aodzip/libcedarc](https://github.com/aodzip/libcedarc).
Third-party; see `../../docs/NOTICE.md`.

## `libs/` — `.so` blobs (NOT committed — get them from your device)
The five proprietary libraries are on every TrimUI Smart Pro. Pull them from
your own device with adb:

```sh
ADB=~/Library/Android/sdk/platform-tools/adb   # or wherever adb lives
mkdir -p libs
for lib in libvdecoder libVE libMemAdapter libvideoengine libcdc_base; do
  $ADB pull /usr/lib/$lib.so libs/$lib.so
done
```

Then build:

```sh
cd ..            # hwdecode/
ZIG=$(command -v zig) ./build.sh
```

These blobs are Allwinner's; they are git-ignored and must not be redistributed.
