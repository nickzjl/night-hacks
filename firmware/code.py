"""Night Hacks Braille cell firmware: CircuitPython on the Adafruit Feather M4 CAN Express.

Protocol (Mac -> board): one cell per line, two hex digits + newline, e.g. "19\n".
  bit0 = dot 1 (top left)   bit3 = dot 4 (top right)
  bit1 = dot 2 (mid left)   bit4 = dot 5 (mid right)
  bit2 = dot 3 (low left)   bit5 = dot 6 (low right)
Board replies "OK 19" after moving. "T" runs the self-test, "?" prints the current state.
All letter timing lives on the Mac; this board just holds whatever pattern it was sent.

Works on the plain USB console (no boot.py needed). If boot.py enables usb_cdc.data,
it listens on that port too. You can type "3F" + Enter in a serial terminal to test.

WIRING: servo signal wires -> PINS below. Servo power (red/brown) goes to a separate
5 V supply rated >= 3 A, NOT the Feather. Tie that supply's GND to the Feather's GND.
"""
import sys
import time

import board
import digitalio
import pwmio
import supervisor

try:
    import usb_cdc
except ImportError:
    usb_cdc = None

# ---- calibrate these on the real cell -----------------------------------------------------
PINS = (board.D5, board.D6, board.D9, board.D10, board.D11, board.D12)  # dots 1..6
DOWN = [30, 30, 30, 30, 30, 30]   # servo angle (degrees) with the dot lowered
UP = [75, 75, 75, 75, 75, 75]     # servo angle with the dot raised
PULSE_MIN_US = 500                # SG90-style servos: 0 deg ~ 500 us, 180 deg ~ 2500 us
PULSE_MAX_US = 2500
STAGGER_S = 0.02                  # move dots one after another to soften the current spike
RELEASE_AFTER_S = 0               # >0: stop pulses this long after a move (stops servo hum;
                                  # only if the mechanism holds raised dots by itself)
SELF_TEST = True                  # raise each dot once at boot
# --------------------------------------------------------------------------------------------


def duty(angle):
    pulse_us = PULSE_MIN_US + (PULSE_MAX_US - PULSE_MIN_US) * angle / 180
    return int(pulse_us * 65535 / 20000)  # 50 Hz -> 20 ms period


servos = []
for i, pin in enumerate(PINS):
    try:
        servos.append(pwmio.PWMOut(pin, frequency=50, duty_cycle=duty(DOWN[i])))
    except Exception as exc:  # usually a timer conflict: move this dot to another pin (A2-A5)
        print("ERR dot", i + 1, "on", pin, ":", exc)
        servos.append(None)

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

current = 0
released = False
moved_at = time.monotonic()


def apply(mask):
    global current, released, moved_at
    for i, servo in enumerate(servos):
        if servo is None:
            continue
        up = (mask >> i) & 1
        if released or up != (current >> i) & 1:
            servo.duty_cycle = duty(UP[i] if up else DOWN[i])
            time.sleep(STAGGER_S)
    current = mask
    released = False
    moved_at = time.monotonic()
    led.value = mask != 0


def release_if_idle():
    global released
    if RELEASE_AFTER_S and not released and time.monotonic() - moved_at > RELEASE_AFTER_S:
        for servo in servos:
            if servo is not None:
                servo.duty_cycle = 0
        released = True


def self_test():
    for i in range(6):
        apply(1 << i)
        time.sleep(0.3)
    apply(0)


def reply(port, text):
    if port is None:
        print(text)
    else:
        port.write((text + "\n").encode())


def handle(line, port):
    line = line.strip().upper()
    if not line:
        return
    if line == "T":
        self_test()
        reply(port, "OK T")
    elif line == "?":
        reply(port, "STATE %02X" % current)
    elif len(line) == 2:
        try:
            mask = int(line, 16) & 0x3F
        except ValueError:
            reply(port, "ERR " + line)
            return
        apply(mask)
        reply(port, "OK %02X" % mask)
    else:
        reply(port, "ERR " + line)


data_port = usb_cdc.data if usb_cdc is not None else None
console_buf = ""
data_buf = b""

if SELF_TEST:
    self_test()
print("READY")

while True:
    # USB console (the default CircuitPython serial port). Bytes arrive one at a time.
    while supervisor.runtime.serial_bytes_available:
        ch = sys.stdin.read(1)
        if ch in "\r\n":
            handle(console_buf, None)
            console_buf = ""
        else:
            console_buf += ch
    # Optional second CDC port, if boot.py turned it on.
    if data_port is not None and data_port.in_waiting:
        data_buf += data_port.read(data_port.in_waiting)
        while b"\n" in data_buf:
            raw, data_buf = data_buf.split(b"\n", 1)
            handle(raw.decode("utf-8", "ignore"), data_port)
    release_if_idle()
    time.sleep(0.002)
