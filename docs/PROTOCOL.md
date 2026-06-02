# Teleop link protocol

The contract between the **TrimUI handheld** (controller) and the **robot** (the
demo sim, a ROS bridge, a Pi rover, a real robot — anything). Anything that speaks
this is drivable by the handheld with **no client changes**.

Three independent links, all over the LAN:

| link | dir | transport | default port |
|------|-----|-----------|------|
| discovery | both | UDP broadcast | 49600 |
| video | robot → handheld | TCP (H.264 Annex-B) | 49601 |
| control | handheld → robot | UDP | 49602 |
| telemetry | robot → handheld | UDP | 49603 |

UDP messages are single datagrams of one JSON object (UTF-8, no framing). Times are
integer **milliseconds from each sender's own monotonic clock** — only ever compared
against values from the *same* clock (see RTT), so the two sides never need synced clocks.

## Discovery (so there's no IP to configure)

The robot answers a broadcast query; the handheld takes the responder's source IP.

```
handheld --broadcast--> {"q":"trimui-teleop"}
robot    --unicast----> {"svc":"trimui-teleop","stream":49601,"steer":49602,
                         "tele":49603,"name":"robot-sim"}
```

The handheld remembers the IP and probes it directly next boot (instant reconnect),
falling back to a broadcast scan. `name` is shown in the HUD so you see what you found.

## Control — handheld → robot (udp/49602)

Sent at **30 Hz** while the app shows video, plus one final `estop` frame on exit.

```json
{"type":"ctrl","seq":1234,"t":880123,"fwd":0.62,"turn":-0.35,"boost":false,"estop":false}
```

| field | meaning |
|-------|---------|
| `seq` | monotonically increasing control counter |
| `t`   | handheld monotonic ms when sent (robot echoes it back for RTT) |
| `fwd` | drive, −1..1 (forward +) |
| `turn`| steer, −1..1 (right +) |
| `boost` / `estop` | momentary buttons |

`fwd`/`turn` are **normalized** — the robot side scales them to its own units
(m/s + rad/s for ROS, wheel PWM for a Pi, throttle/steering for DonkeyCar, …).

## Telemetry — robot → handheld (udp/49603)

Robot replies to the **source IP of the control packets**, at **10 Hz**.

```json
{"type":"tele","t":12044,"batt":83.4,"speed":0.91,"mode":"drive",
 "ack_seq":1234,"ack_t":880123}
```

| field | meaning |
|-------|---------|
| `batt`| battery %, 0..100 |
| `speed`| speed magnitude (m/s or sim units) |
| `mode`| `"idle"` / `"drive"` / `"estop"` / `"lost"` / freeform |
| `ack_seq` / `ack_t` | last control `seq`/`t` the robot acted on (for RTT) |

## Link health & safety

- **Robot watchdog:** no control for **>0.5 s** → stop motors, report `mode:"lost"`.
  A real robot MUST do this.
- **Handheld link state:** `LINK OK` if telemetry seen within **1 s**, else `LINK LOST`
  (which shows the "ROBOT STOPPED" banner).
- **RTT** (handheld clock): `rtt = now_ms − tele.ack_t`. `ack_t` is the handheld's own
  timestamp echoed back, so no clock sync is needed.

## Writing a robot adapter

A robot adapter does four things: **answer discovery, receive control, run a
watchdog, send telemetry.** `src/robot_link.py` does all four — you just provide a
drive callback:

```python
from robot_link import RobotLink          # needs src/ on the path

def drive(fwd, turn, boost, estop):        # fwd/turn in −1..1; estop → stop
    ...                                     # actuate your motors

link = RobotLink(name="my-robot", on_control=drive)
link.set_telemetry(batt=87, speed=0.4, mode="drive")   # optional, anytime
link.run()                                  # also serves video? no — see below
```

Video is separate: run `robot_sim/h264_server.py` on the robot's camera (or any
single-slice H.264-over-TCP source on `stream` port — see HWDECODE.md for why
single-slice). Worked examples: `robot_sim/robot.py` (no deps), and `integrations/`
(`ros2_bridge.py`, `pi_gpio_bridge.py`, `donkeycar_part.py`).
