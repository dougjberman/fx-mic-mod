# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors

# SPDX-License-Identifier: MIT
0































import array
import math

import audiodelays
import synthio



ADDRESS = 0x18
WHO_AM_I = 0x0F



KNOWN_IDS = {0x33: "LIS3DH", 0x11: "SC7A20"}
CTRL_REG1 = 0x20
CTRL_REG4 = 0x23


REG1_100HZ = 0x57
REG1_ASLEEP = 0x07

REG4_4G = 0x90


OUT_X_L = 0x28 | 0x80

COUNTS_PER_G = 8192



READ_SECONDS = 0.02


GRAVITY_SMOOTHING = 0.1


SHAKE_FROM_G = 0.5
SHAKE_FULL_G = 1.6


ATTACK_SECONDS = 0.03
RELEASE_SECONDS = 0.25


class Accelerometer:
    0


    def __init__(self, i2c):
        self.i2c = i2c
        self._one = bytearray(1)
        self._six = bytearray(6)
        who = self._read(WHO_AM_I, self._one)[0]
        if who not in KNOWN_IDS:
            raise ValueError("unknown accelerometer id 0x{:02x}".format(who))
        self.name = KNOWN_IDS[who]
        self._write(CTRL_REG4, REG4_4G)
        self.awake = True
        self.sleep()

    def wake(self):
        if not self.awake:
            self._write(CTRL_REG1, REG1_100HZ)
            self.awake = True

    def sleep(self):
        if self.awake:
            self._write(CTRL_REG1, REG1_ASLEEP)
            self.awake = False

    def _lock(self):

        while not self.i2c.try_lock():
            pass

    def _read(self, register, buf):
        self._lock()
        try:
            self.i2c.writeto_then_readfrom(ADDRESS, bytes((register,)), buf)
        finally:
            self.i2c.unlock()
        return buf

    def _write(self, register, value):
        self._lock()
        try:
            self.i2c.writeto(ADDRESS, bytes((register, value)))
        finally:
            self.i2c.unlock()

    def read(self):
        0
        b = self._read(OUT_X_L, self._six)
        out = []
        for i in (0, 2, 4):
            value = b[i] | (b[i + 1] << 8)
            if value & 0x8000:
                value -= 0x10000
            out.append(value / COUNTS_PER_G)
        return out


class ShakeMeter:
    0

    def __init__(self, accelerometer):
        self.accelerometer = accelerometer
        self.name = accelerometer.name
        self.amount = 0.0
        self.gravity = None
        self.read_at = None

    @classmethod
    def find(cls, i2c):
        0
        try:
            return cls(Accelerometer(i2c))
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            print("shake: no accelerometer ({})".format(error))
            return None

    @property
    def awake(self):
        return self.accelerometer.awake

    def reset(self):
        0
        self.amount = 0.0
        self.read_at = None
        self.gravity = None
        try:
            self.accelerometer.sleep()
        except OSError:
            pass

    def update(self, now):
        0

        if not self.accelerometer.awake:
            try:
                self.accelerometer.wake()
            except OSError:
                return self.amount
            self.read_at = now
            return self.amount
        if self.read_at is not None and now - self.read_at < READ_SECONDS:
            return self.amount
        dt = READ_SECONDS if self.read_at is None else min(now - self.read_at, 0.2)
        self.read_at = now
        try:
            a = self.accelerometer.read()
        except OSError:
            return self.amount
        g = self.gravity
        if g is None:
            self.gravity = g = list(a)
        for i in range(3):
            g[i] += (a[i] - g[i]) * GRAVITY_SMOOTHING
        off = math.sqrt((a[0] - g[0]) ** 2 + (a[1] - g[1]) ** 2 + (a[2] - g[2]) ** 2)
        target = min(1.0, max(0.0, (off - SHAKE_FROM_G) / (SHAKE_FULL_G - SHAKE_FROM_G)))
        tau = ATTACK_SECONDS if target > self.amount else RELEASE_SECONDS
        self.amount += (target - self.amount) * (1 - math.exp(-dt / tau))
        if target == 0 and self.amount < 0.002:
            self.amount = 0.0
        return self.amount








STYLES = {
    "zip": {"rate": 2.4, "feedback": 0.8, "mix": 0.3, "shortest_ms": 20, "longest_ms": 140},
    "warble": {"rate": 5.5, "feedback": 0.0, "mix": 1.0, "shortest_ms": 42, "longest_ms": 56},
    "glitch": {"rate": 1.7, "feedback": 0.82, "mix": 0.85, "shortest_ms": 32, "longest_ms": 300},
}
DEFAULT_STYLE = "zip"
LIMITS = {
    "rate": (0.1, 20),
    "feedback": (0, 0.95),
    "mix": (0, 1),
    "shortest_ms": (1, 1000),
    "longest_ms": (2, 1000),
}


MAX_SWEEP_RATIO = 16
WAVE_POINTS = 32


def settings(config):
    0


    spec = config.get("shake", DEFAULT_STYLE)
    if spec is False or spec == "off":
        return None
    if isinstance(spec, str):
        spec = {"style": spec}
    if not isinstance(spec, dict):
        print('shake must be "zip", "warble", "glitch", "off" or an object; using {}'.format(DEFAULT_STYLE))
        spec = {}
    style = spec.get("style", DEFAULT_STYLE)
    if style == "off":
        return None
    if style not in STYLES:
        print("shake style {!r} unknown; using {}".format(style, DEFAULT_STYLE))
        style = DEFAULT_STYLE
    out = dict(STYLES[style], style=style)
    for key, (low, high) in LIMITS.items():
        if key not in spec:
            continue
        value = spec[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
            out[key] = value
        else:
            print("shake {} must be a number from {} to {}; using {}".format(key, low, high, out[key]))
    if out["shortest_ms"] >= out["longest_ms"]:
        print("shake shortest_ms must be under longest_ms; using the style's own")
        out["shortest_ms"], out["longest_ms"] = STYLES[style]["shortest_ms"], STYLES[style]["longest_ms"]
    if style != "glitch" and out["longest_ms"] > out["shortest_ms"] * MAX_SWEEP_RATIO:
        out["shortest_ms"] = out["longest_ms"] / MAX_SWEEP_RATIO
        print("shake shortest_ms raised to {:.0f} (longest / {})".format(out["shortest_ms"], MAX_SWEEP_RATIO))
    return out


def shape(style, phase):
    0

    if style == "warble":
        return math.sin(2 * math.pi * phase)
    if style == "glitch":

        step = min(5, int(phase * 6))
        return 1 - 2 * (step / 5) ** 0.55
    return 1 - 2 * phase ** 0.6


def _waveform(style):
    points = 6 if style == "glitch" else WAVE_POINTS


    span = points - 1 if style == "zip" else points
    return array.array("h", [int(32767 * shape(style, i / span)) for i in range(points)])


class ShakeFX:
    0


    def __init__(self, shake, fmt):
        self.style = shake["style"]
        self.mix = shake["mix"]
        self.freq_shift = self.style != "glitch"
        low, high = shake["shortest_ms"], shake["longest_ms"]
        self.lfo = synthio.LFO(
            waveform=_waveform(self.style),
            rate=shake["rate"],
            scale=(high - low) / 2,
            offset=(high + low) / 2,
            interpolate=self.style != "glitch",
        )
        self.echo = audiodelays.Echo(
            max_delay_ms=int(math.ceil(high)),
            delay_ms=self.lfo,
            decay=shake["feedback"],
            mix=0.0,
            freq_shift=self.freq_shift,
            **fmt
        )
        self.on = False

    def play(self, source):
        self.echo.play(source)
        return self.echo

    def stop(self):
        self.echo.stop()
        self.set_amount(0.0)

    def set_amount(self, amount):
        0
        if amount > 0 and not self.on:



            self.echo.freq_shift = not self.freq_shift
            self.echo.freq_shift = self.freq_shift
            self.lfo.retrigger()
        self.on = amount > 0
        self.echo.mix = self.mix * amount

    def deinit(self):
        self.echo.deinit()



ROLL_ON = 0.5
ROLL_OFF = 0.2


class Roll:
    0




    def __init__(self, shake, block_seconds):
        self.shake = shake
        self.block_ms = block_seconds * 1000
        self.since = None
        self.sweep = -1

    def reset(self):
        self.since = None

    def update(self, reader, amount, now):
        if self.since is None and amount < ROLL_ON or self.since is not None and amount < ROLL_OFF:
            if self.since is not None:
                reader.roll(0)
                self.since = None
            return
        if self.since is None:
            self.since, self.sweep = now, -1
        at = (now - self.since) * self.shake["rate"]
        sweep = int(at)
        low, high = self.shake["shortest_ms"], self.shake["longest_ms"]
        ms = low + (high - low) * (shape(self.shake["style"], at - sweep) + 1) / 2
        if sweep != self.sweep:
            self.sweep = sweep
            reader.roll(0)
        reader.roll(max(1, int(ms / self.block_ms + 0.5)))


def describe(shake):
    return "{style}, {rate} a second, delay {shortest_ms:.0f} to {longest_ms:.0f} ms".format(**shake)
