"""Zero-config LAN discovery over UDP broadcast — no dependencies.

The robot/server advertises itself; the handheld finds it, so you never have to
put an IP in settings.json. Both sides just need to be on the same network.

Wire protocol (UDP broadcast, port 49600):
    handheld  --broadcast-->  {"q": "trimui-teleop"}
    robot     --unicast--->   {"svc":"trimui-teleop","stream":49601,"steer":49602,
                               "tele":49603,"name":"robot"}
The handheld takes the responder's *source IP* as the host.

Robot side:   discovery.respond({"stream":49601,"steer":49602,"tele":49603,"name":"r1"})
Handheld:     host, info = discovery.discover()   # or None
"""
import json
import socket
import threading
import time

SVC = "trimui-teleop"
PORT = 49600                  # UDP broadcast rendezvous (private range, uncommon)


def respond(info, port=PORT, svc=SVC):
    """Start a daemon thread that answers discovery queries with `info` (a dict).
    Returns the thread. Call once on the robot/server."""
    def run():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("0.0.0.0", port))
        while True:
            try:
                data, addr = s.recvfrom(1024)
                if json.loads(data.decode()).get("q") == svc:
                    reply = dict(info); reply["svc"] = svc
                    s.sendto(json.dumps(reply).encode(), addr)
            except Exception:
                pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def probe(host, port=PORT, svc=SVC, timeout=1.2):
    """Targeted check: unicast a query to one host and see if it's our robot.
    Returns (host_ip, info) if it answers, else None. Used to reconnect fast to
    the last-known IP before falling back to a broadcast scan."""
    if not host:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.4)
    req = json.dumps({"q": svc}).encode()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                s.sendto(req, (host, port))
            except OSError:
                return None
            try:
                data, addr = s.recvfrom(1024)
                m = json.loads(data.decode())
                if m.get("svc") == svc:
                    return addr[0], m
            except socket.timeout:
                continue
            except Exception:
                continue
    finally:
        s.close()
    return None


def discover(port=PORT, svc=SVC, timeout=8.0):
    """Broadcast a query and return (host_ip, info_dict) of the first responder,
    or None after `timeout` seconds. Re-broadcasts every 0.5 s."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    req = json.dumps({"q": svc}).encode()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            for tgt in ("255.255.255.255", "<broadcast>"):
                try:
                    s.sendto(req, (tgt, port))
                except OSError:
                    pass
            try:
                data, addr = s.recvfrom(1024)
                m = json.loads(data.decode())
                if m.get("svc") == svc:
                    return addr[0], m
            except socket.timeout:
                continue
            except Exception:
                continue
    finally:
        s.close()
    return None
