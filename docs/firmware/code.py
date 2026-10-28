# SPDX-FileCopyrightText: Copyright (c) 2026 Tim Cocks for Adafruit Industries
# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors

# SPDX-License-Identifier: MIT
0








































































import gc
import json
import os
import time

import analogio
import audiobusio
import audioi2sin
import audiocore
import audiomixer
import board
import digitalio
import keypad

import storage
try:
    import supervisor
except ImportError:
    supervisor = None
import sys
import synthio

import adafruit_nau88l21
import autotune
from autotune import KEYS, Autotune, key_by_name, scale_by_name
from config_loader import (
    create_effects,
    create_preset,
    uses_tune_stage,
    load_samples,
    preset_level,
    sample_routing,
)
from recorder import Recorder, drive_free
import shake







FIRMWARE_VERSION = "1.7"
print("fx-mic-mod firmware", FIRMWARE_VERSION)


CONFIG_PATHS = ("/config.json", "/ep2350_circuitpython_config.json")

def load_config():
    0








    for path in CONFIG_PATHS:
        try:
            with open(path, "r") as file:
                return json.load(file)
        except (OSError, ValueError) as error:
            print("config {}: {}".format(path, error))
    return {}


CONFIG = load_config()






DEFAULT_RATE = 24000
RATE = CONFIG.get("sample_rate", DEFAULT_RATE)
try:
    autotune.set_rate(RATE)
except ValueError as error:
    print("config: {}; using {}".format(error, DEFAULT_RATE))
    RATE = DEFAULT_RATE
    autotune.set_rate(RATE)


_phase = CONFIG.get("feed_phase", autotune.FEED_PHASE)
if _phase is None or (_phase in (0, 1) and not isinstance(_phase, bool)):
    autotune.FEED_PHASE = _phase
else:
    print("feed_phase must be 0, 1 or null; using {}".format(autotune.FEED_PHASE))


RATE_SCALE = RATE / 16000
SAMPLE_COUNT_PARAMS = ("grain_size", "window", "overlap")

MAX_PRESETS = 4
MAX_SAMPLES = 4





MAX_PAGES = 4
EFFECTS_ONLY = CONFIG.get("effects_only", False)
if not isinstance(EFFECTS_ONLY, bool):
    print("effects_only must be true or false; using false")
    EFFECTS_ONLY = False


SHUTDOWN_HOLD_SECONDS = 0.8




MIC_GAIN = 14

ADC_VOLUME = 0

HEADPHONE_VOLUME = 0






VOLUME_MIN_DB = -50
VOLUME_MAX_DB = 6



VOLUME_OFF_FRACTION = 0.02


VOLUME_DEADBAND = 700










HANDLE_OUT_COUNTS = 32370
HANDLE_MIN_SPAN = 2000


HANDLE_SAMPLES = 16
HANDLE_SMOOTHING = 0.25







HANDLE_OPEN_GUESS = 0.15
HANDLE_DEAD_ZONE = 0.4










TAP_HOLD_SECONDS = 0.25
DOUBLE_TAP_SECONDS = 0.3





RECORD_SECONDS = 4

RECORD_BLINK_SECONDS = 0.25



SQUEEZE_KEY_ON = 0.67
SQUEEZE_KEY_OFF = 0.5



PADDLE_SETTLE_SECONDS = 0.1



HPF_HZ = 120



FORMAT = {
    "buffer_size": int(1024 * RATE_SCALE),
    "sample_rate": RATE,
    "bits_per_sample": 16,
    "samples_signed": True,
    "channel_count": 1,
}




AUTOTUNE_FORMAT = dict(FORMAT, buffer_size=2 * autotune.BLOCK_SIZE)





class DataContext:
    0





    def __init__(self):


        self.chain = []

        self.tune = None



        self.head = None


        self.tune_keys = None

        self.tune_squeezed = False

        self.recorder = None


        self.active = 0

        self.page = 0


        self.white_active = 1



        self.current_wave = None


        self.current_effects = []




        self.current_source = None

        self.current_path = None


        self.wave_playing = False




        self.press_at = 0
        self.press_double = False
        self.tap_at = None


        self.reversed_wave = None
        self.reversed_data = None



        self.shake_fx = None
        self.shake_roll = None


        self.top_held = False
        self.middle_held = False

        self.combo_since = None

        self.power_hold = None




        self.knob_applied = -1 << 20


        self.volume = None


        self.paddle_held = False

        self.gate_open = False

        self.level = 1.0


        self.paddle_changed = None

        self.handle_shown = -1.0

        self.note_shown = None

        self.blink_on = True
        self.blink_since = 0.0




PACK_NAME = CONFIG.get("name", "none")




PRESETS = list(CONFIG.get("presets", ())[:MAX_PRESETS * (MAX_PAGES if EFFECTS_ONLY else 1)])

PAGES = max(1, (len(PRESETS) + MAX_PRESETS - 1) // MAX_PRESETS)


def at_rate(effects):
    0
    if RATE == 16000 or not isinstance(effects, list):
        return effects
    out = []
    for spec in effects:
        if isinstance(spec, dict) and any(
                isinstance(spec.get(k), int) and not isinstance(spec.get(k), bool)
                for k in SAMPLE_COUNT_PARAMS):
            spec = dict(spec)
            for k in SAMPLE_COUNT_PARAMS:
                if isinstance(spec.get(k), int) and not isinstance(spec[k], bool):
                    spec[k] = int(spec[k] * RATE_SCALE)
        out.append(spec)
    return out


PRESETS = [dict(p, list=at_rate(p.get("list"))) if isinstance(p, dict) else p for p in PRESETS]


SAMPLES = [] if EFFECTS_ONLY else load_samples(CONFIG, max_samples=MAX_SAMPLES)
SAMPLES = [dict(e, effects=at_rate(e.get("effects"))) for e in SAMPLES]



try:
    THROUGH_PRESET = sample_routing(CONFIG) == "preset" and not EFFECTS_ONLY
except ValueError as error:
    print("config: {}".format(error))
    THROUGH_PRESET = False


_dead_zone = CONFIG.get("handle_dead_zone", HANDLE_DEAD_ZONE)
if isinstance(_dead_zone, (int, float)) and not isinstance(_dead_zone, bool) and 0 <= _dead_zone <= 0.5:
    HANDLE_DEAD_ZONE = _dead_zone
else:
    print("handle_dead_zone must be a number from 0 to 0.5; using {}".format(HANDLE_DEAD_ZONE))



TRIM_TAKES = CONFIG.get("trim_takes", True)
if not isinstance(TRIM_TAKES, bool):
    print("trim_takes must be true or false; using true")
    TRIM_TAKES = True




NORMALIZE_TAKES = CONFIG.get("normalize_takes", False)
if not isinstance(NORMALIZE_TAKES, bool):
    print("normalize_takes must be true or false; using false")
    NORMALIZE_TAKES = False




SAMPLE_LEVEL = 1.0
_sample_level = CONFIG.get("sample_level", SAMPLE_LEVEL)
if isinstance(_sample_level, (int, float)) and not isinstance(_sample_level, bool) and 0 <= _sample_level <= 1:
    SAMPLE_LEVEL = _sample_level
else:
    print("sample_level must be a number from 0 to 1; using {}".format(SAMPLE_LEVEL))



SHAKE = None if EFFECTS_ONLY else shake.settings(CONFIG)



RECORD_SPOT = None if EFFECTS_ONLY else len(PRESETS) + 1


def page_presets(ctx):
    0
    return PRESETS[ctx.page * MAX_PRESETS:(ctx.page + 1) * MAX_PRESETS]


def next_spot(ctx):
    0


    if EFFECTS_ONLY:
        if not ctx.active:
            ctx.page = 0
        return ctx.active % len(page_presets(ctx)) + 1 if PRESETS else 0
    return (ctx.active + 1) % (len(page_presets(ctx)) + 2)


CAN_RECORD = not storage.getmount("/").readonly







i2s = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WS, board.I2S_DOUT)

codec = adafruit_nau88l21.NAU88L21(board.I2C())
codec.configure_clocks()



codec.headphone_output = True
codec.headphone_volume = HEADPHONE_VOLUME

codec.configure_microphone_input(gain_db=MIC_GAIN)
codec.adc_volume = ADC_VOLUME



codec.configure_adc_highpass(frequency=HPF_HZ, sample_rate=RATE)

mic = audioi2sin.I2SIn(
    board.I2S_BIT_CLOCK,
    board.I2S_WS,
    board.I2S_DIN,
    sample_rate=RATE,
    bit_depth=16,
    mono=True,
    external_clock=True,
)




def make_mixer(fmt):
    0
    return audiomixer.Mixer(
        voice_count=2,
        buffer_size=fmt["buffer_size"],
        channel_count=fmt["channel_count"],
        bits_per_sample=fmt["bits_per_sample"],
        samples_signed=fmt["samples_signed"],
        sample_rate=fmt["sample_rate"],
    )


mixer = make_mixer(FORMAT)
i2s.play(mixer, loop=True)



shake_meter = shake.ShakeMeter.find(board.I2C()) if SHAKE else None




def teardown(ctx):
    0
    mixer.voice[0].stop()
    for _effect in ctx.chain:
        _effect.deinit()
    ctx.chain = []
    if ctx.tune is not None:
        ctx.tune.deinit()
        ctx.tune = None
    if ctx.recorder is not None:
        ctx.recorder.deinit()
        ctx.recorder = None
    if ctx.head is not None:
        ctx.head.deinit()
        ctx.head = None
    if THROUGH_PRESET:

        ctx.wave_playing = False
    gc.collect()


def build(ctx, preset):
    0


























    specs = preset.get("list", ()) if preset else ()
    ctx.level = preset_level(preset) if preset else 1.0
    fmt = AUTOTUNE_FORMAT if any(uses_tune_stage(spec) for spec in specs) else FORMAT
    tune_args = None
    ctx.chain = []
    if preset:
        tune_args, ctx.chain = create_preset(
            preset, blocks={"handle": handle_control}, **fmt
        )

    if tune_args is not None:




        for name in ("speed", "humanize", "harmony_level", "vocoder_mix"):
            if tune_args.get(name) is handle_control:
                tune_args[name] = handle_value



        rest = (tune_args.get("key", "C"), tune_args.get("scale", "chromatic"))
        alt = (tune_args.pop("squeeze_key", None), tune_args.pop("squeeze_scale", None))
        ctx.tune_keys = None
        if alt != (None, None):
            alt = (alt[0] or rest[0], alt[1] or rest[1])
            for key, scale in (rest, alt):
                key_by_name(key)
                scale_by_name(scale)
            ctx.tune_keys = (rest, alt)
        ctx.tune_squeezed = False
        ctx.tune = Autotune(mic, fmt, **tune_args)
        ctx.tune.sample_gain = int(SAMPLE_LEVEL * 256)
        source = ctx.tune.output
    else:
        source = mic

    inlet, inlet_loop = source, ctx.tune is not None or not ctx.chain
    if THROUGH_PRESET:
        mixer.voice[1].stop()
        mixer.voice[0].level = ctx.level
        if ctx.tune is not None:


            ctx.tune.gate = ctx.paddle_held
        else:
            ctx.head = make_mixer(fmt)
            ctx.head.voice[0].level = 1.0 if ctx.paddle_held else 0.0
            inlet, inlet_loop = ctx.head, True

    if not ctx.chain:
        mixer.voice[0].play(inlet, loop=True)
    else:
        mixer.voice[0].play(ctx.chain[-1], loop=True)
        for index in range(len(ctx.chain) - 1, 0, -1):
            ctx.chain[index].play(ctx.chain[index - 1])
        ctx.chain[0].play(inlet, loop=inlet_loop)

    if not THROUGH_PRESET and ctx.gate_open:

        mixer.voice[0].level = ctx.level

    if ctx.head is not None:

        ctx.head.voice[0].play(source, loop=True)



    if ctx.tune is not None:
        ctx.tune.start()


def select_preset(ctx, index):
    0









    muted = codec.dac_soft_mute
    codec.dac_soft_mute = True
    teardown(ctx)

    ctx.active = index
    try:
        if index == RECORD_SPOT:
            enter_record_mode(ctx)
        else:
            build(ctx, page_presets(ctx)[index - 1] if index else [])
    except (ValueError, MemoryError) as error:
        print("preset {} failed: {}".format(index, error))
        teardown(ctx)
        ctx.active = 0
        build(ctx, [])

    update_red_leds(ctx)
    update_white_leds(ctx)
    if ctx.active == RECORD_SPOT:
        print("record mode, {} bytes free, {} KB left on the drive".format(
            gc.mem_free(), (drive_free() or 0) // 1024))
    else:
        presets = page_presets(ctx)
        print(
            "{}preset {} of {}{} ({} effects{}), {} bytes free".format(
                "page {} of {}, ".format(ctx.page + 1, PAGES) if EFFECTS_ONLY else "",
                ctx.active,
                len(presets),
                ": " + presets[ctx.active - 1]["name"]
                if ctx.active and isinstance(presets[ctx.active - 1], dict)
                and isinstance(presets[ctx.active - 1].get("name"), str) else "",
                len(ctx.chain),
                ", {} via {}{}".format(
                    "autotune" if ctx.tune.correct else "voice",
                    ctx.tune.shifter_name,
                    ", harmonizer {} voices".format(len(ctx.tune.voices)) if ctx.tune.voices else "",
                ) + (
                    ", vocoder {} bands{}".format(len(ctx.tune.vocoder.levels),
                                                  " (filters)" if ctx.tune.vocoder.filters else "") if ctx.tune.vocoder else ""
                ) if ctx.tune else "",
                gc.mem_free(),
            )
        )
    codec.dac_soft_mute = muted



    mic.overflow





def enter_record_mode(ctx):
    0




    ctx.chain = []
    if CAN_RECORD:
        ctx.recorder = Recorder(mic, seconds=RECORD_SECONDS, sample_rate=RATE,
                                trim=TRIM_TAKES, normalize=NORMALIZE_TAKES)
        if ctx.recorder.seconds < RECORD_SECONDS:
            print("record mode: memory is short, takes stop at {:.1f} s".format(
                ctx.recorder.seconds))
    else:
        print("record mode: drive is read-only (edit mode), recording is off")
    ctx.blink_on = True
    ctx.blink_since = time.monotonic()


def resync_mic():
    0







    voice = mixer.voice[0]
    level = voice.level
    voice.level = 0.0
    voice.play(mic, loop=True)
    voice.stop()
    voice.level = level


def save_take(ctx):
    0
    sample = sample_for_slot(ctx.white_active)
    path = "/{}".format(sample["file"])

    unload_wave_sample(ctx)
    waterfall = SaveWaterfall()
    try:
        frames = ctx.recorder.save(path, tick=waterfall.step)
        print("saved {} ({:.2f} s), {} KB left on the drive".format(
            path, frames / RATE, (drive_free() or 0) // 1024))
    except OSError as error:
        print("{} not saved: {}, {} KB left on the drive".format(
            path, error, (drive_free() or 0) // 1024))
    ctx.recorder.frames = 0
    load_selected_wave_sample(ctx)
    waterfall.finish()
    update_white_leds(ctx)
    update_red_leds(ctx)




WATERFALL_STEP_SECONDS = 0.07


class SaveWaterfall:
    0


    def __init__(self):
        self.leds = red_leds + white_leds
        for _led in self.leds:
            _led.value = False
        self.lit = -1
        self.moved = 0.0
        self.step()

    def step(self):
        now = time.monotonic()
        if self.lit < 0 or now - self.moved >= WATERFALL_STEP_SECONDS:
            self.lit += 1
            self.moved = now
            index = self.lit % len(self.leds)
            self.leds[index - 1].value = False
            self.leds[index].value = True

    def finish(self):
        0

        while self.lit % len(self.leds) != len(self.leds) - 1:
            time.sleep(max(0.0, WATERFALL_STEP_SECONDS - (time.monotonic() - self.moved)))
            self.step()
        time.sleep(WATERFALL_STEP_SECONDS)
        for _led in self.leds:
            _led.value = False


class HandleValue:
    0

    @property
    def value(self):
        return handle_control.a


handle_value = HandleValue()







BUTTON_PINS = (board.BUTTON_TOP, board.BUTTON_MIDDLE, board.BUTTON_BOTTOM)





TOP = 0
MIDDLE = 1
BOTTOM = 2

keys = keypad.Keys(BUTTON_PINS, value_when_pressed=False, pull=True)






paddle = digitalio.DigitalInOut(board.HANDLE_OUT)
paddle.switch_to_input(pull=digitalio.Pull.UP)



RED_PINS = (board.LED_RED1, board.LED_RED2, board.LED_RED3, board.LED_RED4)

red_leds = []
for _pin in RED_PINS:
    _led = digitalio.DigitalInOut(_pin)
    _led.direction = digitalio.Direction.OUTPUT
    red_leds.append(_led)



WHITE_PINS = (board.LED_WHITE1, board.LED_WHITE2, board.LED_WHITE3, board.LED_WHITE4)

white_leds = []
for _pin in WHITE_PINS:
    _led = digitalio.DigitalInOut(_pin)
    _led.direction = digitalio.Direction.OUTPUT
    white_leds.append(_led)


def update_red_leds(ctx):
    0





    if ctx.active == RECORD_SPOT:
        if ctx.recorder is None:
            for index, _led in enumerate(red_leds):
                _led.value = (index % 2 == 0) == ctx.blink_on
            return
        if ctx.recorder.recording:

            lit = -int(-ctx.recorder.remaining * len(red_leds) // 1)
        else:
            lit = len(red_leds) if ctx.blink_on else 0
        for index, _led in enumerate(red_leds):
            _led.value = index < lit
        return
    for index, _led in enumerate(red_leds):
        _led.value = index < ctx.active


def update_white_leds(ctx):
    0

    if EFFECTS_ONLY:
        lit = ctx.page + 1 if ctx.active else 0
    else:
        lit = ctx.white_active
    for index, _led in enumerate(white_leds):
        _led.value = index < lit


def build_sample_chain(wave, sample):
    0












    effects_specs = sample["effects"]
    if not effects_specs:
        return [], wave

    preset = {"list": effects_specs, "blocks": sample["blocks"]}
    chain = create_effects(preset, blocks={"handle": handle_control}, **FORMAT)
    if not chain:

        return [], wave
    for index in range(len(chain) - 1, 0, -1):
        chain[index].play(chain[index - 1])
    chain[0].play(wave)
    return chain, chain[-1]


def load_selected_wave_sample(ctx):
    0












    unload_wave_sample(ctx)

    sample = sample_for_slot(ctx.white_active)
    path = "/{}".format(sample["file"])
    ctx.current_path = path
    try:
        wave = audiocore.WaveFile(path)
    except OSError as error:
        print("{} not loaded: {}".format(path, error))
        return
    shape = (wave.sample_rate, wave.channel_count, wave.bits_per_sample)
    if shape != (RATE, 1, 16):


        print(
            "{} not loaded: it is {} Hz, {} ch, {} bit, and the mic plays "
            "16-bit mono {} Hz WAVs".format(path, shape[0], shape[1], shape[2], RATE)
        )
        wave.deinit()
        return
    ctx.current_wave = wave

    if THROUGH_PRESET:

        sample = dict(sample, effects=[])
    try:
        ctx.current_effects, ctx.current_source = build_sample_chain(
            ctx.current_wave, sample
        )
    except (ValueError, MemoryError) as error:
        print("{} effects failed: {}".format(path, error))
        ctx.current_effects, ctx.current_source = [], ctx.current_wave

    print(
        "loaded {} ({} Hz, {} ch, {} bit, {} effect(s))".format(
            path,
            ctx.current_wave.sample_rate,
            ctx.current_wave.channel_count,
            ctx.current_wave.bits_per_sample,
            len(ctx.current_effects),
        )
    )


def unload_wave_sample(ctx):
    0
    stop_current_wave(ctx)
    for _effect in ctx.current_effects:
        _effect.deinit()
    ctx.current_effects = []
    if ctx.current_wave is not None:
        ctx.current_wave.deinit()
        ctx.current_wave = None
    ctx.current_source = None
    if ctx.reversed_wave is not None:
        ctx.reversed_wave.deinit()
    ctx.reversed_wave = ctx.reversed_data = None
    ctx.tap_at = None
    gc.collect()


def sample_for_slot(slot):
    0
    if slot <= len(SAMPLES):
        return SAMPLES[slot - 1]
    return {
        "file": "{}.wav".format(slot),
        "effects": [],
        "blocks": {},
    }


def sample_voice(ctx):
    0





    return ctx.head.voice[1] if ctx.head is not None else mixer.voice[1]


def set_gate(ctx, held):
    0
    if THROUGH_PRESET and ctx.tune is not None:
        ctx.tune.gate = held
    elif ctx.head is not None:
        ctx.head.voice[0].level = 1.0 if held else 0.0
    else:
        mixer.voice[0].level = ctx.level if held else 0.0
    ctx.gate_open = held


def reversed_wave(ctx):
    0



    if ctx.reversed_wave is None:
        began = time.monotonic()
        try:
            gc.collect()
            ctx.reversed_data = autotune.load_reversed(ctx.current_path)
            ctx.reversed_wave = audiocore.RawSample(
                ctx.reversed_data, channel_count=1, sample_rate=RATE
            )
        except (MemoryError, OSError, ValueError) as error:
            ctx.reversed_data = None
            print("{} can't play backwards: {}".format(ctx.current_path, error))
            gc.collect()
            return None
        print("{} turned round in {:.0f} ms".format(
            ctx.current_path, (time.monotonic() - began) * 1000))
    return ctx.reversed_wave


def start_sample(ctx, reverse=False):
    0

    if THROUGH_PRESET and ctx.tune is not None:
        try:
            ctx.tune.play_sample(ctx.current_path, reverse=reverse)
        except (OSError, ValueError) as error:
            print("{} can't go through autotune: {}".format(ctx.current_path, error))
        return
    wave = reversed_wave(ctx) if reverse else None
    if wave is None:
        wave = ctx.current_wave
    source = wave
    if ctx.current_effects:

        ctx.current_effects[0].play(wave)
        source = ctx.current_source
    if ctx.shake_fx is not None:


        source = ctx.shake_fx.play(source)
    try:
        voice = sample_voice(ctx)
        voice.level = SAMPLE_LEVEL
        voice.play(source)
    except (ValueError, RuntimeError) as error:
        print("{} can't play: {}".format(ctx.current_path, error))


def stop_current_wave(ctx):
    0
    if ctx.tune is not None:
        ctx.tune.stop_sample()
    sample_voice(ctx).stop()
    if ctx.shake_fx is not None:
        ctx.shake_fx.stop()
    ctx.wave_playing = False



TICKS_MASK = (1 << 29) - 1


def event_ms(event):
    0



    stamp = getattr(event, "timestamp", None)
    if stamp is None:
        stamp = int(time.monotonic() * 1000)
    return stamp & TICKS_MASK


def ms_between(earlier, later):
    return (later - earlier) & TICKS_MASK


def next_page(ctx):
    0


    if not ctx.active:
        select_preset(ctx, next_spot(ctx))
        return
    if PAGES == 1:
        print("one page of presets: add a fifth to get a second page")
        return
    ctx.page = (ctx.page + 1) % PAGES
    select_preset(ctx, min(ctx.active, len(page_presets(ctx))))


def play_current_wave_sample(ctx, at=0):
    0







    if ctx.current_source is None:
        print("no wave loaded for LED {}".format(ctx.white_active))
        return

    double = ctx.tap_at is not None and ms_between(ctx.tap_at, at) <= DOUBLE_TAP_SECONDS * 1000
    ctx.press_at, ctx.press_double, ctx.tap_at = at, double, None
    start_sample(ctx, reverse=double)
    ctx.wave_playing = True
    print("playing {}{}".format(ctx.white_active, " (double tap: backwards)" if double else ""))


def release_current_wave(ctx, at=0):
    0





    if not ctx.wave_playing:
        return
    held = ms_between(ctx.press_at, at)
    if held >= TAP_HOLD_SECONDS * 1000:
        stop_current_wave(ctx)
        print("stopped {} (held {} ms)".format(ctx.white_active, held))
    elif not ctx.press_double:

        ctx.tap_at = ctx.press_at




handle_pot = analogio.AnalogIn(board.HANDLE_POSITION)
handle_control = synthio.Math(synthio.MathOperation.SUM, 0.0, 0.0, 0.0)



knob = analogio.AnalogIn(board.VOLUME)


def apply_volume(ctx, force=False):
    0













    raw = knob.value
    if not force and abs(raw - ctx.knob_applied) < VOLUME_DEADBAND:
        return
    ctx.knob_applied = raw

    fraction = raw / 65535
    if fraction <= VOLUME_OFF_FRACTION:



        ctx.volume = None
        codec.dac_volume = -66
        print("volume off")
        return


    fraction = (fraction - VOLUME_OFF_FRACTION) / (1 - VOLUME_OFF_FRACTION)
    ctx.volume = VOLUME_MIN_DB + fraction * (VOLUME_MAX_DB - VOLUME_MIN_DB)
    codec.dac_volume = ctx.volume
    print("volume {:+.1f} dB".format(codec.dac_volume))




handle_travel = 0.0
handle_open = HANDLE_OPEN_GUESS


handle_out = HANDLE_OUT_COUNTS
handle_low = HANDLE_OUT_COUNTS
handle_in = HANDLE_OUT_COUNTS - HANDLE_MIN_SPAN


def read_handle():
    0

    global handle_travel, handle_out, handle_low, handle_in
    total = 0
    for _ in range(HANDLE_SAMPLES):
        total += handle_pot.value
    raw = total / HANDLE_SAMPLES

    handle_out = max(handle_out, raw)
    handle_low = min(handle_low, raw)
    handle_in = min(handle_low, handle_out - HANDLE_MIN_SPAN)
    fraction = (handle_out - raw) / (handle_out - handle_in)
    handle_travel = min(1.0, max(0.0, fraction))
    start = handle_open + HANDLE_DEAD_ZONE
    fraction = (handle_travel - start) / (1.0 - start)
    fraction = min(1.0, max(0.0, fraction))
    handle_control.a += (fraction - handle_control.a) * HANDLE_SMOOTHING


def note_name(midi):
    return "{}{}".format(KEYS[int(midi) % 12], int(midi) // 12 - 1)


def show_note(ctx):
    0


    tune = ctx.tune
    note = tune.snapper.target if ctx.paddle_held else None
    if note == ctx.note_shown:
        return
    ctx.note_shown = note
    if note is None:
        return
    line = "note " + note_name(note)
    if tune.cents is not None:
        line += " (sung {:+.0f} cents)".format(tune.cents)
    if tune.voices:
        line += ", harmony " + " ".join(note_name(note + i) for i in tune._intervals)
    print(line)


def learn_handle_open():
    0


    global handle_open
    if handle_travel < handle_open:
        handle_open = handle_travel
    else:
        handle_open += (min(handle_travel, 0.6) - handle_open) * 0.25
    print("handle opens the mic at {:.2f} of its travel (raw {:.0f} out, {:.0f} in)".format(
        handle_open, handle_out, handle_in))




data_context = DataContext()
data_context.paddle_held = paddle.value
if shake_meter is not None:
    try:
        data_context.shake_fx = shake.ShakeFX(SHAKE, FORMAT)
        data_context.shake_roll = shake.Roll(SHAKE, autotune.BLOCK_SIZE / RATE)
        print("shake: {} found; shaking a sample plays it {}".format(
            shake_meter.name, shake.describe(SHAKE)))
    except (ValueError, MemoryError) as error:
        print("shake: off ({})".format(error))
        shake_meter = None


update_white_leds(data_context)
if not EFFECTS_ONLY:
    load_selected_wave_sample(data_context)

print(
    "demo v2: pack {!r}, {} preset(s), {} Hz, mic gain {:.0f} dB, codec ADC HPF {:d} Hz".format(
        PACK_NAME, len(PRESETS), RATE, codec.mic_gain, HPF_HZ
    )
)
if EFFECTS_ONLY:
    print("effects only: {} page(s) of presets, no samples or record mode".format(PAGES))
    print("TOP button = next preset on the page, MIDDLE = next page, BOTTOM = clean")
else:
    print("TOP button = next preset ({} spots, 0 = clean)".format(len(page_presets(data_context)) + 1))
print(
    "volume knob = DAC volume, {:.0f} to {:+.0f} dB".format(
        VOLUME_MIN_DB, VOLUME_MAX_DB
    )
)
print('handle travel = "$handle" block, 0.0 (out) to 1.0 (in)')
if not EFFECTS_ONLY:
    print("BOTTOM: held {:.0f} ms+ = hold, two presses within {:.0f} ms = backwards".format(
        TAP_HOLD_SECONDS * 1000, DOUBLE_TAP_SECONDS * 1000))




codec.dac_soft_mute = False
apply_volume(data_context, force=True)
mixer.voice[0].level = 0.0


for _ in range(int(1 / HANDLE_SMOOTHING) + 1):
    read_handle()
select_preset(data_context, 0)

try:
    while True:

        event = keys.events.get()
        while event is not None:
            if event.key_number == TOP:
                data_context.top_held = event.pressed
            if event.key_number == MIDDLE:
                data_context.middle_held = event.pressed
            if event.pressed and event.key_number == TOP:




                select_preset(data_context, next_spot(data_context))
            if event.pressed and event.key_number == MIDDLE and EFFECTS_ONLY:
                next_page(data_context)
            elif event.pressed and event.key_number == MIDDLE:


                data_context.white_active = (
                    data_context.white_active % len(white_leds) + 1
                )
                update_white_leds(data_context)
                load_selected_wave_sample(data_context)
                print("white LED {}".format(data_context.white_active))
            if event.pressed and event.key_number == BOTTOM and EFFECTS_ONLY:

                if data_context.active:
                    select_preset(data_context, 0)
            elif event.pressed and event.key_number == BOTTOM:
                play_current_wave_sample(data_context, event_ms(event))
            if event.released and event.key_number == BOTTOM and not EFFECTS_ONLY:
                release_current_wave(data_context, event_ms(event))
            event = keys.events.get()


        if data_context.top_held and data_context.middle_held:
            if data_context.combo_since is None:
                data_context.combo_since = time.monotonic()
            elif time.monotonic() - data_context.combo_since >= SHUTDOWN_HOLD_SECONDS:
                print(
                    "TOP+MIDDLE held {}s: shutting down".format(SHUTDOWN_HOLD_SECONDS)
                )




                if data_context.power_hold is None:
                    data_context.power_hold = digitalio.DigitalInOut(board.POWER_HOLD)
                    data_context.power_hold.direction = digitalio.Direction.OUTPUT
                data_context.power_hold.value = False
                print("still on: USB is powering the mic. Unplug USB and it turns off.")
                data_context.combo_since = float("inf")
        else:
            data_context.combo_since = None




        now = time.monotonic()
        held = paddle.value
        if held != data_context.paddle_held:
            data_context.paddle_held = held
            data_context.paddle_changed = now
            print("paddle {}".format("pressed" if held else "released"))
            if held:
                learn_handle_open()
            recorder = data_context.recorder
            if not held and recorder is not None and recorder.recording:
                recorder.stop()
        if (
            data_context.paddle_changed is not None
            and now - data_context.paddle_changed >= PADDLE_SETTLE_SECONDS
        ):
            data_context.paddle_changed = None
            if data_context.active == RECORD_SPOT:
                if held and data_context.recorder is not None:
                    resync_mic()
                    data_context.recorder.start()
                    print("recording into slot {}".format(data_context.white_active))
            else:
                set_gate(data_context, held)

        if data_context.active == RECORD_SPOT:
            recorder = data_context.recorder

            if recorder is not None and recorder.frames and not recorder.recording:
                save_take(data_context)
            if now - data_context.blink_since >= RECORD_BLINK_SECONDS:
                data_context.blink_on = not data_context.blink_on
                data_context.blink_since = now
                update_white_leds(data_context)
            update_red_leds(data_context)



        apply_volume(data_context)


        read_handle()


        if data_context.paddle_held and abs(handle_travel - data_context.handle_shown) >= 0.05:
            data_context.handle_shown = handle_travel
            print("squeeze {:.2f} of travel, $handle {:.2f}".format(handle_travel, handle_control.a))




        if data_context.tune is not None and data_context.tune_keys is not None:
            position = handle_control.a
            squeezed = data_context.tune_squeezed
            if (not squeezed and position >= SQUEEZE_KEY_ON) or (
                squeezed and position <= SQUEEZE_KEY_OFF
            ):
                data_context.tune_squeezed = not squeezed
                key, scale = data_context.tune_keys[1 if not squeezed else 0]
                data_context.tune.set_key_scale(key, scale)
                print("autotune now {} {}".format(key, scale))

        vocoder = data_context.tune.vocoder if data_context.tune is not None else None
        if vocoder is not None and vocoder.squeezable:
            position = handle_control.a
            if (not vocoder.squeezed and position >= SQUEEZE_KEY_ON) or (
                vocoder.squeezed and position <= SQUEEZE_KEY_OFF
            ):
                vocoder.squeeze(not vocoder.squeezed)
                print("vocoder chord now {}".format(vocoder.chord or "one note"))






        if shake_meter is not None:
            tune = data_context.tune
            shake_fx = data_context.shake_fx
            if THROUGH_PRESET and tune is not None:
                if tune.sample is not None:
                    data_context.shake_roll.update(tune.sample, shake_meter.update(now), now)
                elif shake_meter.amount:
                    shake_meter.reset()
                    data_context.shake_roll.reset()
            elif data_context.wave_playing and (shake_fx.echo.playing or shake_fx.on):
                shake_fx.set_amount(shake_meter.update(now))
            elif shake_meter.amount or shake_fx.on:
                shake_meter.reset()
                shake_fx.set_amount(0.0)






        recorder = data_context.recorder
        idle = data_context.active == RECORD_SPOT and not (
            recorder is not None and recorder.recording
        )
        if not idle and mic.overflow:
            if data_context.tune is not None:
                tune = data_context.tune



                print("overflow (autotune took {:.1f} ms of 16, plus {:.1f} waiting for the chain; "
                      "{:.1f} spare, {:.1f} outside)".format(
                          tune.busy_ms, tune.wait_ms, tune.idle_ms, tune.outside_ms))
            else:
                print("overflow")



        if supervisor is not None and supervisor.runtime.serial_bytes_available:
            typed = sys.stdin.read(supervisor.runtime.serial_bytes_available)
            if "f" in typed.lower() and autotune.FEED_PHASE is not None:
                autotune.FEED_PHASE ^= 1
                print("feed_phase now {}".format(autotune.FEED_PHASE))



        if data_context.tune is not None:
            data_context.tune.step()
            show_note(data_context)
        elif data_context.recorder is not None and data_context.recorder.recording:
            data_context.recorder.step()
        else:
            time.sleep(0.01)
except KeyboardInterrupt:
    teardown(data_context)
    stop_current_wave(data_context)
    for effect in data_context.current_effects:
        effect.deinit()
    if data_context.shake_fx is not None:
        data_context.shake_fx.deinit()
    if data_context.current_wave is not None:
        data_context.current_wave.deinit()
    mixer.deinit()
    i2s.deinit()
    mic.deinit()
    keys.deinit()
    paddle.deinit()
    knob.deinit()
    handle_pot.deinit()
    for led in red_leds:
        led.value = False
        led.deinit()
    for led in white_leds:
        led.value = False
        led.deinit()
