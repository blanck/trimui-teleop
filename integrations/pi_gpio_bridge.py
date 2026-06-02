#!/usr/bin/env python3
"""Raspberry Pi GPIO adapter for trimui-teleop — runs ON the Pi.

Drives a 2-motor differential-drive rover through a motor driver (L298N, TB6612,
DRV8833, ...) using gpiozero. The handheld's normalized fwd/turn are mixed into
left/right wheel speeds; discovery, telemetry and the safety watchdog come from
RobotLink, so this file is basically just the motor mixing.

  pip install gpiozero            # with RPi.GPIO or lgpio backend
  python3 integrations/pi_gpio_bridge.py

Video: run robot_sim/h264_server.py on the Pi camera so the handheld has a feed
(this adapter is control + telemetry only). The pins below are BCM numbers for a
typical L298N (IN1..IN4) — adjust for your wiring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from robot_link import RobotLink     # noqa: E402

from gpiozero import Robot           # noqa: E402

# left motor = (forward_pin, backward_pin); right motor = (forward_pin, backward_pin)
rover = Robot(left=(17, 18), right=(22, 23))
NORMAL = 0.6                          # normal max throttle; the boost button -> 1.0


def clamp(v):
    return max(-1.0, min(1.0, v))


def drive(fwd, turn, boost, estop):
    if estop:
        rover.stop()
        link.set_telemetry(speed=0.0, mode="estop")
        return
    s = 1.0 if boost else NORMAL
    rover.value = (clamp((fwd + turn) * s), clamp((fwd - turn) * s))   # differential mix
    moving = abs(fwd) > 0.02 or abs(turn) > 0.02
    link.set_telemetry(speed=round(abs(fwd) * s, 2), mode="drive" if moving else "idle")


link = RobotLink(name="pi-gpio", on_control=drive)

if __name__ == "__main__":
    print("pi_gpio_bridge: differential drive, Robot(left=(17,18), right=(22,23))")
    link.run()
