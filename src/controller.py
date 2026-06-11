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
            return {"fwd": 0.0, "turn": 0.0, "boost": False,
                    "estop": False, "action": False, "connected": False}

        # either stick drives: sum both sticks' contributions, clamped to [-1, 1]
        fwd = self._deadzone(self._axis(self.cfg["drive_axis"])) \
            + self._deadzone(self._axis(self.cfg.get("drive_axis2", -1)))
        fwd = max(-1.0, min(1.0, fwd))
        if not self.cfg.get("invert_drive"):   # stick up (negative axis) = forward
            fwd = -fwd
        turn = self._deadzone(self._axis(self.cfg["turn_axis"])) \
             + self._deadzone(self._axis(self.cfg.get("turn_axis2", -1)))
        turn = max(-1.0, min(1.0, turn))

        return {
            "fwd": round(fwd, 3),
            "turn": round(turn, 3),
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
