# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors
#
# SPDX-License-Identifier: MIT
"""
On-device checks for fx-mic-mod, driven over the serial REPL by
tools/ting_test.py on the computer. Nothing here runs at power-on.

Stop code.py with Ctrl-C, then for example:

    >>> import selftest
    >>> selftest.info()
    >>> selftest.build_all()
    >>> selftest.run({"list": [{"effect": "autotune"}]}, "/selftest/notes.wav")

Every result is one line, "@@" followed by JSON, so the computer can pick
them out of the other console output. Audio heard by the mic comes back as
base64 lines between "@@" wav_begin and wav_end records.

Audio checks are an acoustic loopback: a test WAV goes in where the mic
normally would, the preset plays it out of the speaker, and the mic records
what the speaker plays. The mic never feeds the output during a check, so
nothing can howl.
"""

import array
import binascii
import gc
import json
import math
import os
import sys
import time

import analogio
import audiobusio
import audiocore
import audioi2sin
import audiomixer
import board
import digitalio
import keypad
import storage
import synthio

import adafruit_nau88l21
import autotune
from autotune import BLOCK_SIZE, Autotune, detect_pitch_autocorrelation
from config_loader import create_preset, preset_level, uses_tune_stage

# Same audio settings as code.py.
RATE = 16000
MIC_GAIN = 14
ADC_VOLUME = 0
HEADPHONE_VOLUME = 0
HPF_HZ = 120
FORMAT = {
    "buffer_size": 1024,
    "sample_rate": RATE,
    "bits_per_sample": 16,
    "samples_signed": True,
    "channel_count": 1,
}
AUTOTUNE_FORMAT = dict(FORMAT, buffer_size=512)

# Output level for loopback checks, in dB on the DAC. Loud enough for the mic
# to hear the speaker clearly, quiet enough not to clip it.
LOOPBACK_DB = -12
# Base64 lines carry this many bytes of audio each.
CHUNK_BYTES = 384


def emit(kind, **data):
    data["t"] = kind
    print("@@" + json.dumps(data))


def now_ms():
    return time.monotonic_ns() // 1000000


def load_config(path="/config.json"):
    with open(path, "r") as file:
        return json.load(file)


class Rig:
    """The codec, mic and output mixer, brought up the way code.py does."""

    def __init__(self):
        self.i2s = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WS, board.I2S_DOUT)
        self.codec = adafruit_nau88l21.NAU88L21(board.I2C())
        self.codec.configure_clocks()
        self.codec.headphone_output = True
        self.codec.headphone_volume = HEADPHONE_VOLUME
        self.codec.configure_microphone_input(gain_db=MIC_GAIN)
        self.codec.adc_volume = ADC_VOLUME
        self.codec.configure_adc_highpass(frequency=HPF_HZ, sample_rate=RATE)
        self.codec.dac_volume = LOOPBACK_DB
        self.mic = audioi2sin.I2SIn(
            board.I2S_BIT_CLOCK, board.I2S_WS, board.I2S_DIN,
            sample_rate=RATE, bit_depth=16, mono=True, external_clock=True)
        self.mixer = audiomixer.Mixer(voice_count=1, **FORMAT)
        self.i2s.play(self.mixer, loop=True)
        self.handle = synthio.Math(synthio.MathOperation.SUM, 0.0, 0.0, 0.0)

    def deinit(self):
        self.mixer.voice[0].stop()
        for part in (self.mixer, self.mic, self.i2s):
            part.deinit()


_rig = None


def rig():
    global _rig
    if _rig is None:
        _rig = Rig()
    return _rig


class TapMic:
    """Hands autotune the real mic's timing, and keeps what the mic heard."""

    def __init__(self, mic, capture):
        self.mic = mic
        self.capture = capture
        self.at = 0
        self.overflow = 0
        self.returned = 0

    def record(self, buf, count):
        got = self.mic.record(buf, count)
        self.returned = time.monotonic_ns()
        cap, at = self.capture, self.at
        if cap is not None and at < len(cap):
            n = min(got, len(cap) - at)
            cap[at:at + n] = buf[0:n]
            self.at = at + n
        if getattr(self.mic, "overflow", False):
            self.overflow += 1
        return got


class HandleValue:
    def __init__(self, math_block):
        self.block = math_block

    @property
    def value(self):
        return self.block.a


class Chain:
    """One preset wired like code.py's build(), with a WAV as its input."""

    def __init__(self, rig_, preset, source, capture=None, handle=1.0):
        self.rig = rig_
        rig_.handle.a = handle
        specs = preset.get("list", ()) if preset else ()
        fmt = AUTOTUNE_FORMAT if any(uses_tune_stage(s) for s in specs) else FORMAT
        self.tune = None
        self.wave = None
        self.chain = []
        self.tap = TapMic(rig_.mic, capture)
        tune_args = None
        if preset:
            tune_args, self.chain = create_preset(preset, blocks={"handle": rig_.handle}, **fmt)
        if tune_args is not None:
            value = HandleValue(rig_.handle)
            for name in ("speed", "humanize", "harmony_level"):
                if tune_args.get(name) is rig_.handle:
                    tune_args[name] = value
            tune_args.pop("squeeze_key", None)
            tune_args.pop("squeeze_scale", None)
            self.tune = Autotune(self.tap, fmt, **tune_args)
            # The mic only paces autotune; the WAV is the voice.
            self.tune.gate = 0
            self.tune.play_sample(source)
            inlet, loop = self.tune.output, True
        else:
            self.wave = audiocore.WaveFile(source)
            inlet, loop = self.wave, False
        voice = rig_.mixer.voice[0]
        voice.level = preset_level(preset) if preset else 1.0
        if not self.chain:
            voice.play(inlet, loop=loop)
        else:
            voice.play(self.chain[-1], loop=True)
            for index in range(len(self.chain) - 1, 0, -1):
                self.chain[index].play(self.chain[index - 1])
            self.chain[0].play(inlet, loop=loop)
        if self.tune is not None:
            self.tune.start()

    def step(self):
        """One 16 ms block of mic capture, for presets without autotune."""
        self.tap.record(self._block(), BLOCK_SIZE)

    def _block(self):
        block = getattr(self, "_buf", None)
        if block is None:
            block = self._buf = array.array("h", [0] * BLOCK_SIZE)
        return block

    def deinit(self):
        self.rig.mixer.voice[0].stop()
        for effect in self.chain:
            effect.deinit()
        if self.tune is not None:
            self.tune.deinit()
        if self.wave is not None:
            self.wave.deinit()
        gc.collect()


def info():
    """Firmware, memory, drive mode, and which audio modules exist."""
    gc.collect()
    modules = {}
    for name in ("audiodelays", "audiofilters", "audiofreeverb", "ulab"):
        try:
            __import__(name)
            modules[name] = True
        except ImportError:
            modules[name] = False
    try:
        import audiodelays
        modules["GranularPitchShift"] = hasattr(audiodelays, "GranularPitchShift")
    except ImportError:
        modules["GranularPitchShift"] = False
    try:
        config = load_config()
        presets = [p.get("name", "") for p in config.get("presets", ())]
        config_error = None
    except (OSError, ValueError) as error:
        presets, config_error = [], str(error)
    stat = os.statvfs("/")
    emit("info",
         version=sys.version, platform=sys.platform,
         uname=list(os.uname()) if hasattr(os, "uname") else None,
         mem_free=gc.mem_free(),
         drive_free=stat[0] * stat[3],
         writable=not storage.getmount("/").readonly,
         modules=modules, presets=presets, config_error=config_error)


def build_all(cycles=3, config_path="/config.json"):
    """Build and free every preset in config.json, ``cycles`` times each.

    Reports memory each preset takes and whether freeing it gives it all
    back (a leak shows as ``leak`` growing with cycles)."""
    config = load_config(config_path)
    for index, preset in enumerate(config.get("presets", ())):
        build(preset, index + 1, cycles)


def build(preset, index=0, cycles=3):
    r = rig()
    gc.collect()
    start = gc.mem_free()
    used = []
    error = None
    for _ in range(cycles):
        gc.collect()
        before = gc.mem_free()
        try:
            chain = Chain(r, preset, "/selftest/silence.wav")
        except (ValueError, MemoryError, OSError, RuntimeError, TypeError) as err:
            error = "{}: {}".format(type(err).__name__, err)
            break
        used.append(before - gc.mem_free())
        chain.deinit()
    gc.collect()
    emit("build", index=index, name=preset.get("name", ""), ok=error is None,
         error=error, used=max(used) if used else None,
         leak=start - gc.mem_free(), cycles=cycles)


def _spin(ms):
    """Count tight-loop passes in ``ms`` milliseconds."""
    end = now_ms() + ms
    count = 0
    while now_ms() < end:
        count += 1
    return count


def run(preset, source, seconds=3.0, handle=1.0, name="", capture=True, stream=True):
    """Play ``source`` through ``preset`` out of the speaker for ``seconds``.

    Measures how much of each 16 ms block the Python side of autotune needs,
    whether it kept up (``blocks`` against ``expected_blocks``), and for
    presets without autotune how much CPU the native effects leave for Python. With ``capture``, the mic records
    the speaker and the take is streamed back."""
    r = rig()
    gc.collect()
    samples = int(seconds * RATE)
    buf = None
    while capture and buf is None and samples >= RATE:
        try:
            buf = array.array("h", bytes(2 * samples))
        except MemoryError:
            emit("warn", message="no room to record {} samples".format(samples))
            samples //= 2
            gc.collect()
    error = None
    try:
        chain = Chain(r, preset, source, capture=buf, handle=handle)
    except (ValueError, MemoryError, OSError, RuntimeError, TypeError) as err:
        emit("run", name=name, ok=False, error="{}: {}".format(type(err).__name__, err))
        return
    busy = []
    blocks = 0
    spare = None
    started = now_ms()
    try:
        if chain.tune is None:
            # Native effects run in the background; what they take shows in
            # how far a tight loop gets, against the same loop at rest. The
            # test WAVs start with a second of silence to cover this.
            idle = _spin_idle()
            spare = _spin(500) / idle if idle else None
        started = now_ms()
        while now_ms() - started < seconds * 1000:
            if chain.tune is not None:
                chain.tune.step()
                # Time from the mic handing over a block to the block being
                # done: the Python share of the 16 ms budget.
                busy.append((time.monotonic_ns() - chain.tap.returned) // 1000)
            else:
                chain.step()
            blocks += 1
    except (MemoryError, RuntimeError, ValueError) as err:
        error = "{}: {}".format(type(err).__name__, err)
    elapsed = now_ms() - started
    overflow = chain.tap.overflow
    captured = chain.tap.at
    chain.deinit()
    emit("run", name=name, ok=error is None, error=error,
         blocks=blocks, expected_blocks=int(elapsed * RATE / 1000 / BLOCK_SIZE),
         overflow=overflow, spare=spare,
         python_us_mean=sum(busy) // len(busy) if busy else None,
         python_us_max=max(busy) if busy else None,
         block_us=1000000 * BLOCK_SIZE // RATE,
         mem_free=gc.mem_free())
    if buf is not None and stream:
        send_wav(buf, captured, name)


_idle_count = None


def _spin_idle():
    """Tight-loop passes in 500 ms with only the bare mixer running."""
    global _idle_count
    if _idle_count is None:
        _idle_count = _spin(500)
    return _idle_count


def send_wav(buf, count, name=""):
    """Stream ``count`` samples of ``buf`` as base64 lines."""
    emit("wav_begin", name=name, rate=RATE, samples=count)
    view = memoryview(buf)
    step = CHUNK_BYTES // 2
    for at in range(0, count, step):
        line = binascii.b2a_base64(bytes(view[at:min(count, at + step)]))
        print("@@B" + line.decode().strip())
    emit("wav_end", name=name)


def detect(source):
    """Run the pitch detector over a WAV, without real time, and report the
    pitch it finds for each 16 ms block and how long detection takes."""
    reader = autotune.WaveReader(source)
    window = array.array("h", [0] * autotune.BUFFER_SIZE)
    found = []
    worst = 0
    try:
        while True:
            n = reader.read()
            if n < BLOCK_SIZE:
                break
            window[0:BLOCK_SIZE] = window[BLOCK_SIZE:]
            window[BLOCK_SIZE:] = reader.buf
            before = now_ms()
            hz = detect_pitch_autocorrelation(window)
            worst = max(worst, now_ms() - before)
            found.append(round(hz, 1))
    finally:
        reader.close()
    emit("detect", hz=found, block_ms=1000 * BLOCK_SIZE // RATE, detect_ms_max=worst)


def leds(seconds_each=0.4):
    """Light each LED in turn: red 1-4, then white 1-4."""
    names = ["LED_RED1", "LED_RED2", "LED_RED3", "LED_RED4",
             "LED_WHITE1", "LED_WHITE2", "LED_WHITE3", "LED_WHITE4"]
    for name in names:
        pin = digitalio.DigitalInOut(getattr(board, name))
        pin.direction = digitalio.Direction.OUTPUT
        pin.value = True
        emit("led", name=name)
        time.sleep(seconds_each)
        pin.value = False
        pin.deinit()
    emit("leds_done")


def controls(seconds=20.0):
    """Report button, paddle, handle and knob changes for ``seconds``."""
    keys = keypad.Keys((board.BUTTON_TOP, board.BUTTON_MIDDLE, board.BUTTON_BOTTOM),
                       value_when_pressed=False, pull=True)
    paddle = digitalio.DigitalInOut(board.HANDLE_OUT)
    paddle.switch_to_input(pull=digitalio.Pull.UP)
    handle = analogio.AnalogIn(board.HANDLE_POSITION)
    knob = analogio.AnalogIn(board.VOLUME)
    names = ("top", "middle", "bottom")
    last = {"paddle": None, "handle": None, "knob": None}
    end = now_ms() + int(seconds * 1000)
    emit("controls_begin", seconds=seconds)
    try:
        while now_ms() < end:
            event = keys.events.get()
            while event:
                emit("button", name=names[event.key_number], pressed=event.pressed)
                event = keys.events.get()
            held = paddle.value
            if held != last["paddle"]:
                last["paddle"] = held
                emit("paddle", held=held, handle=handle.value)
            for key, pot in (("handle", handle), ("knob", knob)):
                value = pot.value
                if last[key] is None or abs(value - last[key]) > 300:
                    last[key] = value
                    emit(key, raw=value)
            time.sleep(0.02)
    finally:
        for part in (keys, paddle, handle, knob):
            part.deinit()
    emit("controls_end")


def mic_level(seconds=1.0):
    """Record the room through the mic with nothing playing."""
    r = rig()
    r.mixer.voice[0].stop()
    block = array.array("h", [0] * BLOCK_SIZE)
    peak = 0
    total = 0
    count = 0
    dc = 0
    for _ in range(int(seconds * RATE / BLOCK_SIZE)):
        got = r.mic.record(block, BLOCK_SIZE)
        for i in range(got):
            v = block[i]
            dc += v
            total += v * v
            peak = max(peak, abs(v))
        count += got
    emit("mic_level", rms=math.sqrt(total / count) if count else 0,
         peak=peak, dc=dc / count if count else 0, samples=count)


def write_file(path, b64_lines):
    """Write a file sent from the computer as base64 lines (record mode only)."""
    folder = path.rsplit("/", 1)[0]
    if folder:
        try:
            os.mkdir(folder)
        except OSError:
            pass
    with open(path, "wb") as file:
        for line in b64_lines:
            file.write(binascii.a2b_base64(line))
    emit("wrote", path=path, size=os.stat(path)[6])


def done():
    """Free the audio hardware."""
    global _rig
    if _rig is not None:
        _rig.deinit()
        _rig = None
    emit("done")
