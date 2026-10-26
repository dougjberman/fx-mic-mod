# SPDX-FileCopyrightText: Copyright (c) 2026 Tim Cocks for Adafruit Industries
# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors
#
# SPDX-License-Identifier: MIT
"""
EP-2350 CircuitPython Demo: mic -> headphone passthrough with JSON-configured effect chains.

fx-mic-mod fork of Adafruit's demo, adding:

* an ``"autotune"`` effect. A preset that has one runs autotune first and its
  other effects after it (see autotune.py and config_loader.create_preset).
* a record mode, one spot past the last preset, for recording samples on the
  device (see recorder.py and boot.py).

Up to four presets are used (one per red LED). Only one chain exists at a time:
switching tears the old chain down and builds the new one.

Controls:

* TOP side button -- steps to the next spot in the preset cycle. The cycle is the
                     configured presets plus a "clean" spot with no effects at
                     all, where the mic feeds the output directly.
* Handle          -- gates the mic output. It starts muted; the DAC un-mutes
                     while the paddle is squeezed and mutes again on release.
* Volume knob     -- sets the DAC digital volume, continuously. Fully anti-
                     clockwise is silence.
* Handle travel   -- offered to the presets, and to a sample's own effects
                     chain, as the block ``"$handle"``, 0.0 with the handle out
                     to 1.0 squeezed in. A preset or sample that does not
                     mention it ignores the handle entirely.

* MIDDLE side button -- steps to the next spot in the list of wave samples. the
                        current position in the list is indicated by the white
                        LEDs.
* BOTTOM side button -- plays the current sample wave file with any effects
                        configured for it.
* TOP + MIDDLE, held together for 800ms -- shuts the unit down by driving
                        POWER_HOLD low.

Record mode (TOP past the last preset):

* All four red LEDs flash together; the white LEDs show the selected slot.
* MIDDLE picks the slot to record into, BOTTOM plays it back as usual.
* Hold the handle to record, release to stop and save over the slot's file.
  The red LEDs stop flashing and count the time left down from 4 to 0; at 0
  the take stops.
* Needs the drive writable by CircuitPython (the default, see boot.py). In
  edit mode (TOP held at power-on) the red LEDs flash in alternating pairs
  and nothing records.

Indication, counting up like the stock firmware:

* Top four red LEDs: preset 1 lights one, preset 2 lights two, and so on to
  all four for preset 4. On the clean spot they are all dark.

* Bottom four white LEDs: sample 1 lights one, sample 2 lights two, and so
  on to all four for sample 4.
"""

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

# Where to look for the preset config
CONFIG_PATHS = ("/config.json", "/ep2350_circuitpython_config.json")

def load_config():
    """Read and parse the JSON config file.

    Tries `CONFIG_PATHS` in order; an unreadable or malformed file at a given
    path is not fatal, it just moves on to the next one.

    :return: The parsed config dict, or ``{}`` if none of the paths worked --
        which leaves the demo with nothing but the clean spot in its preset
        cycle and no samples to play.
    """
    for path in CONFIG_PATHS:
        try:
            with open(path, "r") as file:
                return json.load(file)
        except (OSError, ValueError) as error:
            print("config {}: {}".format(path, error))
    return {}


CONFIG = load_config()

# Frame rate of the whole audio path: the codec, the mic, every effect and
# the samples. config.json's "sample_rate" picks it from autotune.RATES; the
# codec's FLL follows the bit clock, so any of them works there. 24 kHz is the
# most a Ting keeps up with on a full preset: at 32 kHz the built-in effects
# overrun the mic with distortion, echo and reverb all running.
DEFAULT_RATE = 24000
RATE = CONFIG.get("sample_rate", DEFAULT_RATE)
try:
    autotune.set_rate(RATE)
except ValueError as error:
    print("config: {}; using {}".format(error, DEFAULT_RATE))
    RATE = DEFAULT_RATE
    autotune.set_rate(RATE)
# "feed_phase": 0 or 1, which half of the autotune feed buffer is written
# while the other plays. The right one can differ between sample rates; the
# wrong one crackles steadily on autotune presets only.
_phase = CONFIG.get("feed_phase", autotune.FEED_PHASE)
if _phase in (0, 1) and not isinstance(_phase, bool):
    autotune.FEED_PHASE = _phase
else:
    print("feed_phase must be 0 or 1; using {}".format(autotune.FEED_PHASE))
# Sample counts in config.json (grain sizes, pitch_shift windows) are written
# for 16 kHz, so they keep their length in time at other rates.
RATE_SCALE = RATE / 16000
SAMPLE_COUNT_PARAMS = ("grain_size", "window", "overlap")

MAX_PRESETS = 4
MAX_SAMPLES = 4

# How long TOP + MIDDLE must be held together, in seconds, to shut down.
SHUTDOWN_HOLD_SECONDS = 0.8

# --- Hardware effects config ---

# Analog mic gain, dB, -1 .. 36.
MIC_GAIN = 14
# ADC digital gain, dB.
ADC_VOLUME = 0
# Headphone analog volume, dB. Only 0/-3/-6/-9 exist.
HEADPHONE_VOLUME = 0

# --- Volume knob ---

# What the two ends of the knob's travel mean, as a DAC digital volume in dB.
# The codec's own limits are -66 to +24; the top is kept well short of that.
# Travel maps to dB linearly.
VOLUME_MIN_DB = -50
VOLUME_MAX_DB = 6

# Fraction of the travel at the anticlockwise end that means silence, so the
# knob has a definite "off" rather than bottoming out at merely very quiet.
VOLUME_OFF_FRACTION = 0.02

# Volume knob smoothing for noisy signal
VOLUME_DEADBAND = 700

# --- Handle position ---

# The handle travel pot on GP28, in ADC counts. The whole swing is only
# ~2500-2900 counts of the 16-bit scale, and where it sits differs from mic to
# mic (an FX Mic read 32370 out / 29850 in, a Ting 33470 / 30570), so the ends
# are learned as the handle moves: "out" is the highest reading seen, "in" the
# lowest, but never closer to "out" than HANDLE_MIN_SPAN so a light first
# squeeze can't read as fully in. HANDLE_OUT_COUNTS is only the starting guess,
# low enough that any mic's resting reading pushes it up.
HANDLE_OUT_COUNTS = 32370
HANDLE_MIN_SPAN = 2000

# Handle position potentiometer smoothing for noisy signal
HANDLE_SAMPLES = 16
HANDLE_SMOOTHING = 0.25

# "$handle" starts where the mic opens, not at rest. The paddle switch clicks
# partway into the squeeze, so the travel up to that click, plus a dead zone
# for singing with a light squeeze, reads 0, and the rest of the travel sweeps
# 0 to 1. The click point is learned each time the paddle closes, starting
# from HANDLE_OPEN_GUESS; the serial console prints it.
# A config.json can set its own dead zone with "handle_dead_zone".
HANDLE_OPEN_GUESS = 0.15
HANDLE_DEAD_ZONE = 0.25

# --- Record mode ---

# Longest take, in seconds. Held in RAM (48 KB per second at 24 kHz) and
# written to flash after the take, so it is bounded by free memory.
RECORD_SECONDS = 4
# How fast the red LEDs flash in record mode, in seconds per toggle.
RECORD_BLINK_SECONDS = 0.25

# Handle travel (0 out .. 1 in) at which an autotune preset's "squeeze_key"
# takes over, and below which it lets go again.
SQUEEZE_KEY_ON = 0.67
SQUEEZE_KEY_OFF = 0.5

# The paddle switch bounces and its click reaches the mic; the mic gate (and
# the start of a take) waits this long after a paddle change.
PADDLE_SETTLE_SECONDS = 0.1

# DC-blocking high-pass corner, Hz. Applied in the codec's ADC path (see the
# codec.configure_adc_highpass() call below)
HPF_HZ = 120

# Audio format of the chain. Passed to every effect config_loader builds; the
# JSON only describes the sound-affecting parameters, never the format ones.
FORMAT = {
    "buffer_size": int(1024 * RATE_SCALE),
    "sample_rate": RATE,
    "bits_per_sample": 16,
    "samples_signed": True,
    "channel_count": 1,
}

# Autotune hands the chain 256-sample blocks (16 ms), so presets that use it
# build their effects with a matching 512-byte buffer. Untested on the device
# against Adafruit's 1024; see NOTES.md.
AUTOTUNE_FORMAT = dict(FORMAT, buffer_size=2 * autotune.BLOCK_SIZE)


# --- Mutable state container ---


class DataContext:
    """Holds all state that is reassigned inside functions.

    Keeping mutable state in one object removes the need for ``global``
    declarations and makes the data dependencies of each function explicit.
    """

    def __init__(self):
        # The effects making up the currently active preset, in signal-flow
        # order. Empty on the clean spot.
        self.chain = []
        # The preset's autotune stage, heading the chain, or None.
        self.tune = None
        # With samples played through the preset: the two-voice mixer at the
        # chain's input, mic (or autotune) on voice 0, samples on voice 1.
        # None otherwise, and in record mode.
        self.head = None
        # ((key, scale) at rest, (key, scale) squeezed) when the autotune
        # preset sets "squeeze_key" / "squeeze_scale", else None.
        self.tune_keys = None
        # Whether the squeezed key is the one in use.
        self.tune_squeezed = False
        # The sample recorder while in record mode, else None.
        self.recorder = None
        # Index into the cycle: 0 == clean, 1..len(PRESETS) == that preset,
        # RECORD_SPOT == record mode.
        self.active = 0

        # Active white LED, 1..4 (top to bottom).
        self.white_active = 1

        # WaveFile currently loaded,  ``None`` if there is no matching sample
        # configured or its file could not be loaded.
        self.current_wave = None
        # Playmode of the active sample, one of config_loader.PLAYMODES.
        # Stays "oneshot" (a no-op default) when there is no sample.
        self.current_playmode = "oneshot"
        # The sample's effect chain, built from its "effects" config (empty
        # list if it has none), in signal-flow order.
        self.current_effects = []
        # What `play_current_wave_sample()` actually hands to
        # `mixer.voice[1].play()`: `current_wave` itself if the sample has no
        # effects, otherwise the last element of `current_effects`. ``None``
        # when there is no sample loaded.
        self.current_source = None
        # The loaded sample's file path, for autotune to stream it.
        self.current_path = None
        # True while `current_source` is looping on mixer voice 1 because of a
        # "hold" or "startstop" press, tracked so a "startstop" press knows
        # whether to start or stop, and so a LED switch mid-loop can clean up
        # correctly.
        self.wave_playing = False

        # Whether TOP / MIDDLE are currently held down, for the shutdown combo.
        self.top_held = False
        self.middle_held = False
        # the `time.monotonic()` the combo started, or None while it is not both-down.
        self.combo_since = None

        # Wiper position, in ADC counts, that `volume` was last computed from.
        # Starts far enough outside the 16-bit range that the first poll always
        # applies.
        self.knob_applied = -1 << 20
        # The dB the DAC volume was last set to, or None while the knob is at
        # its off end.
        self.volume = None

        # True == paddle squeezed.
        self.paddle_held = False
        # Whether set_gate() last opened the mic.
        self.gate_open = False
        # The active preset's output level, 0.0 to 1.0 (the "level" key).
        self.level = 1.0
        # `time.monotonic()` of the last paddle change still waiting to be
        # acted on after PADDLE_SETTLE_SECONDS, or None.
        self.paddle_changed = None
        # Handle travel last printed while squeezed.
        self.handle_shown = -1.0
        # The autotune note last printed while squeezed, or None.
        self.note_shown = None
        # Flash phase of the red LEDs in record mode.
        self.blink_on = True
        self.blink_since = 0.0


# --- Preset config ---

PACK_NAME = CONFIG.get("name", "none")

# Presets are passed to config_loader whole, since a preset is more than its
# effect list: it may carry a "blocks" mapping of LFOs and Math blocks that
# its effects refer to by name.
PRESETS = list(CONFIG.get("presets", ())[:MAX_PRESETS])


def at_rate(effects):
    """``effects`` with their sample-count parameters scaled to RATE."""
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

# One wave file per white LED slot, each with a playmode telling the BOTTOM
# button how to play it back. See config_loader.load_samples().
SAMPLES = load_samples(CONFIG, max_samples=MAX_SAMPLES)
SAMPLES = [dict(e, effects=at_rate(e.get("effects"))) for e in SAMPLES]

# "preset": samples play dry into the selected preset, like the mic does.
# "own": each sample keeps its own effects (Adafruit's behaviour).
try:
    THROUGH_PRESET = sample_routing(CONFIG) == "preset"
except ValueError as error:
    print("config: {}".format(error))
    THROUGH_PRESET = False

# "handle_dead_zone": how much travel past the mic-open click still reads 0.
_dead_zone = CONFIG.get("handle_dead_zone", HANDLE_DEAD_ZONE)
if isinstance(_dead_zone, (int, float)) and not isinstance(_dead_zone, bool) and 0 <= _dead_zone <= 0.5:
    HANDLE_DEAD_ZONE = _dead_zone
else:
    print("handle_dead_zone must be a number from 0 to 0.5; using {}".format(HANDLE_DEAD_ZONE))

# "sample_level": how loud samples play, 0..1. Full level by default, so a
# take recorded on the mic plays back as loud as it was sung; the install
# guide's converter brings imported files down to about the mic's level.
SAMPLE_LEVEL = 1.0
_sample_level = CONFIG.get("sample_level", SAMPLE_LEVEL)
if isinstance(_sample_level, (int, float)) and not isinstance(_sample_level, bool) and 0 <= _sample_level <= 1:
    SAMPLE_LEVEL = _sample_level
else:
    print("sample_level must be a number from 0 to 1; using {}".format(SAMPLE_LEVEL))

# The cycle position of record mode: one past the last preset.
RECORD_SPOT = len(PRESETS) + 1

# Whether the code may write the drive (boot.py decides at power-on).
CAN_RECORD = not storage.getmount("/").readonly

# --- Codec + audio bring-up ---

# The internal clock mode I2S object has to exist first: it generates BCLK/WS,
# and the external clock mode I2S object syncs to the WS edges it sees.
# Constructing the internal clock one starts BCLK, which the codec's
# FLL then has something to lock to.
i2s = audiobusio.I2SOut(board.I2S_BIT_CLOCK, board.I2S_WS, board.I2S_DOUT)

codec = adafruit_nau88l21.NAU88L21(board.I2C())
codec.configure_clocks()

# enable headphone output and set hardware volume. The DAC digital volume is
# deliberately not set here, the knob owns it.
codec.headphone_output = True
codec.headphone_volume = HEADPHONE_VOLUME

codec.configure_microphone_input(gain_db=MIC_GAIN)
codec.adc_volume = ADC_VOLUME

# Strip the microphone's DC offset and slow subsonic bias drift in the codec's
# own ADC biquad, before it ever reaches the I2S bus.
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

# Two-voice audio mixer sits between the sources and the I2S output.
# Voice 0 carries the current mic/effects chain. Voice 1 carries the
# wave file samples
def make_mixer(fmt):
    """A two-voice mixer in audio format ``fmt``."""
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

# --- Chain switching ---


def teardown(ctx):
    """Stop the current voice, free the current chain's effects"""
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
        # A sample playing into the old preset stops with it.
        ctx.wave_playing = False
    gc.collect()


def build(ctx, preset):
    """Wire up ``preset`` as the live chain on mixer voice 0.

    WIRING ORDER MATTERS. Work from the voice input backwards, so the call
    that hands the mic to something is the LAST one:

        mixer.voice[0].play(head) -> ... -> tail.play(mic)

    The output side is the mixer itself, wired to I2SOut once at startup. That
    means preset switching does not restart the clock source: it only replaces
    the source feeding mixer voice 0. The clock follower I2SIn re-syncs when
    something calls play() *on the mic* (or on an effect that eventually feeds
    the mic); effects do not propagate reset_buffer(), so the mic must be wired
    last.

    :param ctx: The mutable state container.
    :param preset: A preset from the config, or an empty one for the clean
        spot, where the mic is played directly.

    With samples played through the preset (``THROUGH_PRESET``), a mixer
    heads the chain instead: mic on its voice 0, samples on voice 1, and the
    paddle gates voice 0 of that mixer rather than of the output mixer. An
    autotune preset needs no mixer: autotune mixes the sample into the mic
    blocks itself, and the paddle mutes the mic there.
    """
    # Build first: a config error or an out-of-memory here must not leave a
    # half-wired graph running. create_preset() builds without wiring, which
    # is what lets the wiring below run output-first.
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
        # Autotune reads the mic itself, so it, not the mic, heads the chain.
        # Python reads speed / humanize each block; "$handle" is offered to
        # them as the raw handle position, since a synthio Math block only
        # updates while an effect ticks it.
        for name in ("speed", "humanize", "harmony_level"):
            if tune_args.get(name) is handle_control:
                tune_args[name] = handle_value
        # "squeeze_key" / "squeeze_scale": a second key and scale that take
        # over while the handle is squeezed far in. Checked here, so a typo
        # fails the build (and falls back to clean) instead of mid-song.
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
        mixer.voice[1].stop()  # a sample left over from record mode
        mixer.voice[0].level = ctx.level
        if ctx.tune is not None:
            # Autotune mixes the sample in itself, before pitch detection,
            # so the sample is tuned too; the paddle mutes only the mic.
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
        # Squeezed through a preset change: open at the new preset's level.
        mixer.voice[0].level = ctx.level

    if ctx.head is not None:
        # The mic still goes in last.
        ctx.head.voice[0].play(source, loop=True)

    # Same rule as handing the mic over last: the feed into the shifter is
    # wired once everything downstream is.
    if ctx.tune is not None:
        ctx.tune.start()


def select_preset(ctx, index):
    """Make preset cycle position ``index`` the active chain.

    The switch is done with the DAC soft-muted, since tearing the graph down
    and back up puts a step in the output. A preset that fails to build (an
    effect this firmware lacks, or one delay line too many for RAM) falls back
    to the clean spot rather than taking the demo down.

    :param ctx: The mutable state container.
    :param index: The preset position to activate.
    """
    muted = codec.dac_soft_mute
    codec.dac_soft_mute = True
    teardown(ctx)

    ctx.active = index
    try:
        if index == RECORD_SPOT:
            enter_record_mode(ctx)
        else:
            build(ctx, PRESETS[index - 1] if index else [])
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
        print(
            "preset {} of {}{} ({} effects{}), {} bytes free".format(
                ctx.active,
                len(PRESETS),
                ": " + PRESETS[ctx.active - 1]["name"]
                if ctx.active and isinstance(PRESETS[ctx.active - 1], dict)
                and isinstance(PRESETS[ctx.active - 1].get("name"), str) else "",
                len(ctx.chain),
                ", {} via {}{}".format(
                    "autotune" if ctx.tune.correct else "voice",
                    ctx.tune.shifter_name,
                    ", harmonizer {} voices".format(len(ctx.tune.voices)) if ctx.tune.voices else "",
                ) if ctx.tune else "",
                gc.mem_free(),
            )
        )
    codec.dac_soft_mute = muted
    # Building the chain takes longer than one block, so the mic always
    # overflows once during a switch. Reading .overflow clears that, so the
    # console only reports overflows while a preset is actually running.
    mic.overflow


# --- Record mode ---


def enter_record_mode(ctx):
    """Set up record mode: nothing plays on mixer voice 0, the mic is free.

    Only one consumer can read the I2SIn, so the mic is left unwired here and
    the recorder calls ``mic.record()`` itself while the paddle is held.
    """
    ctx.chain = []
    if CAN_RECORD:
        ctx.recorder = Recorder(mic, seconds=RECORD_SECONDS, sample_rate=RATE)
        if ctx.recorder.seconds < RECORD_SECONDS:
            print("record mode: memory is short, takes stop at {:.1f} s".format(
                ctx.recorder.seconds))
    else:
        print("record mode: drive is read-only (edit mode), recording is off")
    ctx.blink_on = True
    ctx.blink_since = time.monotonic()


def resync_mic():
    """Bring the idle mic back into word sync before a take.

    Between takes nothing reads the mic, and saving a take stalls the CPU on
    flash writes, so the clock follower I2SIn can slip a word: the next take
    then reads the empty channel (silence) or straddles two (static). It
    re-syncs when something calls play() on it, so hand it to the idle
    output voice for a moment, muted.
    """
    voice = mixer.voice[0]
    level = voice.level
    voice.level = 0.0
    voice.play(mic, loop=True)
    voice.stop()
    voice.level = level


def save_take(ctx):
    """Write the finished take over the selected slot's file, then reload it."""
    sample = sample_for_slot(ctx.white_active)
    path = "/{}".format(sample["file"])
    # The slot's WaveFile holds the file open; release it before replacing it.
    unload_wave_sample(ctx)
    for _led in red_leds:
        _led.value = False
    try:
        frames = ctx.recorder.save(path)
        print("saved {} ({:.2f} s), {} KB left on the drive".format(
            path, frames / RATE, (drive_free() or 0) // 1024))
    except OSError as error:
        print("{} not saved: {}, {} KB left on the drive".format(
            path, error, (drive_free() or 0) // 1024))
    ctx.recorder.frames = 0
    load_selected_wave_sample(ctx)
    update_red_leds(ctx)


class HandleValue:
    """The handle position as a plain ``.value``, for Python-side readers."""

    @property
    def value(self):
        return handle_control.a


handle_value = HandleValue()


# --- Controls ---

# The three side buttons are switches to ground with an internal pull-up,
# pressed reads low, so they share one Keys group. key_number is the index
# into this tuple.
BUTTON_PINS = (board.BUTTON_TOP, board.BUTTON_MIDDLE, board.BUTTON_BOTTOM)

# key_number in the keypad.Keys group:
#   0 = TOP       -- already used to step presets.
#   1 = MIDDLE    -- steps the white LEDs / selected sample.
#   2 = BOTTOM    -- plays the selected sample, per its playmode.
TOP = 0
MIDDLE = 1
BOTTOM = 2

keys = keypad.Keys(BUTTON_PINS, value_when_pressed=False, pull=True)

# The paddle is read from its HANDLE_OUT stage (GP20) rather than HANDLE_IN:
# HANDLE_IN is the travel end-stop, and its tactile click leaks a loud thump
# into the mic. HANDLE_OUT is the earlier "user is squeezing" switch with no
# hard end-stop. It is normally closed to ground, so with a pull-up it reads low
# at rest and high once held.
paddle = digitalio.DigitalInOut(board.HANDLE_OUT)
paddle.switch_to_input(pull=digitalio.Pull.UP)

# Top four red LEDs: one per preset. The lit one is the active preset.
# None are lit is clean passthrough.
RED_PINS = (board.LED_RED1, board.LED_RED2, board.LED_RED3, board.LED_RED4)

red_leds = []
for _pin in RED_PINS:
    _led = digitalio.DigitalInOut(_pin)
    _led.direction = digitalio.Direction.OUTPUT
    red_leds.append(_led)


# Four white LEDs: one per sample. The lit one is the active sample.
WHITE_PINS = (board.LED_WHITE1, board.LED_WHITE2, board.LED_WHITE3, board.LED_WHITE4)

white_leds = []
for _pin in WHITE_PINS:
    _led = digitalio.DigitalInOut(_pin)
    _led.direction = digitalio.Direction.OUTPUT
    white_leds.append(_led)


def update_red_leds(ctx):
    """Light as many red LEDs as the active preset's number (none on clean).

    In record mode all four flash together, and count down the time left
    while a take runs. In edit mode, where nothing can record, they flash in
    alternating pairs instead.
    """
    if ctx.active == RECORD_SPOT:
        if ctx.recorder is None:
            for index, _led in enumerate(red_leds):
                _led.value = (index % 2 == 0) == ctx.blink_on
            return
        if ctx.recorder.recording:
            # ceil() without math: 4 while more than 3/4 is left, ... 1, then 0
            lit = -int(-ctx.recorder.remaining * len(red_leds) // 1)
        else:
            lit = len(red_leds) if ctx.blink_on else 0
        for index, _led in enumerate(red_leds):
            _led.value = index < lit
        return
    for index, _led in enumerate(red_leds):
        _led.value = index < ctx.active


def update_white_leds(ctx):
    """Light as many white LEDs as the selected sample's number."""
    for index, _led in enumerate(white_leds):
        _led.value = index < ctx.white_active


def build_sample_chain(wave, sample, loop):
    """Build and wire the effect chain for a sample's wave file, if it has one.

    WIRING ORDER MATTERS, same as `build()`: work from the end backwards, so
    the call that hands the wave to something is the last one.

    :param wave: The `audiocore.WaveFile` to feed into the chain, or straight
        to the mixer if there are no effects.
    :param dict sample: One entry from `SAMPLES`, i.e. a
        ``load_samples()`` dict with "effects" and "blocks" keys.
    :param bool loop: Whether wave playback should loop. A sample's playmode
        does not change while it stays loaded, so this is fixed for the whole
        chain and threaded through every stage.
    :return: ``(chain, source)`` -- ``chain`` is the list of effect objects to
        `deinit()` later (empty if there are none), ``source`` is what to
        hand to ``mixer.voice[1].play()``.
    """
    effects_specs = sample["effects"]
    if not effects_specs:
        return [], wave

    preset = {"list": effects_specs, "blocks": sample["blocks"]}
    chain = create_effects(preset, blocks={"handle": handle_control}, **FORMAT)
    if not chain:
        # All effects in effects_specs were "enabled": false.
        return [], wave
    for index in range(len(chain) - 1, 0, -1):
        chain[index].play(chain[index - 1], loop=loop)
    chain[0].play(wave, loop=loop)
    return chain, chain[-1]


def load_selected_wave_sample(ctx):
    """Load the selected sample wave file.

    Points ``ctx.current_wave``, ``ctx.current_playmode``,
    ``ctx.current_effects`` and ``ctx.current_source`` at it, stopping and
    freeing whatever was loaded before. ``ctx.white_active`` is 1-based;
    ``SAMPLES`` is 0-based, so it is short by one. A white LED with no matching
    entry in ``SAMPLES`` (fewer samples configured than white LEDs) plays
    ``/<slot>.wav`` plain, if there is one, so recorded takes always play.

    A sample whose effects fail to build (an effect this firmware lacks, or
    one too many for RAM) falls back to the plain wave.

    :param ctx: The mutable state container.
    """
    unload_wave_sample(ctx)

    sample = sample_for_slot(ctx.white_active)
    ctx.current_playmode = sample["playmode"]
    path = "/{}".format(sample["file"])
    ctx.current_path = path
    try:
        wave = audiocore.WaveFile(path)
    except OSError as error:
        print("{} not loaded: {}".format(path, error))
        return
    shape = (wave.sample_rate, wave.channel_count, wave.bits_per_sample)
    if shape != (RATE, 1, 16):
        # The mixer refuses any other format, and an uncaught refusal stops
        # code.py, so the file is left unloaded rather than played.
        print(
            "{} not loaded: it is {} Hz, {} ch, {} bit, and the mic plays "
            "16-bit mono {} Hz WAVs".format(path, shape[0], shape[1], shape[2], RATE)
        )
        wave.deinit()
        return
    ctx.current_wave = wave

    if THROUGH_PRESET:
        # The preset is the sample's effect chain; its own list is unused.
        sample = dict(sample, effects=[])
    try:
        ctx.current_effects, ctx.current_source = build_sample_chain(
            ctx.current_wave, sample, loop=ctx.current_playmode != "oneshot"
        )
    except (ValueError, MemoryError) as error:
        print("{} effects failed: {}".format(path, error))
        ctx.current_effects, ctx.current_source = [], ctx.current_wave

    print(
        "loaded {} ({} Hz, {} ch, {} bit, {} mode, {} effect(s))".format(
            path,
            ctx.current_wave.sample_rate,
            ctx.current_wave.channel_count,
            ctx.current_wave.bits_per_sample,
            ctx.current_playmode,
            len(ctx.current_effects),
        )
    )


def unload_wave_sample(ctx):
    """Stop and free the loaded sample, its effects and its open file."""
    stop_current_wave(ctx)
    for _effect in ctx.current_effects:
        _effect.deinit()
    ctx.current_effects = []
    if ctx.current_wave is not None:
        ctx.current_wave.deinit()
        ctx.current_wave = None
    ctx.current_playmode = "oneshot"
    ctx.current_source = None
    gc.collect()


def sample_for_slot(slot):
    """The config entry for white LED ``slot`` (1-based), or a plain default."""
    if slot <= len(SAMPLES):
        return SAMPLES[slot - 1]
    return {
        "file": "{}.wav".format(slot),
        "playmode": "oneshot",
        "effects": [],
        "blocks": {},
    }


def sample_voice(ctx):
    """The mixer voice samples play on.

    The input mixer of the preset when samples play through it, otherwise
    voice 1 of the output mixer (always the case in record mode, so a slot
    preview there plays dry).
    """
    return ctx.head.voice[1] if ctx.head is not None else mixer.voice[1]


def set_gate(ctx, held):
    """Open or close the mic for the paddle, wherever the mic is mixed."""
    if THROUGH_PRESET and ctx.tune is not None:
        ctx.tune.gate = held
    elif ctx.head is not None:
        ctx.head.voice[0].level = 1.0 if held else 0.0
    else:
        mixer.voice[0].level = ctx.level if held else 0.0
    ctx.gate_open = held


def start_sample(ctx, loop=False):
    """Play the loaded sample from the top on whatever plays samples now."""
    if THROUGH_PRESET and ctx.tune is not None:
        try:
            ctx.tune.play_sample(ctx.current_path, loop)
        except (OSError, ValueError) as error:
            print("{} can't go through autotune: {}".format(ctx.current_path, error))
    else:
        try:
            voice = sample_voice(ctx)
            voice.level = SAMPLE_LEVEL
            voice.play(ctx.current_source, loop=loop)
        except (ValueError, RuntimeError) as error:
            print("{} can't play: {}".format(ctx.current_path, error))


def stop_current_wave(ctx):
    """Stop whatever the sample voice is playing, if anything."""
    if ctx.tune is not None:
        ctx.tune.stop_sample()
    sample_voice(ctx).stop()
    ctx.wave_playing = False


def play_current_wave_sample(ctx):
    """Handle a BOTTOM button press, per the selected sample's playmode.

    * "oneshot"   -- plays through once; a press while it is still playing
                     restarts it, since `audiomixer.MixerVoice.play` always
                     replaces whatever a voice is doing.
    * "hold"      -- starts looping; `release_current_wave()` stops it.
    * "startstop" -- toggles between looping and stopped.

    :param ctx: The mutable state container.
    """
    if ctx.current_source is None:
        print("no wave loaded for LED {}".format(ctx.white_active))
        return

    if ctx.current_effects:
        # Re-hand the wave to the head of the chain so the press restarts it.
        ctx.current_effects[0].play(
            ctx.current_wave, loop=ctx.current_playmode != "oneshot"
        )

    if ctx.current_playmode == "oneshot":
        start_sample(ctx)
        print("playing {} (oneshot)".format(ctx.white_active))
    elif ctx.current_playmode == "hold":
        start_sample(ctx, loop=True)
        ctx.wave_playing = True
        print("playing {} (hold)".format(ctx.white_active))
    elif ctx.current_playmode == "startstop":
        if ctx.wave_playing:
            stop_current_wave(ctx)
            print("stopped {} (startstop)".format(ctx.white_active))
        else:
            start_sample(ctx, loop=True)
            ctx.wave_playing = True
            print("playing {} (startstop)".format(ctx.white_active))


def release_current_wave(ctx):
    """Handle a BOTTOM button release -- only "hold" cares about this.

    :param ctx: The mutable state container.
    """
    if ctx.current_playmode == "hold":
        stop_current_wave(ctx)
        print("stopped {} (hold released)".format(ctx.white_active))


# The handle travel pot, offered to the presets as the block named
# "$handle".
handle_pot = analogio.AnalogIn(board.HANDLE_POSITION)
handle_control = synthio.Math(synthio.MathOperation.SUM, 0.0, 0.0, 0.0)


# The volume knob is a plain potentiometer across 3V3 with its wiper on GP29.
knob = analogio.AnalogIn(board.VOLUME)


def apply_volume(ctx, force=False):
    """Set the DAC digital volume from the knob, if the knob has moved.

    The knob's travel maps linearly onto `VOLUME_MIN_DB` .. `VOLUME_MAX_DB`,
    except for `VOLUME_OFF_FRACTION` at the anticlockwise end, which mutes.

    Small movements are ignored: the wiper is noisy enough to jitter by a few
    hundred counts while nobody is touching it, and each change costs an I2C
    write in the middle of the audio loop.

    :param ctx: The mutable state container.
    :param bool force: Apply the current position even if it has not moved --
        used for the first read, and after anything else has written the DAC
        volume.
    """
    raw = knob.value
    if not force and abs(raw - ctx.knob_applied) < VOLUME_DEADBAND:
        return
    ctx.knob_applied = raw

    fraction = raw / 65535
    if fraction <= VOLUME_OFF_FRACTION:
        # -66 dB is the bottom of the codec's digital volume scale; below that
        # the codes are reserved rather than usable, so this is as close to off
        # as this control goes. It is inaudible.
        ctx.volume = None
        codec.dac_volume = -66
        print("volume off")
        return

    # Rescale so the usable part of the travel still covers the whole range.
    fraction = (fraction - VOLUME_OFF_FRACTION) / (1 - VOLUME_OFF_FRACTION)
    ctx.volume = VOLUME_MIN_DB + fraction * (VOLUME_MAX_DB - VOLUME_MIN_DB)
    codec.dac_volume = ctx.volume
    print("volume {:+.1f} dB".format(codec.dac_volume))


# Raw handle travel, 0.0 out to 1.0 in, and the travel at which the paddle
# switch opens the mic.
handle_travel = 0.0
handle_open = HANDLE_OPEN_GUESS
# The learned ends of the pot's swing, in ADC counts, and the lowest reading
# seen so far.
handle_out = HANDLE_OUT_COUNTS
handle_low = HANDLE_OUT_COUNTS
handle_in = HANDLE_OUT_COUNTS - HANDLE_MIN_SPAN


def read_handle():
    """Update `handle_control` from the handle position: 0.0 until the
    squeeze passes the mic-open click plus HANDLE_DEAD_ZONE, 1.0 fully in."""
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
    """While squeezed, print each new note autotune snaps to, and the notes
    the harmony sings with it, so the console can be checked against the
    editor's readout."""
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
    """Called as the paddle closes: move the mic-open point toward the travel
    just read. A lower reading is taken at once; a higher one (a fast squeeze
    overshoots the click before the next read) only a quarter of the way."""
    global handle_open
    if handle_travel < handle_open:
        handle_open = handle_travel
    else:
        handle_open += (min(handle_travel, 0.6) - handle_open) * 0.25
    print("handle opens the mic at {:.2f} of its travel (raw {:.0f} out, {:.0f} in)".format(
        handle_open, handle_out, handle_in))


# --- Main demo setup and loop ---

data_context = DataContext()
data_context.paddle_held = paddle.value

# Initialize the first LED and load its wave file.
update_white_leds(data_context)
load_selected_wave_sample(data_context)

print(
    "demo v2: pack {!r}, {} preset(s), {} Hz, mic gain {:.0f} dB, codec ADC HPF {:d} Hz".format(
        PACK_NAME, len(PRESETS), RATE, codec.mic_gain, HPF_HZ
    )
)
print("TOP button = next preset ({} spots, 0 = clean)".format(len(PRESETS) + 1))
print(
    "volume knob = DAC volume, {:.0f} to {:+.0f} dB".format(
        VOLUME_MIN_DB, VOLUME_MAX_DB
    )
)
print('handle travel = "$handle" block, 0.0 (out) to 1.0 (in)')

# The DAC is permanently un-muted. The paddle gates only mixer voice 0
# (the mic/effects chain) so that wave playback on voice 1 still works with the
# handle released.
codec.dac_soft_mute = False
apply_volume(data_context, force=True)
mixer.voice[0].level = 0.0  # mic chain silent until the paddle is squeezed
# Seed the handle before the first chain is built, so a preset that maps it
# starts at the handle's real position rather than sliding up from 0.
for _ in range(int(1 / HANDLE_SMOOTHING) + 1):
    read_handle()
select_preset(data_context, 0)

try:
    while True:
        # Drain every button event that arrived since the last pass.
        event = keys.events.get()
        while event is not None:
            if event.key_number == TOP:
                data_context.top_held = event.pressed
            if event.key_number == MIDDLE:
                data_context.middle_held = event.pressed
            if event.pressed and event.key_number == TOP:
                # Step to the next spot in the cycle, wrapping around. The
                # cycle is two longer than the preset count: spot 0 is clean
                # and the last spot is record mode.
                select_preset(data_context, (data_context.active + 1) % (RECORD_SPOT + 1))
            if event.pressed and event.key_number == MIDDLE:
                # Cycle the white LEDs: 1 -> 2 -> 3 -> 4 -> 1 ...
                # No empty spot; exactly one is always lit.
                data_context.white_active = (
                    data_context.white_active % len(white_leds) + 1
                )
                update_white_leds(data_context)
                load_selected_wave_sample(data_context)
                print("white LED {}".format(data_context.white_active))
            if event.pressed and event.key_number == BOTTOM:
                play_current_wave_sample(data_context)
            if event.released and event.key_number == BOTTOM:
                release_current_wave(data_context)
            event = keys.events.get()

        # TOP + MIDDLE held together for SHUTDOWN_HOLD_SECONDS powers off.
        if data_context.top_held and data_context.middle_held:
            if data_context.combo_since is None:
                data_context.combo_since = time.monotonic()
            elif time.monotonic() - data_context.combo_since >= SHUTDOWN_HOLD_SECONDS:
                print(
                    "TOP+MIDDLE held {}s: shutting down".format(SHUTDOWN_HOLD_SECONDS)
                )
                power_hold = digitalio.DigitalInOut(board.POWER_HOLD)
                power_hold.direction = digitalio.Direction.OUTPUT
                power_hold.value = False
        else:
            data_context.combo_since = None

        # The paddle gates the mic/effects chain (mixer voice 0), or in
        # record mode runs a take. Changes are acted on PADDLE_SETTLE_SECONDS
        # later, without sleeping, so autotune and the recorder keep pace.
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
                recorder.stop()  # at once: the release click stays out
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
            # A take that ended (released, or out of time) gets saved.
            if recorder is not None and recorder.frames and not recorder.recording:
                save_take(data_context)
            if now - data_context.blink_since >= RECORD_BLINK_SECONDS:
                data_context.blink_on = not data_context.blink_on
                data_context.blink_since = now
                update_white_leds(data_context)
            update_red_leds(data_context)

        # The knob is free-running: it is read every pass and only acted on
        # when it has actually moved.
        apply_volume(data_context)

        # The handle is read every pass too, always applied
        read_handle()
        # While squeezed, print the handle whenever it moves a step, so the
        # console shows what "$handle" reads at each point of a squeeze.
        if data_context.paddle_held and abs(handle_travel - data_context.handle_shown) >= 0.05:
            data_context.handle_shown = handle_travel
            print("squeeze {:.2f} of travel, $handle {:.2f}".format(handle_travel, handle_control.a))

        # An autotune preset with a squeeze key switches key / scale when the
        # handle goes far in, and back once it eases off (hysteresis, so a
        # hand hovering near the threshold doesn't flip it back and forth).
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

        # Reading .overflow clears it. It trips if the playback side falls
        # behind the mic, which should not happen here. Both run off the
        # same BCLK, so a report means the DSP chain can't keep up.
        # (Skipped while record mode sits idle: nothing reads the mic then,
        # so it is bound to overflow.)
        recorder = data_context.recorder
        idle = data_context.active == RECORD_SPOT and not (
            recorder is not None and recorder.recording
        )
        if not idle and mic.overflow:
            if data_context.tune is not None:
                print("overflow (autotune took {:.1f} ms of 16)".format(data_context.tune.busy_ms))
            else:
                print("overflow")

        # Autotune and the recorder each block on one 16 ms mic read, which
        # paces the loop; otherwise nothing here needs to run faster.
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
