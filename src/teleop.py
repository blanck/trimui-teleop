"""Teleop — pygame ground-station UI for the TrimUI handheld.

Video is decoded (Cedar VPU `hwdec_shmem`, or PyAV `sw_decode.py`) into a shared-
memory frame; this app blits the ready-made RGB frames and draws the UI on top, so
it's cheap. It finds the robot by LAN discovery, reads the gamepad -> control (UDP),
and receives telemetry (UDP) -> on-screen status. Menu (or Select+Start) exits.

    SDL_VIDEODRIVER=mali LD_LIBRARY_PATH=/usr/trimui/lib python3.11 teleop.py
"""
import json
import mmap
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import pygame

import config
import discovery
from controller import Controller

SW_NAMES = ("sw", "soft", "software", "python", "pyav")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # /mnt/UDISK


def is_sw(backend):
    return backend in SW_NAMES


def stop_decoder():
    """Kill any running decoder. Closing its TCP stream is what tells the robot's
    video server to stop encoding/sending — it serves per-connection, so an EOF on
    the socket frees the camera and stops the bytes."""
    os.system("kill $(pidof hwdec_shmem) 2>/dev/null; pkill -9 -f src/sw_decode.py 2>/dev/null")


def start_decoder(backend, host, port):
    """Stop whatever decoder is running and start the one for `backend`, both of
    which write /tmp/hwframe. Run in a thread (it sleeps ~1s to let the stream
    server free the camera before the new decoder reconnects)."""
    stop_decoder()
    time.sleep(1.2)
    if is_sw(backend):
        # On the device the SW decoder runs in the mali venv; off-device (e.g.
        # `uv run` on a desktop) there's no rtvenv, so use the current interpreter.
        py = os.path.join(BASE, "rtvenv", "bin", "python3.11")
        if not os.path.exists(py):
            py = sys.executable
        cmd = [py, os.path.join(BASE, "src", "sw_decode.py"),
               f"tcp://{host}:{port}", "/tmp/hwframe"]
        env = dict(os.environ)
    else:
        cmd = [f"{BASE}/hwdec_shmem", str(host), str(port), "/tmp/hwframe"]
        env = dict(os.environ, LD_LIBRARY_PATH="/usr/lib:/usr/trimui/lib")
    log = open("/tmp/hwdec.log", "ab")
    try:
        subprocess.Popen(cmd, env=env, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log.write(f"start_decoder failed: {e}\n".encode())


_BATT_DIR = "/sys/class/power_supply/axp2202-battery"
_batt = {"t": -1e9, "pct": None, "chg": False, "ok": True}


def handheld_battery():
    """The TrimUI's own battery as (pct, charging), or (None, False) when the sysfs
    node is absent (e.g. running off-device). Cached ~10 s — battery moves slowly."""
    now = time.monotonic()
    if _batt["ok"] and now - _batt["t"] >= 10:
        _batt["t"] = now
        try:
            with open(_BATT_DIR + "/capacity") as f:
                _batt["pct"] = int(f.read().strip())
            with open(_BATT_DIR + "/status") as f:
                _batt["chg"] = "harg" in f.read()        # "Charging"
        except OSError:
            _batt["ok"] = False; _batt["pct"] = None
    return _batt["pct"], _batt["chg"]


def save_setting(settings_path, section, key, value):
    """Persist one setting so the next launch keeps it. Creates settings.json if
    it doesn't exist yet (defaults otherwise live in config.py)."""
    try:
        d = json.load(open(settings_path))
    except Exception:
        d = {}
    d.setdefault(section, {})[key] = value
    try:
        json.dump(d, open(settings_path, "w"), indent=2)
    except Exception:
        pass

VERSION = "build 15"         # bump on every deploy so the HUD shows it's updated
DEADZONES = (0.06, 0.10, 0.15, 0.22)   # cycled by the in-app menu
HDR = 4096
NBUF = 8                     # must match hwdec_shmem.c (ring of frame buffers)

# --- technical HUD palette (cyan/green mission-control) ---
ACC = (0, 224, 255)          # cyan primary accent
OKC = (44, 232, 150)         # green: ok / active
WARN = (255, 184, 40)        # amber: caution
ALERT = (255, 72, 72)        # red: alert
TXT = (206, 222, 230)        # primary text
DIM = (104, 128, 140)        # labels / inactive
LINE = (46, 66, 78)          # hairlines
PANEL = (8, 13, 19)          # panel base (used with alpha)
FONT_PATH = os.path.join(BASE, "res", "ShareTechMono.ttf")


def load_font(size):
    try:
        return pygame.font.Font(FONT_PATH, size)
    except Exception:
        return pygame.font.Font(None, size)


def load_icon(size=140):
    """The app icon, shared with the launcher tile. Deployed to res/ on the device;
    falls back to app/ when running from the repo. Call after the display is set."""
    for p in (os.path.join(BASE, "res", "icon.png"), os.path.join(BASE, "app", "icon.png")):
        try:
            img = pygame.image.load(p)
            try:
                img = img.convert_alpha()
            except Exception:
                pass
            return pygame.transform.smoothscale(img, (size, size))
        except Exception:
            continue
    return None


def now_ms():
    return int(time.monotonic() * 1000)


def say_text(phrases, phrase_idx, emotions, emotion_idx):
    """Combine the selected emotion and phrase into a v3 tagged string."""
    phrase = phrases[phrase_idx]
    emotion = emotions[emotion_idx] if emotions else "neutral"
    if emotion == "neutral":
        return phrase
    return f"[{emotion}] {phrase}"


class CpuMon:
    def __init__(self):
        try:
            self.clk = os.sysconf("SC_CLK_TCK")
        except Exception:
            self.clk = 100
        self.t = time.monotonic(); self.proc = self._p(); self.pct = 0.0

    def _p(self):
        try:
            p = open("/proc/self/stat").read().split()
            return (int(p[13]) + int(p[14])) / self.clk
        except Exception:
            return None

    def sample(self):
        now = time.monotonic(); dt = now - self.t
        p = self._p()
        if dt > 0 and p is not None and self.proc is not None:
            self.pct = max(0.0, (p - self.proc) / dt * 100.0)
        self.proc = p; self.t = now


class Telemetry(threading.Thread):
    """Receives robot telemetry (PROTOCOL.md) on udp/port; tracks link + RTT."""
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port; self.lock = threading.Lock()
        self.d = {}; self.last_rx = 0.0; self.rtt = 0.0

    def run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", self.port)); s.settimeout(1.0)
        while True:
            try:
                data, _ = s.recvfrom(2048)
                m = json.loads(data.decode())
                if m.get("type") == "tele":
                    with self.lock:
                        self.d = m; self.last_rx = time.monotonic()
                        if m.get("ack_t"):
                            self.rtt = now_ms() - int(m["ack_t"])
            except socket.timeout:
                pass
            except Exception:
                pass

    def get(self):
        with self.lock:
            link = self.last_rx > 0 and (time.monotonic() - self.last_rx) < 1.0
            return dict(self.d), link, self.rtt


def open_shmem(path):
    for _ in range(200):
        if os.path.exists(path) and os.path.getsize(path) >= HDR:
            break
        time.sleep(0.1)
    f = open(path, "rb")
    return f, mmap.mmap(f.fileno(), 0, mmap.MAP_SHARED, mmap.PROT_READ)


def _ticks(surf, w, h, col, L=16, t=2):
    """Draw corner brackets on a surface/screen of size w x h."""
    for cx, cy, dx, dy in [(0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)]:
        pygame.draw.line(surf, col, (cx, cy), (cx + dx * L, cy), t)
        pygame.draw.line(surf, col, (cx, cy), (cx, cy + dy * L), t)


def render_panel(title, rows, f_hdr, f_row, w, acc=ACC):
    """A framed translucent telemetry panel: header strip + label/value rows with
    status dots. Returned as a cached Surface (text renders are costly)."""
    rh, top = 30, 38
    h = top + rh * len(rows) + 8
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    s.fill((*PANEL, 184))
    pygame.draw.rect(s, (*acc, 32), (0, 0, w, 28))            # header strip
    pygame.draw.line(s, acc, (0, 28), (w, 28), 1)
    s.blit(f_hdr.render(title, True, acc), (12, 4))
    th = f_hdr.render("//", True, (*acc, 140))
    s.blit(th, (w - th.get_width() - 10, 4))
    y = top
    for dot, label, value, vc in rows:
        if dot:
            pygame.draw.circle(s, dot, (15, y + 11), 4)
            pygame.draw.circle(s, (*dot, 60), (15, y + 11), 7, 1)
        s.blit(f_row.render(label, True, DIM), (30, y))
        vs = f_row.render(value, True, vc)
        s.blit(vs, (w - vs.get_width() - 12, y))
        y += rh
    pygame.draw.rect(s, (*acc, 130), (0, 0, w, h), 1)
    _ticks(s, w - 1, h - 1, acc, 10, 2)
    return s


def draw_axis(screen, x, y, w, h, value, acc, label, f):
    """Center-zero segmented gauge (throttle/steering), with numeric readout."""
    screen.blit(f.render(label, True, DIM), (x, y - 1))
    bx, bw = x + 62, w - 62
    pygame.draw.rect(screen, (12, 18, 24), (bx, y, bw, h))
    pygame.draw.rect(screen, LINE, (bx, y, bw, h), 1)
    cx = bx + bw // 2
    half = bw // 2
    step = 11                                                # segment pitch
    nseg = (half - 4) // step
    fill = int(round(abs(value) * nseg))
    for i in range(nseg):
        on = i < fill
        col = acc if on else (28, 40, 48)
        sx = cx + 3 + i * step if value >= 0 else cx - 3 - (i + 1) * step + 2
        pygame.draw.rect(screen, col, (sx, y + 2, step - 3, h - 4))
    pygame.draw.line(screen, (150, 170, 180), (cx, y - 2), (cx, y + h + 2), 1)
    vt = f.render(f"{value:+.2f}", True, acc if abs(value) > 0.02 else DIM)
    screen.blit(vt, (bx + bw + 12, y - 1))


def render_chip(text, f, acc=ACC):
    t = f.render(text, True, acc)
    w, h = t.get_width() + 26, t.get_height() + 10
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    s.fill((*PANEL, 170))
    pygame.draw.rect(s, (*acc, 150), (0, 0, w, h), 1)
    pygame.draw.rect(s, acc, (0, 0, 4, h))                    # left accent
    s.blit(t, (14, 5))
    return s


def draw_banner(screen, SW, y, text, f, fg):
    t = f.render(text, True, fg)
    pw, ph = t.get_width() + 52, t.get_height() + 16
    px = (SW - pw) // 2
    s = pygame.Surface((pw, ph), pygame.SRCALPHA)
    s.fill((10, 6, 8, 215) if fg == ALERT else (6, 12, 10, 215))
    pygame.draw.rect(s, fg, (0, 0, pw, ph), 2)
    pygame.draw.rect(s, fg, (0, 0, 6, ph))
    s.blit(t, (28, 8))
    screen.blit(s, (px, y))


def draw_reticle(screen, SW, SH):
    cx, cy = SW // 2, SH // 2
    col = (0, 150, 175)          # dim cyan, drawn directly (no per-frame alpha surface)
    g, r = 16, 30
    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        pygame.draw.line(screen, col, (cx + dx * g, cy + dy * g), (cx + dx * r, cy + dy * r), 1)
    pygame.draw.circle(screen, col, (cx, cy), 2)


def splash(screen, SW, SH, icon, title, sub, f_big, f_small, sub_dy=0):
    """Branded full-screen message (startup scan / awaiting video)."""
    screen.fill((6, 9, 13))
    cy = SH // 2 - 70
    if icon:
        screen.blit(icon, (SW // 2 - icon.get_width() // 2, cy - icon.get_height() - 14))
    t = f_big.render(title, True, ACC)
    screen.blit(t, (SW // 2 - t.get_width() // 2, cy))
    if sub:
        h = f_small.render(sub, True, DIM)
        screen.blit(h, (SW // 2 - h.get_width() // 2, cy + 54 + sub_dy))


def draw_menu(screen, SW, SH, fonts, items, idx, sub=""):
    f_title, f_item, f_hint = fonts
    ov = pygame.Surface((SW, SH), pygame.SRCALPHA); ov.fill((3, 7, 11, 188))
    screen.blit(ov, (0, 0))
    pw, ph = 620, 122 + 70 * len(items)
    px, py = (SW - pw) // 2, (SH - ph) // 2
    p = pygame.Surface((pw, ph), pygame.SRCALPHA)
    p.fill((*PANEL, 238))
    pygame.draw.rect(p, (*ACC, 34), (0, 0, pw, 58))
    pygame.draw.line(p, ACC, (0, 58), (pw, 58), 2)
    p.blit(f_title.render("ROBOT CONTROL", True, ACC), (26, 12))
    if sub:
        st = f_hint.render(sub, True, DIM)
        p.blit(st, (pw - st.get_width() - 26, 22))
    iy0 = 88
    for i, it in enumerate(items):
        iy = iy0 + i * 70
        sel = i == idx
        if sel:
            pygame.draw.rect(p, (*ACC, 42), (16, iy - 8, pw - 32, 56))
            pygame.draw.rect(p, ACC, (16, iy - 8, 5, 56))
            p.blit(f_item.render(">", True, ACC), (pw - 46, iy))
        p.blit(f_item.render(it, True, (236, 246, 250) if sel else DIM), (42, iy))
    pygame.draw.line(p, (*ACC, 90), (0, ph - 46), (pw, ph - 46), 1)
    hint = f_hint.render("D-PAD MOVE    /    A SELECT    /    MENU CLOSE", True, (96, 116, 128))
    p.blit(hint, ((pw - hint.get_width()) // 2, ph - 36))
    _ticks(p, pw - 1, ph - 1, ACC, 20, 3)
    screen.blit(p, (px, py))


def main():
    settings_path = (sys.argv[sys.argv.index("--settings") + 1]
                     if "--settings" in sys.argv else config.default_path())
    cfg = config.load(settings_path)   # DEFAULTS + optional settings.json on top
    steer_cfg = cfg["steer"]
    vcfg = cfg.get("video", {})
    shmem_path = vcfg.get("shmem", "/tmp/hwframe")
    cur_backend = vcfg.get("backend", "shmem")

    # Local audio off by default: the handheld is silent (phrases are spoken on the
    # robot), so we point SDL at its dummy audio driver before init. That keeps pygame
    # from opening ALSA — no audio threads, no constant buffer-underrun churn (~8% CPU).
    # Flip cfg["audio"]["enabled"] to True (e.g. for robot-mic playback) to use real audio.
    if not cfg.get("audio", {}).get("enabled", False):
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    pygame.init()
    pygame.mouse.set_visible(False)
    # Fullscreen on the handheld; a 1280x720 window off-device so the same app is
    # usable for UI dev on a desktop (`uv run python src/teleop.py`). Force windowed
    # with TELEOP_WINDOWED=1.
    on_device = os.path.exists(os.path.join(BASE, "rtvenv"))
    if on_device and os.environ.get("TELEOP_WINDOWED") != "1":
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    else:
        screen = pygame.display.set_mode((1280, 720))
    SW, SH = screen.get_size()
    icon = load_icon()                      # after set_mode so convert_alpha works
    if icon:
        pygame.display.set_icon(icon)
    f_row = load_font(21)        # panel rows / axis
    f_hdr = load_font(23)        # panel header
    f_chip = load_font(20)       # top-right chip
    f_ban = load_font(40)        # banners
    f_wait = load_font(44)       # waiting / scanning screen
    menu_fonts = (load_font(36), load_font(32), load_font(18))
    clock = pygame.time.Clock()

    # ---- locate the robot: auto-discovery (default) or an explicit host ----
    # `tgt` holds the live target; re-discovery (below) may update it.
    tgt = {"host": str(vcfg.get("stream_host", "auto")),
           "stream": int(vcfg.get("stream_port", 49601)),
           "steer": int(steer_cfg.get("port", 49602)),
           "tele": int(cfg.get("telemetry", {}).get("port", 49603)),
           "name": "--"}        # robot platform name, learned from discovery
    auto = tgt["host"].lower() in ("auto", "", "none")
    if auto:
        res = None
        last = vcfg.get("last_host")
        if last:                         # fast path: ask the last-known IP directly
            splash(screen, SW, SH, icon, "CONNECTING", f"{last}", f_wait, f_row)
            pygame.display.flip()
            res = discovery.probe(last)
        if not res:                      # fall back to a broadcast scan
            splash(screen, SW, SH, icon, "SCANNING NETWORK",
                   "looking for the robot on this network ...", f_wait, f_row)
            pygame.display.flip()
            res = discovery.discover(timeout=8)
        if res:
            host, info = res
            tgt["host"] = host
            tgt["stream"] = int(info.get("stream", tgt["stream"]))
            tgt["steer"] = int(info.get("steer", tgt["steer"]))
            tgt["tele"] = int(info.get("tele", tgt["tele"]))
            tgt["name"] = str(info.get("name", "robot"))
            if host != last:             # remember it for an instant reconnect next boot
                save_setting(settings_path, "video", "last_host", host)
            print(f"robot at {host} {info}", flush=True)
        else:
            tgt["host"] = vcfg.get("fallback_host", "127.0.0.1")

    ctrl = Controller(cfg["controls"])
    tele = Telemetry(tgt["tele"]); tele.start()
    cpu = CpuMon()
    steer_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start_decoder(cur_backend, tgt["host"], tgt["stream"])   # teleop owns the decoder
    sf, mm = open_shmem(shmem_path)

    seq = 0
    last_seq = 0
    vstate = "waiting"
    fcount = 0; t_fps = time.monotonic(); render_fps = 0.0
    t_cpu = time.monotonic()
    hud_surf = None; hud_t = 0.0
    disco_t = 0.0; disco_busy = [False]   # periodic re-discovery while no video (auto)

    def menu_list():     # built fresh each time the menu opens so values are current
        other = "Hardware (Cedar)" if is_sw(cur_backend) else "Software (PyAV)"
        inv = "On" if ctrl.cfg.get("invert_drive") else "Off"
        dz = ctrl.cfg.get("deadzone", 0.12)
        return ["Resume",
                f"Video:  {other}",
                f"Invert drive:  {inv}",
                f"Deadzone:  {dz:.2f}",
                "Rescan robot",
                "Exit"]

    menu_button = cfg["controls"].get("menu_button", 8)
    confirm_button = cfg["controls"].get("confirm_button", 0)
    restart_button = cfg["controls"].get("restart_button", 7)
    say_button = cfg["controls"].get("boost_button", 0)
    video_toggle_button = cfg["controls"].get("video_toggle_button", 6)
    video_on = True              # SELECT toggles; off stops the decoder (robot stops sending)
    restart = False
    menu_open = False; menu_idx = 0; menu_items = menu_list()

    # Phrases the robot can speak, D-pad up/down selects, B sends the current one
    phrases = cfg.get("phrases", [])
    phrase_idx = 0
    say_seq = 0

    # Emotions, D-pad left/right selects, prepended to the phrase as a v3 tag
    emotions = cfg.get("emotions", [])
    emotion_idx = 0
    phrase_chip = None; phrase_chip_key = None
    switch_t = 0.0; notice = ""  # decoder-restart debounce + the banner shown during it
    cap_left = 0; cap_n = 0      # X-button burst capture of the raw input frames
    print(f"teleop {VERSION} up", flush=True)
    # Startup robustness: drain stale input events queued during the MainUI->app
    # handover (a leftover MENU/Select+Start there would otherwise quit us on the
    # first frame), and ignore all quit/menu input for a short grace window.
    pygame.event.clear()
    grace = 20                   # ~0.7s at 30Hz before quit/menu inputs are honored
    running = True
    # Idle throttle: after idle_after_s with no input we render at idle_fps instead of
    # fps to save CPU (ctrl packets keep flowing >2 Hz, inside the robot's 0.5s watchdog).
    fps = int(cfg["screen"].get("fps", 30))
    idle_fps = int(cfg["screen"].get("idle_fps", 5))
    idle_after = float(cfg["screen"].get("idle_after_s", 5.0))
    last_activity = time.monotonic()
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                if grace == 0:
                    running = False
            elif e.type == pygame.JOYBUTTONDOWN:
                print(f"BTN {e.button}", flush=True)        # log button ids (identify X)
                if grace > 0:                                # ignore stray startup presses
                    continue
                last_activity = time.monotonic()            # wake from idle throttle
                if e.button in (2, 3):                       # X / Y -> capture a burst of input frames
                    cap_left = 10

                # B speaks the selected phrase with the selected emotion when not in the menu
                if e.button == say_button and not menu_open and phrases:
                    say_seq += 1
                    say_msg = {"type": "say", "seq": say_seq, "text": say_text(phrases, phrase_idx, emotions, emotion_idx)}
                    try:
                        steer_sock.sendto(json.dumps(say_msg).encode(), (tgt["host"], tgt["steer"]))
                    except OSError:
                        pass

                if e.button == restart_button and not ctrl.quit_combo():
                    restart = True; running = False          # START alone -> relaunch
                if e.button == video_toggle_button and not menu_open and not ctrl.quit_combo():
                    video_on = not video_on                  # SELECT -> show/hide video
                    try:                                     # tell the robot to stop/resume sending
                        steer_sock.sendto(json.dumps({"type": "video", "on": video_on}).encode(),
                                          (tgt["host"], tgt["steer"]))
                    except OSError:
                        pass
                    if video_on:                             # reconnect: robot resumes encoding
                        threading.Thread(target=start_decoder,
                                         args=(cur_backend, tgt["host"], tgt["stream"]),
                                         daemon=True).start()
                    else:                                    # disconnect: robot stops encoding
                        stop_decoder(); vstate = "off"
                if e.button == menu_button:
                    menu_open = not menu_open; menu_idx = 0
                    if menu_open:
                        menu_items = menu_list()         # reflect current backend
                elif menu_open and e.button == confirm_button:   # A selects (confirm_button)
                    sel = menu_items[menu_idx]
                    if sel == "Resume":
                        menu_open = False
                    elif sel.startswith("Video") and time.monotonic() - switch_t > 3.0:
                        cur_backend = "shmem" if is_sw(cur_backend) else "sw"
                        save_setting(settings_path, "video", "backend", cur_backend)
                        notice = "SWITCHING TO " + ("SOFTWARE" if is_sw(cur_backend) else "HARDWARE") + " DECODE"
                        switch_t = time.monotonic()
                        threading.Thread(target=start_decoder,
                                         args=(cur_backend, tgt["host"], tgt["stream"]),
                                         daemon=True).start()
                        menu_open = False
                    elif sel.startswith("Invert"):
                        ctrl.cfg["invert_drive"] = not ctrl.cfg.get("invert_drive")
                        save_setting(settings_path, "controls", "invert_drive",
                                     bool(ctrl.cfg["invert_drive"]))
                        menu_items = menu_list()
                    elif sel.startswith("Deadzone"):
                        cur = ctrl.cfg.get("deadzone", 0.12)
                        i = min(range(len(DEADZONES)), key=lambda k: abs(DEADZONES[k] - cur))
                        nxt = DEADZONES[(i + 1) % len(DEADZONES)]
                        ctrl.cfg["deadzone"] = nxt
                        save_setting(settings_path, "controls", "deadzone", nxt)
                        menu_items = menu_list()
                    elif sel == "Rescan robot" and time.monotonic() - switch_t > 3.0:
                        notice = "RESCANNING NETWORK"
                        switch_t = time.monotonic()

                        def _rescan():
                            r = discovery.discover(timeout=5)
                            if r:
                                tgt["host"] = r[0]
                                tgt["stream"] = int(r[1].get("stream", tgt["stream"]))
                                tgt["steer"] = int(r[1].get("steer", tgt["steer"]))
                                tgt["name"] = str(r[1].get("name", tgt["name"]))
                                save_setting(settings_path, "video", "last_host", r[0])
                                start_decoder(cur_backend, tgt["host"], tgt["stream"])
                        threading.Thread(target=_rescan, daemon=True).start()
                        menu_open = False
                    elif sel == "Exit":
                        running = False
            elif e.type == pygame.JOYHATMOTION:
                last_activity = time.monotonic()            # wake from idle throttle
                # In the menu the D-pad moves the selection
                if menu_open:
                    if e.value[1] > 0:
                        menu_idx = (menu_idx - 1) % len(menu_items)
                    elif e.value[1] < 0:
                        menu_idx = (menu_idx + 1) % len(menu_items)

                # Otherwise the D-pad up/down scrolls the phrase, left/right the emotion
                else:
                    if phrases and e.value[1] > 0:
                        phrase_idx = (phrase_idx - 1) % len(phrases)
                    elif phrases and e.value[1] < 0:
                        phrase_idx = (phrase_idx + 1) % len(phrases)
                    if emotions and e.value[0] < 0:
                        emotion_idx = (emotion_idx - 1) % len(emotions)
                    elif emotions and e.value[0] > 0:
                        emotion_idx = (emotion_idx + 1) % len(emotions)
        if grace > 0:
            grace -= 1

        # ---- controls -> UDP @ ~30 Hz (every loop, loop is capped at 30) ----
        cmd = ctrl.read()
        if (cmd["fwd"] or cmd["turn"] or cmd["boost"] or cmd["action"]
                or cmd["estop"] or menu_open):           # held stick / button -> stay awake
            last_activity = time.monotonic()
        if grace == 0 and ctrl.quit_combo():        # Select+Start: hidden backup exit
            running = False
        if menu_open:        # while in the menu, don't drive — sticks navigate
            cmd = {"fwd": 0.0, "turn": 0.0, "boost": False, "estop": False,
                   "action": False, "connected": cmd["connected"]}
        seq += 1
        msg = {"type": "ctrl", "seq": seq, "t": now_ms(),
               "fwd": cmd["fwd"], "turn": cmd["turn"],
               "boost": bool(cmd["boost"]), "estop": bool(cmd["estop"]),
               "action": bool(cmd["action"])}
        try:
            steer_sock.sendto(json.dumps(msg).encode(), (tgt["host"], tgt["steer"]))
        except OSError:
            pass

        # ---- video ----
        # SELECT hides video: skip the decode/blit entirely and tell the operator.
        # The decoder is already stopped (so the robot stopped sending), so there's
        # nothing fresh in shmem to read.
        if not video_on:
            splash(screen, SW, SH, icon, "VIDEO OFF",
                   "press SELECT to resume", f_wait, f_row,
                   sub_dy=3 * f_row.get_linesize())
            vstate = "off"
        else:
            # Read a NON-TORN frame: copy the buffer, then re-check the published seq.
            # If the decoder advanced >= NBUF frames during our copy it has overwritten
            # the buffer we were reading (a burst lapped us) -> the copy may be torn ->
            # retry with the newest. Blit every loop so the translucent HUD composites once.
            fb = None; w = h = 0
            for _try in range(4):
                magic, vseq, w, h, bufstride, fmt = struct.unpack_from("<6I", mm, 0)
                if not (w and h):
                    break
                off = HDR + (vseq & (NBUF - 1)) * bufstride
                fb = mm[off:off + w * h * 3]
                vseq2 = struct.unpack_from("<I", mm, 4)[0]
                if ((vseq2 - vseq) & 0xffffffff) < NBUF:
                    break                        # buffer was not lapped -> intact
            if fb is not None and w and h:
                vstate = "connected"
                if cap_left > 0 and cap_n < 40:  # write to disk (NOT tmpfs), cap total count
                    with open(f"/mnt/UDISK/cap_{cap_n}.ppm", "wb") as cf:
                        cf.write(f"P6\n{w} {h}\n255\n".encode()); cf.write(fb)
                    cap_n += 1; cap_left -= 1
                else:
                    cap_left = 0
                surf = pygame.image.frombuffer(fb, (w, h), "RGB")
                if (w, h) != (SW, SH):
                    surf = pygame.transform.scale(surf, (SW, SH))
                screen.blit(surf, (0, 0))
                if vseq != last_seq:
                    last_seq = vseq
            else:
                splash(screen, SW, SH, icon, "AWAITING VIDEO LINK",
                       ("backend: " + ("software" if is_sw(cur_backend) else "hardware")),
                       f_wait, f_row, sub_dy=3 * f_row.get_linesize())

        # ---- HUD viewport frame: corner brackets + subtle center reticle ----
        fc = (0, 138, 162)
        for cx, cy, dx, dy in [(10, 10, 1, 1), (SW - 10, 10, -1, 1),
                               (10, SH - 10, 1, -1), (SW - 10, SH - 10, -1, -1)]:
            pygame.draw.line(screen, fc, (cx, cy), (cx + dx * 30, cy), 2)
            pygame.draw.line(screen, fc, (cx, cy), (cx, cy + dy * 30), 2)
        draw_reticle(screen, SW, SH)

        # ---- telemetry panel (cached ~8 Hz; AA text renders are costly) ----
        now = time.monotonic()
        td, link, rtt = tele.get()
        if hud_surf is None or now - hud_t >= 0.12:
            hud_t = now
            batt = td.get("batt", 0); spd = td.get("speed", 0); mode = td.get("mode", "?")
            estop = cmd["estop"] or td.get("estop"); boost = cmd["boost"]
            action = cmd["action"]
            rows = [
                (OKC, "ROBOT", str(tgt["name"]).upper()[:14], ACC),
                (OKC if link else ALERT, "LINK", f"ONLINE {rtt:.0f}MS" if link else "OFFLINE",
                 OKC if link else ALERT),
                (OKC if vstate == "connected" else DIM, "VIDEO",
                 "LIVE" if vstate == "connected" else vstate.upper(),
                 OKC if vstate == "connected" else DIM),
                (None, "MODE", "E-STOP" if estop else ("ACTION" if action else
                 ("SPEAK" if boost else str(mode).upper())),
                 ALERT if estop else (ACC if action else (WARN if boost else TXT))),
                (None, "SPEED", f"{spd:.2f} M/S", TXT),
                (None, "SYS", f"{render_fps:.0f} FPS {cpu.pct:.0f}% CPU", TXT),
            ]
            if "batt" in td:             # robot reports a battery -> show it
                rows.insert(3, (OKC if batt > 20 else ALERT, "POWER", f"{batt:.0f}%",
                                OKC if batt > 20 else ALERT))
            cb, cchg = handheld_battery()   # the controller's own battery, just under VIDEO
            if cb is not None:
                rows.insert(3, (OKC if cb > 20 else ALERT, "CONTROLLER",
                                f"{cb}% BATTERY" if cchg else f"{cb}%",
                                OKC if cb > 20 else ALERT))
            if not cmd["connected"]:     # only surface INPUT when the pad is missing
                rows.insert(len(rows) - 1, (ALERT, "INPUT", "NO PAD", ALERT))
            hud_surf = render_panel("TELEMETRY", rows, f_hdr, f_row, 322)
        screen.blit(hud_surf, (16, 16))

        # ---- periodic re-discovery while there's no video (robot may boot later) ----
        if auto and video_on and vstate != "connected" and not disco_busy[0] \
                and now - disco_t > 5 and now - switch_t > 3:
            disco_t = now; disco_busy[0] = True

            def _redisco():
                r = discovery.discover(timeout=4)
                if r and r[0] not in (tgt["host"], "0.0.0.0"):
                    tgt["host"] = r[0]
                    tgt["stream"] = int(r[1].get("stream", tgt["stream"]))
                    tgt["steer"] = int(r[1].get("steer", tgt["steer"]))
                    tgt["name"] = str(r[1].get("name", tgt["name"]))
                    save_setting(settings_path, "video", "last_host", r[0])
                    start_decoder(cur_backend, tgt["host"], tgt["stream"])
                disco_busy[0] = False
            threading.Thread(target=_redisco, daemon=True).start()

        # ---- throttle / steering gauges ----
        draw_axis(screen, 24, SH - 94, 360, 22, cmd["fwd"], ACC, "THR", f_row)
        draw_axis(screen, 24, SH - 52, 360, 22, cmd["turn"], ACC, "DIR", f_row)

        # ---- speak phrase chip, B says this, cached until the selection changes ----
        if phrases:
            chip_key = (phrase_idx, emotion_idx)
            if phrase_chip is None or chip_key != phrase_chip_key:
                emotion = emotions[emotion_idx] if emotions else "neutral"
                phrase_chip = render_chip(f"SAY \u25b8 [{emotion.upper()}] {phrases[phrase_idx].upper()}", f_chip)
                phrase_chip_key = chip_key
            screen.blit(phrase_chip, ((SW - phrase_chip.get_width()) // 2, SH - phrase_chip.get_height() - 18))

        if vstate == "connected" and not link:
            draw_banner(screen, SW, 22, "LINK LOST  --  ROBOT STOPPED", f_ban, ALERT)
        if now - switch_t < 2.5 and notice:
            draw_banner(screen, SW, SH // 2 + 30, notice, f_ban, WARN)

        if menu_open:
            draw_menu(screen, SW, SH, menu_fonts, menu_items, menu_idx,
                      sub=("SOFTWARE / PyAV" if is_sw(cur_backend) else "HARDWARE / CEDAR"))

        pygame.display.flip()

        fcount += 1
        if now - t_fps >= 1.0:
            render_fps = fcount / (now - t_fps); fcount = 0; t_fps = now
        if now - t_cpu >= 0.5:
            cpu.sample(); t_cpu = now
        clock.tick(idle_fps if (now - last_activity) > idle_after else fps)

    # final e-stop so the robot stops promptly on exit
    try:
        steer_sock.sendto(json.dumps({"type": "ctrl", "seq": seq + 1, "t": now_ms(),
                                      "fwd": 0, "turn": 0, "boost": False, "estop": True}).encode(),
                          (tgt["host"], tgt["steer"]))
    except OSError:
        pass
    # stop the decoder we own
    os.system("kill $(pidof hwdec_shmem) 2>/dev/null; pkill -9 -f src/sw_decode.py 2>/dev/null")
    pygame.quit()
    if restart:
        print("restarting", flush=True)
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
