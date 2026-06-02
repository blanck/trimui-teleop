/* hwdec_shmem — pure hardware H.264 decoder for the TrimUI (Allwinner Cedar VPU).
 *
 * Receives H.264 over TCP, hardware-decodes it, converts each frame to RGB24, and
 * writes it into a double-buffered shared-memory file (/tmp/hwframe). A pygame app
 * reads those frames and does ALL the display + UI. No display/UI code here.
 *
 *   hwdec_shmem <ip> <port> [/tmp/hwframe]
 *
 * Shared file layout:
 *   [0 .. 4096)            header: u32 magic, seq, w, h, bufstride, fmt
 *   [4096 .. +BUFSZ)       frame buffer 0   (RGB24)
 *   [4096+BUFSZ .. +BUFSZ) frame buffer 1
 * C writes buf[seq&1] fully, THEN publishes hdr.seq=seq (so a reader that sees seq
 * always reads a complete, non-racing buffer).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>

#include "vdecoder.h"
#include "sc_interface.h"

extern struct ScMemOpsS *MemAdapterGetOpsS(void);

#ifndef SMOOTH_BUFS
#define SMOOTH_BUFS 0          /* extra decoded-picture buffers; sweep via -DSMOOTH_BUFS=N */
#endif
#ifndef ALIGN_STRIDE
#define ALIGN_STRIDE 0         /* frame-buffer stride alignment; sweep via -DALIGN_STRIDE=N */
#endif
#ifndef FRAMEPKG
#define FRAMEPKG 1             /* 1=frame-package (one AU/submit), 0=stream (Cedar parses) */
#endif

#define MAXW 1280
#define MAXH 720
#define NBUF 8                  /* ring of frame buffers: more slack so a decode burst
                                 * is less likely to lap the (slow) reader mid-copy */
#define BUFSZ (MAXW * MAXH * 3)
#define HDRSZ 4096
#define TOTAL (HDRSZ + NBUF * BUFSZ)

struct shmhdr { uint32_t magic, seq, w, h, bufstride, fmt; };

static volatile int g_quit = 0;
static void on_sig(int s) { (void)s; g_quit = 1; }
static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }

static int tcp_connect(const char *ip, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0); if (fd < 0) return -1;
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(port); inet_pton(AF_INET, ip, &a.sin_addr);
    if (connect(fd, (struct sockaddr *)&a, sizeof a) < 0) { close(fd); return -1; }
    int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    return fd;
}
static int find_sc(const unsigned char *b, int len, int from) {
    for (int i = from; i + 3 <= len; i++) if (b[i] == 0 && b[i + 1] == 0 && b[i + 2] == 1) return i;
    return -1;
}
static long g_drops = 0, g_subs = 0, g_resets = 0;   /* diagnostics: dropped/submitted AUs, buffer resets */
static long g_errs = 0; static int g_dbg = 0;        /* Cedar-flagged error frames + one-shot layout dump */
static long g_trunc = 0;
static int g_dumpfd = -1; static long g_dumped = 0;   /* dump exact submitted bytes for ffmpeg cross-check */
static void submit_au(VideoDecoder *dec, unsigned char *data, int len, int64_t pts) {
    if (g_dumpfd >= 0 && g_dumped < 24L * 1024 * 1024) { write(g_dumpfd, data, len); g_dumped += len; }
    char *buf = NULL, *ring = NULL; int bufSize = 0, ringSize = 0;
    if (RequestVideoStreamBuffer(dec, len, &buf, &bufSize, &ring, &ringSize, 0) != 0 || !buf) { g_drops++; return; }
    if (bufSize >= len) memcpy(buf, data, len);
    else {
        memcpy(buf, data, bufSize);
        if (ring && ringSize >= len - bufSize) memcpy(ring, data + bufSize, len - bufSize);
        else { g_trunc++; return; }   /* can't fit the tail -> would submit a truncated frame; drop instead */
    }
    VideoStreamDataInfo di; memset(&di, 0, sizeof di);
    di.pData = buf; di.nLength = len; di.nPts = pts;
    di.bIsFirstPart = 1; di.bIsLastPart = 1; di.bValid = 1;
    SubmitVideoStreamData(dec, &di, 0);
    g_subs++;
}

/* planar YUV420 (BT601) -> RGB24, with output crop w x h from line stride ys */
static void yuv420_to_rgb(const unsigned char *Y, const unsigned char *U, const unsigned char *V,
                          int ys, int cs, int w, int h, unsigned char *rgb) {
    for (int y = 0; y < h; y++) {
        const unsigned char *yr = Y + (long)y * ys;
        const unsigned char *ur = U + (long)(y >> 1) * cs;
        const unsigned char *vr = V + (long)(y >> 1) * cs;
        unsigned char *o = rgb + (long)y * w * 3;
        for (int x = 0; x < w; x++) {
            int c = yr[x], d = ur[x >> 1] - 128, e = vr[x >> 1] - 128;
            int r = c + ((1436 * e) >> 10);
            int g = c - ((352 * d + 731 * e) >> 10);
            int b = c + ((1814 * d) >> 10);
            o[0] = r < 0 ? 0 : r > 255 ? 255 : r;
            o[1] = g < 0 ? 0 : g > 255 ? 255 : g;
            o[2] = b < 0 ? 0 : b > 255 ? 255 : b;
            o += 3;
        }
    }
}

int main(int argc, char **argv) {
    setlinebuf(stderr);
    signal(SIGTERM, on_sig); signal(SIGINT, on_sig);
    const char *ip = argc > 1 ? argv[1] : "10.0.0.117";
    int port = argc > 2 ? atoi(argv[2]) : 8090;
    const char *path = argc > 3 ? argv[3] : "/tmp/hwframe";

    int sfd = open(path, O_RDWR | O_CREAT, 0666);
    if (sfd < 0) { fprintf(stderr, "open %s: %s\n", path, strerror(errno)); return 1; }
    if (ftruncate(sfd, TOTAL) != 0) { fprintf(stderr, "ftruncate: %s\n", strerror(errno)); return 1; }
    unsigned char *shm = mmap(NULL, TOTAL, PROT_READ | PROT_WRITE, MAP_SHARED, sfd, 0);
    if (shm == MAP_FAILED) { fprintf(stderr, "mmap: %s\n", strerror(errno)); return 1; }
    struct shmhdr *hdr = (struct shmhdr *)shm;
    unsigned char *bufs = shm + HDRSZ;
    memset(hdr, 0, sizeof *hdr); hdr->magic = 0x48574d46; /* 'HWMF' */
    fprintf(stderr, "shmem %s (%d bytes)\n", path, TOTAL);

    int sock = tcp_connect(ip, port);
    if (sock < 0) { fprintf(stderr, "connect %s:%d failed: %s\n", ip, port, strerror(errno)); return 1; }
    fprintf(stderr, "connected %s:%d\n", ip, port);

    AddVDPlugin();
    VideoDecoder *dec = CreateVideoDecoder();
    if (!dec) { fprintf(stderr, "CreateVideoDecoder failed\n"); return 1; }
    VideoStreamInfo si; memset(&si, 0, sizeof si);
    si.eCodecFormat = VIDEO_CODEC_FORMAT_H264; si.bIsFramePackage = FRAMEPKG;
    VConfig vc; memset(&vc, 0, sizeof vc);
    vc.eOutputPixelFormat = PIXEL_FORMAT_NV12; vc.bNoBFrames = 1; vc.bDisable3D = 1;
    vc.nVbvBufferSize = 16 * 1024 * 1024;   /* large so motion bitrate spikes don't overflow -> dropped frames */
    /* Match the reference OMX decoder's frame-buffer config (omx_vdec_aw_decoder
     * _linux.c). The KEY field is nDecodeSmoothFrameBufferNum=3: extra decoded-
     * picture buffers so under motion the decoder has slack and never reuses a
     * frame still needed as a reference -> was the staircase corruption. Leave
     * nFrameBufferNum=0 (decoder computes the DPB; forcing it exhausts ION). */
    vc.bDispErrorFrame = 1;
    vc.nAlignStride = ALIGN_STRIDE;                 /* 0 known-good; 16 = ref (breaks ION here) */
    vc.nDeInterlaceHoldingFrameBufferNum = 0;
    vc.nDisplayHoldingFrameBufferNum = 0;
    vc.nRotateHoldingFrameBufferNum = 0;
    vc.nDecodeSmoothFrameBufferNum = SMOOTH_BUFS;   /* swept: 0 known-good, 3 = ref */
    if (InitializeVideoDecoder(dec, &si, &vc) != 0) { fprintf(stderr, "init failed\n"); return 1; }
    struct ScMemOpsS *memops = MemAdapterGetOpsS();   /* for cache-invalidate of VPU output */
    if (getenv("DUMP")) g_dumpfd = open("/tmp/submitted.h264", O_WRONLY | O_CREAT | O_TRUNC, 0666);
    fprintf(stderr, "decoder ready (memops=%p)\n", (void *)memops);

    static unsigned char acc[4 * 1024 * 1024];
    static unsigned char ycopy[2048 * MAXH];       /* plane copies so we can ReturnPicture */
    static unsigned char ucopy[1024 * (MAXH / 2)]; /* before the slow RGB conversion */
    static unsigned char vcopy[1024 * (MAXH / 2)];
    int accLen = 0, au_start = -1, au_has_vcl = 0;
    unsigned char net[65536];
    int64_t pts = 0;
    long total = 0, last_total = 0; double t_report = now_s();
    uint32_t seq = 0;

    while (!g_quit) {
        int n = recv(sock, net, sizeof net, 0);
        if (n <= 0) {
            if (g_quit) break;
            if (n < 0 && errno == EINTR) continue;
            close(sock); usleep(500000);
            while (!g_quit && (sock = tcp_connect(ip, port)) < 0) usleep(1000000);
            accLen = 0; au_start = -1; au_has_vcl = 0; continue;
        }
        if (accLen + n > (int)sizeof acc) { g_resets++; accLen = 0; au_start = -1; au_has_vcl = 0; }
        memcpy(acc + accLen, net, n); accLen += n;
        if (au_start < 0) { au_start = find_sc(acc, accLen, 0); if (au_start < 0) continue; }
        int cur = find_sc(acc, accLen, au_start);
        while (cur >= 0) {
            int next = find_sc(acc, accLen, cur + 3);
            if (next < 0) break;
            int nal_type = acc[cur + 3] & 0x1F;
            if (nal_type == 1 || nal_type == 5) {
                int first_slice = (cur + 4 < accLen) && (acc[cur + 4] & 0x80);
                if (first_slice && au_has_vcl && cur - au_start > 0) {
                    submit_au(dec, acc + au_start, cur - au_start, pts); pts += 33333; au_start = cur;
                }
                au_has_vcl = 1;
            }
            cur = next;
        }
        if (au_start > 0) { memmove(acc, acc + au_start, accLen - au_start); accLen -= au_start; au_start = 0; }

        int guard = 0;
        while (guard++ < 64) {
            int dr = DecodeVideoStream(dec, 0, 0, 0, 0);
            if (dr == VDECODE_RESULT_NO_BITSTREAM) break;
            while (ValidPictureNum(dec, 0) > 0) {
                VideoPicture *p = RequestPicture(dec, 0);
                if (!p) break;
                int w = p->nWidth, h = p->nHeight, ys = p->nLineStride, cs = ys / 2;
                if (w > MAXW) w = MAXW; if (h > MAXH) h = MAXH;
                if (ys > 2048) ys = 2048; if (cs > 1024) cs = 1024;  /* guard copy bufs */
                if (p->bFrameErrorFlag || p->bTopFieldError || p->bBottomFieldError) g_errs++;
                if (g_dbg < 3) {                /* one-shot layout dump */
                    fprintf(stderr, "PIC fmt=%d stride=%d off T%d L%d B%d R%d err=%d/%d/%d\n",
                            p->ePixelFormat, ys, p->nTopOffset, p->nLeftOffset,
                            p->nBottomOffset, p->nRightOffset,
                            p->bFrameErrorFlag, p->bTopFieldError, p->bBottomFieldError);
                    g_dbg++;
                }
                /* The VPU DMA-writes the decoded frame straight to RAM, bypassing
                 * the CPU cache. Invalidate each plane before we read it, else the
                 * CPU returns STALE cached lines for the regions the VPU just
                 * updated -> "part old / part new" staircase in moving areas. */
                if (memops) {
                    CdcMemFlushCache(memops, p->pData0, (size_t)ys * h);
                    CdcMemFlushCache(memops, p->pData1, (size_t)cs * (h / 2));
                    CdcMemFlushCache(memops, p->pData2, (size_t)cs * (h / 2));
                }
                /* Copy the planes out FAST, then ReturnPicture IMMEDIATELY. The
                 * slow scalar YUV->RGB (~25ms) used to run while we still held the
                 * Cedar picture; under heavy motion the VPU reused that buffer
                 * mid-read -> partial-frame "staircase" corruption. Now we hold it
                 * only for the ~2ms memcpy, then convert from our own copy. */
                memcpy(ycopy, p->pData0, (size_t)ys * h);
                memcpy(ucopy, p->pData1, (size_t)cs * (h / 2));
                memcpy(vcopy, p->pData2, (size_t)cs * (h / 2));
                ReturnPicture(dec, p);
                unsigned char *dst = bufs + (size_t)(seq & (NBUF - 1)) * BUFSZ;
                yuv420_to_rgb(ycopy, ucopy, vcopy, ys, cs, w, h, dst);
                hdr->w = w; hdr->h = h; hdr->bufstride = BUFSZ; hdr->fmt = 0;
                __sync_synchronize();
                hdr->seq = seq;                 /* publish the seq that MATCHES buf[seq & N] */
                seq++;                          /* advance AFTER publishing (was the off-by-one bug) */
                total++;
            }
            if (dr == VDECODE_RESULT_NO_FRAME_BUFFER && ValidPictureNum(dec, 0) == 0) break;
        }

        double t = now_s();
        if (t - t_report >= 1.0) {
            fprintf(stderr, "decode fps=%.1f total=%ld subs=%ld DROPS=%ld TRUNC=%ld resets=%ld ERRS=%ld %dx%d\n",
                    (total - last_total) / (t - t_report), total, g_subs, g_drops, g_trunc, g_resets, g_errs, hdr->w, hdr->h);
            last_total = total; t_report = t;
        }
    }
    fprintf(stderr, "exit\n");
    _exit(0);
}
