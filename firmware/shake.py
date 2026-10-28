# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors
#
# SPDX-License-Identifier: MIT
"""Shake the mic while a sample plays, and the sample zips, warbles or glitches.

The stock firmware did something when you shook the mic during playback.
This brings a shake effect back, for samples only.

* `ShakeMeter` reads the accelerometer on the codec's I2C bus (0x18) and
  turns it into a shake amount, 0 (still) to 1 (shaken hard).
* `ShakeFX` is one `audiodelays.Echo` on the sample voice, its delay swept by
  a `synthio.LFO`. The shake amount sets its mix; at 0 the Echo hands the
  sample straight through and leaves its buffer alone, so it costs next to
  nothing until the mic is shaken. It is never in the mic's chain.
* `Roll` is for autotune presets, which stream the sample in Python
  (autotune.WaveReader): shaking repeats the last stretch of the sample,
  shorter and shorter, a stutter roll. It only moves the file position, so it
  adds no audio work to the tightest pass there is.

The styles were tried in the browser (the Shake FX Lab), which runs the same
Echo step for step at 24 kHz:

* ``zip``: a freq_shift Echo whose delay keeps falling from the longest to the
  shortest. Each repeat plays faster than it was written, so the echoes chirp
  up and the sound zips.
* ``warble``: the same Echo, echo only and no feedback, its delay wobbling on
  a sine: a worn-tape pitch wobble.
* ``glitch``: a plain Echo with lots of feedback, its delay stepping down. Each
  step cuts the loop shorter, so it stutters and buzzes up into a tone.

config.json: ``"shake": "zip"`` (or ``"warble"``, ``"glitch"``, ``"off"``), or
an object with ``"style"`` and any of ``"rate"``, ``"feedback"``, ``"mix"``,
``"shortest_ms"``, ``"longest_ms"``. Without the key the mic zips.
"""

import array
import math

import audiodelays
import synthio

# --- The accelerometer ---

ADDRESS = 0x18
WHO_AM_I = 0x0F
# ST's LIS3DH family (LIS3DH, LIS2DH12) and Silan's SC7A20, which copies its
# registers. Which one the EP-2350 has isn't written down anywhere yet; the
# console prints the id it finds.
KNOWN_IDS = {0x33: "LIS3DH", 0x11: "SC7A20"}
CTRL_REG1 = 0x20
CTRL_REG4 = 0x23
# 100 Hz, x, y and z on; and asleep (no data rate), which it stays in
# until a sample plays, so at rest the shake costs nothing at all.
REG1_100HZ = 0x57
REG1_ASLEEP = 0x07
# Block data update (a read never mixes two samples' bytes), +-4 g.
REG4_4G = 0x90
# The six output bytes, x y z low then high; the top bit asks for the
# address to step on through them.
OUT_X_L = 0x28 | 0x80
# +-4 g, left-justified in 16 bits.
COUNTS_PER_G = 8192

# How often the meter reads, at most. 50 a second is plenty for a hand, and
# each read is about 0.3 ms of I2C.
READ_SECONDS = 0.02
# Gravity is followed slowly (about 0.2 s), and how far a reading is from it
# is the shake. Tipping the mic over moves gravity, not the shake.
GRAVITY_SMOOTHING = 0.1
# Below SHAKE_FROM_G it reads still (handling, pressing a button), and from
# SHAKE_FULL_G it reads a full shake. First guesses; see NOTES.md.
SHAKE_FROM_G = 0.5
SHAKE_FULL_G = 1.6
# The amount rises fast and falls slowly, so the effect holds through each
# swing of a shake rather than pumping with it.
ATTACK_SECONDS = 0.03
RELEASE_SECONDS = 0.25


class Accelerometer:
    """The accelerometer at 0x18, read by register. Raises OSError if nothing
    answers there, ValueError if its id isn't one this knows."""

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
        # The codec shares the bus; its driver locks it too.
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
        """(x, y, z) in g."""
        b = self._read(OUT_X_L, self._six)
        out = []
        for i in (0, 2, 4):
            value = b[i] | (b[i + 1] << 8)
            if value & 0x8000:
                value -= 0x10000
            out.append(value / COUNTS_PER_G)
        return out


class ShakeMeter:
    """How hard the mic is being shaken, 0 to 1, as ``amount``."""

    def __init__(self, accelerometer):
        self.accelerometer = accelerometer
        self.name = accelerometer.name
        self.amount = 0.0
        self.gravity = None
        self.read_at = None

    @classmethod
    def find(cls, i2c):
        """A meter on the accelerometer on ``i2c``, or None (printing why)."""
        try:
            return cls(Accelerometer(i2c))
        except (OSError, ValueError, RuntimeError, AttributeError) as error:
            print("shake: no accelerometer ({})".format(error))
            return None

    @property
    def awake(self):
        return self.accelerometer.awake

    def reset(self):
        """Still again, and the accelerometer back to sleep."""
        self.amount = 0.0
        self.read_at = None
        self.gravity = None
        try:
            self.accelerometer.sleep()
        except OSError:
            pass

    def update(self, now):
        """Read the accelerometer if it's due, and return ``amount``. The
        first call wakes it, and reads from its first sample, 10 ms on."""
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


# --- The effect ---

# The numbers the Shake FX Lab settled on. "rate" is sweeps (or wobbles) a
# second, "feedback" the Echo's decay, "mix" its mix at a full shake, and the
# delay runs between "shortest_ms" and "longest_ms" (also the buffer: 24 kHz
# is 48 bytes a ms).
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
# A freq_shift Echo writes longest / shortest buffer cells per sample, so the
# sweep is kept within this.
MAX_SWEEP_RATIO = 16
WAVE_POINTS = 32


def settings(config):
    """The "shake" settings from ``config``: a dict with "style" and the
    style's numbers, or None for off. A bad value prints why and keeps the
    style's own number."""
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
    """The delay at ``phase`` (0 to 1) of one sweep, as -1 (shortest) to 1
    (longest)."""
    if style == "warble":
        return math.sin(2 * math.pi * phase)
    if style == "glitch":
        # six steps, falling fast and then slower
        step = min(5, int(phase * 6))
        return 1 - 2 * (step / 5) ** 0.55
    return 1 - 2 * phase ** 0.6


def _waveform(style):
    points = 6 if style == "glitch" else WAVE_POINTS
    # The LFO runs on from its last point back to its first: the sine wraps
    # round, and the zip's last point is the shortest delay.
    span = points - 1 if style == "zip" else points
    return array.array("h", [int(32767 * shape(style, i / span)) for i in range(points)])


class ShakeFX:
    """The shake Echo for samples on the native mixer. ``play(source)``
    returns what to play on the sample voice instead of ``source``."""

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
        """Mix in the effect for a shake ``amount`` (0 to 1)."""
        if amount > 0 and not self.on:
            # Each shake starts a fresh sweep on an empty buffer: at mix 0 the
            # Echo stops writing it, so it still holds the last shake. Changing
            # freq_shift is the one thing that clears it.
            self.echo.freq_shift = not self.freq_shift
            self.echo.freq_shift = self.freq_shift
            self.lfo.retrigger()
        self.on = amount > 0
        self.echo.mix = self.mix * amount

    def deinit(self):
        self.echo.deinit()


# A roll starts at this shake amount and ends below ROLL_OFF.
ROLL_ON = 0.5
ROLL_OFF = 0.2


class Roll:
    """The shake on autotune presets: a stutter roll in the streamed sample.
    Each sweep holds one stretch of the sample and repeats it, from the
    longest delay down to the shortest; the next sweep starts where the
    sample has got to."""

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
