"""App configuration.

All defaults live here, so the app runs with **no config file** — it uses these
values, finds the robot by LAN discovery, and the in-app menu manages anything
worth persisting (the video backend). An optional settings.json may override any
field; the app writes one itself when you change the backend from the menu.
"""
import copy
import json
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # app root

DEFAULTS = {
    "video": {
        "backend": "sw",              # "sw" (PyAV, clean) or "shmem" (Cedar HW); menu toggles it
        "shmem": "/tmp/hwframe",
        "stream_host": "auto",        # "auto" = LAN discovery; or an explicit IP
        "stream_port": 49601,         # used if the robot doesn't advertise one
        "fallback_host": "127.0.0.1",
    },
    "steer": {"host": "auto", "port": 49602, "hz": 30},
    "telemetry": {"port": 49603},
    "audio": {"enabled": False},      # local sound off (handheld is silent — phrases
                                      # play on the robot). Set True for robot-mic playback later.
    "controls": {                     # defaults for the TrimUI Smart Pro gamepad
        "drive_axis": 1, "turn_axis": 3,      # left stick Y, right stick X
        "drive_axis2": 4, "turn_axis2": 0,    # right stick Y, left stick X
        "invert_drive": False, "deadzone": 0.12,
        "boost_button": 0, "estop_button": 4,  # B / L shoulder
        "action_button": 1,                    # A — passed through to the robot
        "menu_button": 8, "confirm_button": 1, "quit_buttons": [6, 7],
        "restart_button": 7,                   # START — restart the app
        "video_toggle_button": 6,              # SELECT — show/hide video (and tell robot to stop sending)
        "gesture_cycle_button": 2,             # X — cycle the selected gesture
        "gesture_button": 3,                   # Y — perform the selected gesture
    },
    "screen": {"width": 1280, "height": 720, "fullscreen": True, "fps": 30,
               "idle_fps": 5, "idle_after_s": 5.0},  # throttle to idle_fps after idle_after_s of no input
    "phrases": [                       # D-pad up/down selects, B speaks on the robot
        "hi",
        "hello there",
        "out of my way please",
        "beep boop",
        "i am a robot",
        "nice to meet you",
        "oh no",
        "thank you",
        "annyeonghaseyo",
        "bangapseumnida",
        "gamsahamnida",
        "joesonghamnida",
    ],
    "gestures": [                      # X cycles, Y performs on the robot
        "wave",
        "shrug",
        "point",
    ],
    "emotions": [                      # D-pad left/right selects, prepended as a v3 tag
        "neutral",
        "cheerfully",
        "excited",
        "whispers",
        "shouts",
        "sad",
        "nervous",
        "angry",
        "playfully",
        "robotic",
    ],
}


def default_path():
    return os.path.join(_HERE, "settings.json")


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load(path=None):
    """Return DEFAULTS, with an optional settings.json merged on top (if present)."""
    path = path or default_path()
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path) as f:
            _merge(cfg, json.load(f))
    except (FileNotFoundError, ValueError):
        pass
    return cfg
