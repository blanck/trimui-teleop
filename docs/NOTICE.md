# Third-party components

This project (MIT, see LICENSE) bundles or depends on the following:

### Share Tech Mono (`res/ShareTechMono.ttf`)
The HUD font. © The League of Moveable Type, licensed under the
**SIL Open Font License 1.1** — free to use, bundle, and redistribute.
https://fonts.google.com/specimen/Share+Tech+Mono

### Allwinner Cedar / libcedarc headers (`hwdecode/cedar/include/`)
Interface headers for the Allwinner CedarX hardware video decoder, as published
by the community project **aodzip/libcedarc** (unofficial userspace library).
https://github.com/aodzip/libcedarc
These describe the vendor decode API; they are included so the C decoder can be
built. They are third-party and not covered by this repo's MIT license.

### Allwinner Cedar `.so` blobs (`hwdecode/cedar/libs/`, NOT committed)
The actual decoder libraries (`libvdecoder`, `libVE`, `libMemAdapter`,
`libvideoengine`, `libcdc_base`) are **proprietary Allwinner binaries** shipped
on the device. They are git-ignored. Pull them from your own TrimUI to build —
see `hwdecode/cedar/README.md`. Do not redistribute them.
