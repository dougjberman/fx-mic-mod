"""Stand-ins for the CircuitPython hardware modules, for desktop tests.

Install with ``fakes.install(script)``; ``script`` drives buttons, the paddle
and a fake clock. Only what code.py, config_loader.py, autotune.py and
recorder.py touch is modelled.
"""

import os
import math
import sys
import types


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Script:
    """What the 'user' does, as a function of fake time."""

    def __init__(self, presses=(), paddle=(), end=10.0, readonly=False, tone_hz=207.0):
        self.clock = Clock()
        self.presses = sorted(presses)  # (time, key_number, pressed)
        self.paddle = paddle            # [(start, end)] squeezed intervals
        self.end = end
        self.readonly = readonly
        self.tone_hz = tone_hz
        self.phase = 0.0
        self.leds = {}
        self.led_log = []
        self.shifters = []
        self.mixers = []
        self.synths = []
        self.plays = []
        self.mic_reads = 0

    def paddle_held(self):
        t = self.clock.t
        return any(a <= t < b for a, b in self.paddle)


SCRIPT = None


class _Obj:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.playing = None

    def play(self, source, loop=False):
        self.playing = source
        SCRIPT.plays.append((type(self).__name__, type(source).__name__))

    def stop(self):
        self.playing = None

    def deinit(self):
        self.deinited = True


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# --- board / digitalio / analogio / keypad ---

class _Pin:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


class DigitalInOut:
    def __init__(self, pin):
        # Like CircuitPython, a pin can only be claimed once (POWER_HOLD is
        # claimed by the shutdown combo, which runs again over USB power).
        if pin.name == "POWER_HOLD":
            if getattr(pin, "claimed", False):
                raise ValueError("POWER_HOLD in use")
            pin.claimed = True
        self.pin = pin
        self._value = False
        self.direction = None

    def switch_to_input(self, pull=None):
        pass

    @property
    def value(self):
        if self.pin.name == "HANDLE_OUT":
            return SCRIPT.paddle_held()
        if self.pin.name == "BUTTON_TOP":
            return True  # not held at boot
        return self._value

    @value.setter
    def value(self, v):
        self._value = bool(v)
        if self.pin.name.startswith("LED_"):
            SCRIPT.leds[self.pin.name] = self._value
            SCRIPT.led_log.append((SCRIPT.clock.t, dict(SCRIPT.leds)))

    def deinit(self):
        pass


class AnalogIn:
    def __init__(self, pin):
        self.pin = pin

    @property
    def value(self):
        if self.pin.name == "VOLUME":
            return 40000
        # handle pot: squeezed fully while the paddle is held. FAKE_HANDLE="out,in"
        # sets the two ends, since each mic sits differently.
        out, full = (int(v) for v in os.environ.get("FAKE_HANDLE", "32370,29850").split(","))
        return full if SCRIPT.paddle_held() else out

    def deinit(self):
        pass


class _Event:
    def __init__(self, key, pressed):
        self.key_number = key
        self.pressed = pressed
        self.released = not pressed


class _Events:
    def get(self):
        if SCRIPT.presses and SCRIPT.presses[0][0] <= SCRIPT.clock.t:
            _, key, pressed = SCRIPT.presses.pop(0)
            return _Event(key, pressed)
        return None


class Keys:
    def __init__(self, *args, **kwargs):
        self.events = _Events()

    def deinit(self):
        pass


# --- audio ---

class I2SIn(_Obj):
    overflow_reads = 0

    @property
    def overflow(self):
        return False

    def record(self, buf, count):
        s = SCRIPT
        s.mic_reads += 1
        rate = self.kwargs.get("sample_rate", 16000)
        step = 2 * math.pi * s.tone_hz / rate
        for i in range(count):
            buf[i] = int(8000 * math.sin(s.phase))
            s.phase += step
        s.clock.t += count / rate
        if s.clock.t >= s.end:
            raise KeyboardInterrupt
        return count


def _sleep(seconds):
    SCRIPT.clock.sleep(seconds)
    if SCRIPT.clock.t >= SCRIPT.end:
        raise KeyboardInterrupt


class _Voice:
    def __init__(self):
        self.level = 1.0
        self.playing = None

    def play(self, source, loop=False):
        self.playing = source

    def stop(self):
        self.playing = None


class Mixer(_Obj):
    def __init__(self, voice_count=2, **kwargs):
        super().__init__(**kwargs)
        self.voice = [_Voice() for _ in range(voice_count)]
        SCRIPT.mixers.append(self)


class GranularPitchShift(_Obj):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.semitone_log = []
        SCRIPT.shifters.append(self)

    @property
    def semitones(self):
        return self.__dict__.get("_semitones", 0.0)

    @semitones.setter
    def semitones(self, value):
        self._semitones = value
        if hasattr(self, "semitone_log"):
            self.semitone_log.append(value)


class WaveFile(_Obj):
    def __init__(self, path):
        import os
        if not os.path.exists(_map(path)):
            raise OSError(2, "No such file")
        super().__init__()
        self.path = path
        import wave
        with wave.open(_map(path)) as info:
            self.sample_rate = info.getframerate()
            self.channel_count = info.getnchannels()
            self.bits_per_sample = 8 * info.getsampwidth()


class Codec:
    def __init__(self, i2c):
        self.dac_soft_mute = False
        self.mic_gain = 14
        self.dac_volume = 0
        self.headphone_output = False
        self.headphone_volume = 0
        self.adc_volume = 0

    def configure_clocks(self):
        pass

    def configure_microphone_input(self, gain_db):
        self.mic_gain = gain_db

    def configure_adc_highpass(self, frequency, sample_rate):
        pass


class _Enum:
    def __getattr__(self, name):
        return name


class Math(_Obj):
    def __init__(self, operation, a=0.0, b=0.0, c=0.0):
        super().__init__()
        self.operation, self.a, self.b, self.c = operation, a, b, c


class Note(_Obj):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.amplitude_log = []

    def __setattr__(self, name, value):
        if name == "amplitude" and "amplitude_log" in self.__dict__:
            self.amplitude_log.append(value)
        object.__setattr__(self, name, value)


class Synthesizer(_Obj):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pressed = []
        SCRIPT.synths.append(self)

    def press(self, notes):
        self.pressed.extend(notes)

    def release(self, notes):
        self.pressed = [n for n in self.pressed if n not in notes]

    def release_all(self):
        self.released = True


class _Mount:
    def __init__(self, readonly):
        self.readonly = readonly


# --- filesystem: "/name" maps into a temp dir ---

ROOT = None


def _map(path):
    if isinstance(path, str) and path.startswith("/") and (
            "/" not in path[1:] or path.startswith("/selftest/")):
        return ROOT + path
    return path


def install(script, root):
    import gc
    gc.mem_free = lambda: 123456
    global SCRIPT, ROOT
    import builtins
    import os
    import time as real_time

    SCRIPT = script
    ROOT = root

    pins = {}

    class _Board(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return pins.setdefault(name, _Pin(name))

    board = _Board("board")
    board.I2C = lambda: None
    sys.modules["board"] = board

    _module("digitalio", DigitalInOut=DigitalInOut,
            Direction=types.SimpleNamespace(OUTPUT="out", INPUT="in"),
            Pull=types.SimpleNamespace(UP="up", DOWN="down"))
    _module("analogio", AnalogIn=AnalogIn)
    _module("keypad", Keys=Keys)
    _module("audiobusio", I2SOut=_Obj)
    _module("audioi2sin", I2SIn=I2SIn)
    _module("audiocore", RawSample=_Obj, WaveFile=WaveFile)
    _module("audiomixer", Mixer=Mixer)
    _module("audiodelays", GranularPitchShift=GranularPitchShift, PitchShift=_Obj,
            Chorus=_Obj, Echo=_Obj, MultiTapDelay=_Obj)
    _module("audiofilters", Distortion=_Obj, Filter=_Obj, Phaser=_Obj,
            DistortionMode=_Enum())
    _module("audiofreeverb", Freeverb=_Obj)
    _module("synthio", Math=Math, LFO=_Obj, Biquad=_Obj, Note=Note, Synthesizer=Synthesizer,
            MathOperation=_Enum(), FilterMode=_Enum())
    _module("storage", getmount=lambda path: _Mount(script.readonly),
            remount=lambda *a, **k: None)
    _module("adafruit_nau88l21", NAU88L21=Codec)

    real_open = builtins.open
    builtins.open = lambda path, *a, **k: real_open(_map(path), *a, **k)
    for name in ("stat", "remove", "rename"):
        real = getattr(os, name)
        setattr(os, name, (lambda real: lambda *paths: real(*[_map(p) for p in paths]))(real))

    real_time.monotonic = script.clock.monotonic
    real_time.sleep = _sleep
