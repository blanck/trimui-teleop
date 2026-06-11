"""Robot-side helper for the trimui-teleop protocol (docs/PROTOCOL.md).

Handles the four things every robot adapter needs — LAN discovery, the control
UDP listener, the safety watchdog, and telemetry — so writing an adapter for a
new platform is just:

    from robot_link import RobotLink

    def drive(fwd, turn, boost, estop):     # fwd/turn in -1..1; estop -> stop
        ...                                  # actuate your motors here

    link = RobotLink(name="my-robot", on_control=drive,
                     on_action=lambda: ...)  # optional: A button, once per press
    link.set_telemetry(batt=87, speed=0.4, mode="drive")   # optional, anytime
    link.run()                               # blocks; or link.start() to background it

See integrations/ for real examples (ROS 2, Raspberry Pi GPIO, DonkeyCar). The
client (the handheld) never changes — every adapter speaks this same protocol.
"""
import json
import socket
import threading
import time

import discovery

CTRL_PORT, TELE_PORT, STREAM_PORT = 49602, 49603, 49601


class RobotLink:
    def __init__(self, name="robot", on_control=None, on_action=None, watchdog=0.5,
                 ctrl_port=CTRL_PORT, tele_port=TELE_PORT, stream_port=STREAM_PORT):
        self.name = name
        self.on_control = on_control          # callback(fwd, turn, boost, estop)
        self.on_action = on_action            # callback(); fired once per A press
        self.watchdog = watchdog              # stop if no control for this long (s)
        self.ctrl_port, self.tele_port, self.stream_port = ctrl_port, tele_port, stream_port
        self.tele = {"speed": 0.0, "mode": "idle"}   # no batt until set_telemetry(batt=...)
        self.device_ip = None
        self._last_rx = 0.0
        self._ack_seq = self._ack_t = 0
        self._action_prev = False
        self._stop = False
        self._rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._rx.bind(("0.0.0.0", ctrl_port))
        self._rx.settimeout(0.5)
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def set_telemetry(self, **kw):
        """Update any of batt / speed / mode; sent back to the handheld at 10 Hz."""
        self.tele.update(kw)

    def start(self):
        """Begin serving in background threads (discovery + control + telemetry)."""
        discovery.respond({"stream": self.stream_port, "steer": self.ctrl_port,
                           "tele": self.tele_port, "name": self.name})
        for fn in (self._rx_loop, self._tele_loop, self._wd_loop):
            threading.Thread(target=fn, daemon=True).start()

    def run(self):
        """Serve and block until Ctrl-C."""
        self.start()
        try:
            while not self._stop:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        self._stop = True

    def stop(self):
        self._stop = True

    # --- internals ---
    def _emit(self, fwd, turn, boost, estop):
        if self.on_control:
            self.on_control(fwd, turn, boost, estop)

    def _rx_loop(self):
        while not self._stop:
            try:
                data, addr = self._rx.recvfrom(2048)
                m = json.loads(data.decode())
            except socket.timeout:
                continue
            except Exception:
                continue
            if m.get("type", "ctrl") != "ctrl":
                continue
            self.device_ip = addr[0]
            self._last_rx = time.monotonic()
            self._ack_seq, self._ack_t = int(m.get("seq", 0)), int(m.get("t", 0))
            if m.get("estop"):
                self._emit(0.0, 0.0, False, True)
            else:
                self._emit(float(m.get("fwd", 0.0)), float(m.get("turn", 0.0)),
                           bool(m.get("boost", False)), False)
            act = bool(m.get("action", False))
            if act and not self._action_prev and self.on_action:
                self.on_action()
            self._action_prev = act

    def _wd_loop(self):
        while not self._stop:
            if self._last_rx and time.monotonic() - self._last_rx > self.watchdog:
                self._emit(0.0, 0.0, False, True)          # link stalled -> stop
            time.sleep(self.watchdog / 2)

    def _tele_loop(self):
        while not self._stop:
            if self.device_ip:
                stale = self._last_rx and time.monotonic() - self._last_rx > self.watchdog
                m = {"type": "tele", "t": int(time.monotonic() * 1000),
                     "speed": round(self.tele["speed"], 2),
                     "mode": "lost" if stale else self.tele["mode"],
                     "ack_seq": self._ack_seq, "ack_t": self._ack_t}
                if "batt" in self.tele:   # only report a battery the adapter has set
                    m["batt"] = round(self.tele["batt"], 1)
                msg = json.dumps(m).encode()
                try:
                    self._tx.sendto(msg, (self.device_ip, self.tele_port))
                except OSError:
                    pass
            time.sleep(0.1)
