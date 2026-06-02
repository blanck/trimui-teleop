#!/usr/bin/env python3
"""ROS 2 adapter for trimui-teleop — run on the robot/PC where ROS 2 is installed.

Translates the handheld's protocol to/from standard ROS 2 topics, so the TrimUI
drives any ROS 2 robot with no handheld changes:

  control    handheld fwd/turn (-1..1)  ->  geometry_msgs/Twist on /cmd_vel
                                            (linear.x = fwd*max_lin, angular.z = turn*max_ang)
  telemetry  /battery_state, /odom       ->  handheld telemetry
  video      run robot_sim/h264_server.py on the robot camera (bridge is control only)

Discovery, watchdog and telemetry plumbing come from RobotLink, so this is just
the unit conversion. Drop-in alternative to robot_sim/robot.py — same protocol.

Requires a ROS 2 install (rclpy + common_msgs). Reference file: not run by the
repo's tests since they don't assume ROS is present.

  python3 integrations/ros2_bridge.py [--cmd-topic /cmd_vel] [--max-lin 0.6] [--max-ang 1.5]
"""
import argparse
import os
import sys

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
try:
    from sensor_msgs.msg import BatteryState
    from nav_msgs.msg import Odometry
except Exception:
    BatteryState = Odometry = None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from robot_link import RobotLink     # noqa: E402


class Bridge(Node):
    def __init__(self, args):
        super().__init__("trimui_teleop_bridge")
        self.max_lin, self.max_ang = args.max_lin, args.max_ang
        self.pub = self.create_publisher(Twist, args.cmd_topic, 10)
        if BatteryState:
            self.create_subscription(BatteryState, "/battery_state", self._batt, 10)
        if Odometry:
            self.create_subscription(Odometry, "/odom", self._odom, 10)
        self.link = RobotLink(name="ros2-bridge", on_control=self._drive)
        self.link.start()
        self.get_logger().info(
            f"bridge up — UDP control -> {args.cmd_topic}; run h264_server.py for video")

    def _drive(self, fwd, turn, boost, estop):
        t = Twist()
        if not estop:
            boost_mul = 2.0 if boost else 1.0
            t.linear.x = fwd * self.max_lin * boost_mul
            t.angular.z = turn * self.max_ang
        self.pub.publish(t)
        self.link.set_telemetry(speed=abs(t.linear.x),
                                mode="estop" if estop else "drive")

    def _batt(self, msg):
        if getattr(msg, "percentage", -1) >= 0:
            self.link.set_telemetry(batt=msg.percentage * 100.0)

    def _odom(self, msg):
        self.link.set_telemetry(speed=abs(msg.twist.twist.linear.x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd-topic", default="/cmd_vel")
    ap.add_argument("--max-lin", type=float, default=0.6, help="m/s at full stick")
    ap.add_argument("--max-ang", type=float, default=1.5, help="rad/s at full stick")
    args = ap.parse_args()
    rclpy.init()
    node = Bridge(args)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.link.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
