import time

import pygame


class Controller:
    """Reads the first connected gamepad and maps it to a steering command.

    Axis/button indices vary by device — run `main.py --probe` on the TrimUI to
    discover the right numbers, then set them in settings.json under "controls".
    """

    def __init__(self, cfg):
        self.cfg = cfg
        pygame.joystick.init()
        self.pad = None
        if pygame.joystick.get_count() > 0:
            self.pad = pygame.joystick.Joystick(0)
            self.pad.init()
        self.height = 0.0            # wheel-lift setpoint, integrated from the stick
        self._height_t = None

    def _deadzone(self, v):
        dz = self.cfg.get("deadzone", 0.1)
        if abs(v) < dz:
            return 0.0
        # rescale so output starts at 0 right at the edge of the deadzone
        sign = 1.0 if v > 0 else -1.0
        return (v - sign * dz) / (1.0 - dz)

    def _axis(self, i):
        if self.pad and 0 <= i < self.pad.get_numaxes():
            return self.pad.get_axis(i)
        return 0.0

    def _button(self, i):
        if self.pad and 0 <= i < self.pad.get_numbuttons():
            return bool(self.pad.get_button(i))
        return False

    def read(self):
        if not self.pad:
            return {"fwd": 0.0, "turn": 0.0, "height": self.height, "rock": 0.0,
                    "boost": False, "estop": False, "action": False, "connected": False}

        # right stick drives, clamped to [-1, 1]
        fwd = self._deadzone(self._axis(self.cfg["drive_axis"]))
        fwd = max(-1.0, min(1.0, fwd))
        if not self.cfg.get("invert_drive"):   # stick up (negative axis) = forward
            fwd = -fwd
        turn = self._deadzone(self._axis(self.cfg["turn_axis"]))
        turn = max(-1.0, min(1.0, turn))

        # left stick Y integrates the wheel-lift height setpoint (up = higher);
        # it holds where you leave it, clamped to [0, height_max]
        now = time.monotonic()
        dt = min(now - self._height_t, 0.1) if self._height_t is not None else 0.0
        self._height_t = now
        lift = -self._deadzone(self._axis(self.cfg.get("height_axis", -1)))
        height_max = float(self.cfg.get("height_max", 0.5))
        self.height += lift * float(self.cfg.get("height_rate", 0.4)) * dt
        self.height = max(0.0, min(height_max, self.height))

        # left stick X rocks front/back (momentary, returns to 0 with the stick);
        # positive = fronts up / backs down
        rock = self._deadzone(self._axis(self.cfg.get("rock_axis", -1))) \
             * float(self.cfg.get("rock_max", 0.5))

        return {
            "fwd": round(fwd, 3),
            "turn": round(turn, 3),
            "height": round(self.height, 3),
            "rock": round(rock, 3),
            "boost": self._button(self.cfg.get("boost_button", -1)),
            "estop": self._button(self.cfg.get("estop_button", -1)),
            "action": self._button(self.cfg.get("action_button", -1)),
            "connected": True,
        }

    def quit_combo(self):
        combo = self.cfg.get("quit_buttons") or []
        if not self.pad or not combo:
            return False
        return all(self._button(b) for b in combo)
