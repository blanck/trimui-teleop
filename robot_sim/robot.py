"""Teleop robot simulator (desktop stand-in for the real robot).

Implements the robot end of PROTOCOL.md:
  - receives CONTROL on udp/49602 from the TrimUI device,
  - simulates a differential-drive robot (motion + draining battery),
  - sends TELEMETRY back to the device on udp/49603 at 10 Hz,
  - enforces the safety watchdog (stop if no control for >0.5 s),
  - shows a live window: link state, battery, FWD/TURN, and the robot driving.

    python robot_sim/robot.py [ctrl_port] [tele_port]      # defaults 49602 49603

Uses tkinter (built into Python) for the GUI so it works on any Python — pygame's
font module is broken on Python 3.14.
"""
import glob
import json
import math
import os
import socket
import sys
import time

# uv's bundled Python doesn't set TCL_LIBRARY, so tkinter (this sim's GUI) can't
# find init.tcl. Point it at the bundled tcl/tk before importing tkinter so the
# sim runs under `uv run` with no extra setup. (No-op on Pythons that find Tk.)
if "TCL_LIBRARY" not in os.environ:
    for _p in sorted(glob.glob(os.path.join(sys.base_prefix, "lib", "tcl8.*"))):
        if os.path.isfile(os.path.join(_p, "init.tcl")):
            os.environ["TCL_LIBRARY"] = _p
            _tk = _p.replace("tcl8", "tk8")
            if os.path.isdir(_tk):
                os.environ["TK_LIBRARY"] = _tk
            break

import tkinter as tk     # noqa: E402

CTRL_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 49602
TELE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 49603
STREAM_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 49601   # advertised video port
W, H = 820, 600
BG = "#101218"; PANEL = "#181a22"; TRACK = "#282832"
GREEN = "#00dc78"; RED = "#e63c3c"; CYAN = "#00c8ff"; ORANGE = "#ffa000"
GREY = "#969696"; WHITE = "#ebebf0"; DIM = "#82828c"


def now_ms():
    return int(time.monotonic() * 1000)


class Robot:
    def __init__(self, root):
        self.root = root
        root.title(f"Teleop — robot sim (ctrl udp/{CTRL_PORT}  tele udp/{TELE_PORT})")
        root.configure(bg=BG)
        self.c = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0)
        self.c.pack()

        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("0.0.0.0", CTRL_PORT)); self.rx.setblocking(False)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.fwd = self.turn = 0.0
        self.boost = self.estop = False
        self.ctrl_seq = 0; self.ctrl_t = 0; self.ctrl_pkts = 0
        self.last_rx = 0.0; self.device_ip = None
        self.view = (430, 150, 800, 560)        # x0,y0,x1,y1
        self.px = (430 + 800) / 2; self.py = (150 + 560) / 2
        self.heading = -math.pi / 2; self.speed = 0.0; self.batt = 100.0
        self.mode = "idle"; self.trail = []
        self.tele_seq = 0; self.last_tele = 0.0; self.last_t = time.monotonic()
        root.after(16, self.tick)

    def tick(self):
        now = time.monotonic(); dt = min(0.05, now - self.last_t); self.last_t = now

        while True:
            try:
                data, addr = self.rx.recvfrom(2048)
            except BlockingIOError:
                break
            try:
                m = json.loads(data.decode())
                if m.get("type", "ctrl") == "ctrl":
                    self.fwd = float(m.get("fwd", 0)); self.turn = float(m.get("turn", 0))
                    self.boost = bool(m.get("boost", False)); self.estop = bool(m.get("estop", False))
                    self.ctrl_seq = int(m.get("seq", 0)); self.ctrl_t = int(m.get("t", 0))
                    self.device_ip = addr[0]; self.last_rx = now; self.ctrl_pkts += 1
            except Exception:
                pass

        link = (now - self.last_rx) < 0.5 and self.last_rx > 0
        if not link:
            self.mode = "lost"; self.fwd = self.turn = 0.0; self.speed = 0.0
        elif self.estop:
            self.mode = "estop"; self.speed = 0.0
        else:
            self.mode = "drive" if abs(self.fwd) > 0.02 or abs(self.turn) > 0.02 else "idle"

        x0, y0, x1, y1 = self.view
        if link and not self.estop:
            self.heading += self.turn * 2.4 * dt
            target = self.fwd * 150.0 * (2.0 if self.boost else 1.0)
            self.speed += (target - self.speed) * min(1, 6 * dt)
            self.px = max(x0 + 16, min(x1 - 16, self.px + self.speed * math.cos(self.heading) * dt))
            self.py = max(y0 + 16, min(y1 - 16, self.py + self.speed * math.sin(self.heading) * dt))
            self.trail.append((self.px, self.py)); self.trail = self.trail[-140:]
            self.batt = max(0.0, self.batt - (0.02 + abs(self.speed) / 150.0 * 0.25) * dt)

        if self.device_ip and now - self.last_tele >= 0.1:
            self.last_tele = now; self.tele_seq += 1
            msg = json.dumps({"type": "tele", "seq": self.tele_seq, "t": now_ms(),
                              "batt": round(self.batt, 1), "speed": round(self.speed / 150.0, 2),
                              "mode": self.mode, "estop": self.estop,
                              "ack_seq": self.ctrl_seq, "ack_t": self.ctrl_t}).encode()
            try:
                self.tx.sendto(msg, (self.device_ip, TELE_PORT))
            except OSError:
                pass

        self.draw(link)
        # redrawing the whole canvas is the cost; 30 fps while driving is plenty,
        # and idle (no client) drops to ~8 fps so the sim sits near-zero CPU.
        self.root.after(33 if link else 200, self.tick)

    def text(self, x, y, s, color=WHITE, size=14, anchor="w", bold=False):
        self.c.create_text(x, y, text=s, fill=color, anchor=anchor,
                           font=("Menlo", size, "bold" if bold else "normal"))

    def bar(self, x, y, w, h, value, color, label, signed=True, fmt="{:+.2f}"):
        self.c.create_rectangle(x, y, x + w, y + h, fill=TRACK, outline="")
        if signed:
            cx = x + w / 2; fill = value * (w / 2)
            if fill >= 0:
                self.c.create_rectangle(cx, y, cx + fill, y + h, fill=color, outline="")
            else:
                self.c.create_rectangle(cx + fill, y, cx, y + h, fill=color, outline="")
            self.c.create_line(cx, y - 2, cx, y + h + 2, fill="#c8c8c8", width=2)
        else:
            self.c.create_rectangle(x, y, x + max(0, min(1, value)) * w, y + h, fill=color, outline="")
        self.text(x + w + 12, y + h / 2, label, DIM)
        self.text(x + w + 84, y + h / 2, fmt.format(value), color)

    def draw(self, link):
        c = self.c; c.delete("all")
        self.text(24, 30, "ROBOT SIM", GREEN, 26, bold=True)
        self.text(24, 78, "LINK:  " + ("OK" if link else "LOST — stopped"), GREEN if link else RED)
        self.text(24, 108, f"FROM:  {self.device_ip or '—'}", CYAN if link else DIM)
        self.text(24, 138, f"MODE:  {self.mode.upper()}", RED if self.mode in ("estop", "lost") else WHITE)

        self.bar(24, 200, 240, 22, self.fwd, CYAN, "FWD")
        self.bar(24, 256, 240, 22, self.turn, ORANGE, "TURN")
        bcol = GREEN if self.batt > 40 else ORANGE if self.batt > 15 else RED
        self.bar(24, 336, 240, 22, self.batt / 100.0, bcol, "BATT", signed=False, fmt="{:.0f}%")
        self.bar(24, 378, 240, 22, abs(self.speed) / 300.0, CYAN, "SPD", signed=False, fmt="{:.2f}")

        x0, y0, x1, y1 = self.view
        c.create_rectangle(x0, y0, x1, y1, fill=PANEL, outline="#007850")
        self.text(x0 + 12, y0 + 16, "ROBOT", DIM)
        if len(self.trail) > 1:
            c.create_line(*[v for p in self.trail for v in p], fill="#285046", width=2)
        col = RED if self.mode in ("estop", "lost") else CYAN
        ca, sa = math.cos(self.heading), math.sin(self.heading)
        pts = [(22, 0), (-14, -12), (-14, 12)]      # arrow body in robot frame
        poly = [(self.px + dx * ca - dy * sa, self.py + dx * sa + dy * ca) for dx, dy in pts]
        c.create_polygon(*[v for p in poly for v in p], fill=col, outline=WHITE)

        if not link:
            self.text(W / 2, H - 30, "waiting for control…", DIM, 18, anchor="c")


def main():
    # advertise on the LAN so the handheld finds us with no IP config (src/discovery.py)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    try:
        import discovery
        discovery.respond({"stream": STREAM_PORT, "steer": CTRL_PORT,
                           "tele": TELE_PORT, "name": "robot-sim"})
        print(f"discovery: advertising stream={STREAM_PORT} steer={CTRL_PORT} "
              f"tele={TELE_PORT}", flush=True)
    except Exception as e:
        print("discovery responder not started:", e)
    root = tk.Tk()
    try:                         # app icon on the window (best effort across platforms)
        ic = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "res", "icon.png")
        root.iconphoto(True, tk.PhotoImage(file=ic))
    except Exception:
        pass
    Robot(root)
    root.mainloop()


if __name__ == "__main__":
    main()
