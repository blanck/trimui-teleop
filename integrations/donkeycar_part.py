"""DonkeyCar part for trimui-teleop — drive a DonkeyCar from the handheld.

Drop this in as a DonkeyCar *part* to use the TrimUI as the controller (in place
of the web / joystick controller). The mapping is exact: the handheld's `turn`
becomes steering `angle`, `fwd` becomes `throttle` (both -1..1, DonkeyCar's own
convention). The boost button toggles `recording`, so you can collect training
data while driving from the handheld.

In your DonkeyCar manage.py:

    from donkeycar_part import TrimuiTeleop
    V.add(TrimuiTeleop(),
          outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],
          threaded=True)

Video: run robot_sim/h264_server.py on the Pi camera so the handheld has a feed
(your DonkeyCar camera part still feeds the autopilot as usual).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from robot_link import RobotLink     # noqa: E402


class TrimuiTeleop:
    """Threaded DonkeyCar part: outputs (angle, throttle, mode, recording)."""

    def __init__(self, name="donkeycar"):
        self.angle = 0.0
        self.throttle = 0.0
        self.mode = "user"
        self.recording = False
        self._boost_prev = False
        self.link = RobotLink(name=name, on_control=self._on_control)
        self.link.start()

    def _on_control(self, fwd, turn, boost, estop):
        self.angle = 0.0 if estop else turn        # steering  (-1..1)
        self.throttle = 0.0 if estop else fwd      # throttle  (-1..1)
        if boost and not self._boost_prev:         # rising edge -> toggle data recording
            self.recording = not self.recording
        self._boost_prev = boost
        self.link.set_telemetry(speed=round(abs(self.throttle), 2),
                                mode="recording" if self.recording else "user")

    def run_threaded(self):
        return self.angle, self.throttle, self.mode, self.recording

    def run(self):                                  # non-threaded fallback
        return self.run_threaded()

    def shutdown(self):
        self.link.stop()
