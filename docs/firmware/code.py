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

* All four red LEDs light and the selected sample's white LED blinks.
* MIDDLE picks the slot to record into, BOTTOM plays it back as usual.
* Hold the handle to record, release to stop and save over the slot's file.
  The red LEDs count the time left down from 4 to 0; at 0 the take stops.
* Needs the drive writable by CircuitPython (the default, see boot.py). In
  edit mode (TOP held at power-on) the red LEDs flash and nothing records.

Indication:

* Top four red LEDs, one per preset: the lit one is the active preset. On the
  clean spot they are all dark.

* Bottom four white LEDs, one per sample: the lit one is the currently selected
  sample.
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
from autotune import Autotune
from config_loader import create_effects, create_preset, is_autotune, load_samples
from recorder import Recorder

RATE = 16000  # frame rate on the wire; also what the codec's FLL expects

# Where to look for the preset config
CONFIG_PATHS = ("/config.json", "/ep2350_circuitpython_config.json")

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

# The handle travel pot on GP28, in ADC counts at each end of its swing. The
# whole range is only ~2520 counts of the 16-bit scale, so it has to be mapped
# explicitly;
HANDLE_OUT_COUNTS = 32370  # at rest, handle all the way out
HANDLE_IN_COUNTS = 29850  # squeezed fully in

# Handle position potentiometer smoothing for noisy signal
HANDLE_SAMPLES = 16
HANDLE_SMOOTHING = 0.25

# --- Record mode ---

# Longest take, in seconds. Held in RAM (32 KB per second at 16 kHz) and
# written to flash after the take, so it is bounded by free memory.
RECORD_SECONDS = 4
# How fast the selected white LED blinks in record mode, in seconds per toggle.
RECORD_BLINK_SECONDS = 0.25

# The paddle switch bounces and its click reaches the mic; the mic gate (and
# the start of a take) waits this long after a paddle change.
PADDLE_SETTLE_SECONDS = 0.1

# DC-blocking high-pass corner, Hz. Applied in the codec's ADC path (see the
# codec.configure_adc_highpass() call below)
HPF_HZ = 120

# Audio format of the chain. Passed to every effect config_loader builds; the
# JSON only describes the sound-affecting parameters, never the format ones.
FORMAT = {
    "buffer_size": 1024,
    "sample_rate": RATE,
    "bits_per_sample": 16,
    "samples_signed": True,
    "channel_count": 1,
}

# Autotune hands the chain 256-sample blocks (16 ms), so presets that use it
# build their effects with a matching 512-byte buffer. Untested on the device
# against Adafruit's 1024; see NOTES.md.
AUTOTUNE_FORMAT = dict(FORMAT, buffer_size=512)


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
        # `time.monotonic()` of the last paddle change still waiting to be
        # acted on after PADDLE_SETTLE_SECONDS, or None.
        self.paddle_changed = None
        # Blink phase of the white LED in record mode.
        self.blink_on = True
        self.blink_since = 0.0


# --- Preset config ---


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
PACK_NAME = CONFIG.get("name", "none")

# Presets are passed to config_loader whole, since a preset is more than its
# effect list: it may carry a "blocks" mapping of LFOs and Math blocks that
# its effects refer to by name.
PRESETS = list(CONFIG.get("presets", ())[:MAX_PRESETS])

# One wave file per white LED slot, each with a playmode telling the BOTTOM
# button how to play it back. See config_loader.load_samples().
SAMPLES = load_samples(CONFIG, max_samples=MAX_SAMPLES)

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
mixer = audiomixer.Mixer(
    voice_count=2,
    buffer_size=FORMAT["buffer_size"],
    channel_count=FORMAT["channel_count"],
    bits_per_sample=FORMAT["bits_per_sample"],
    samples_signed=FORMAT["samples_signed"],
    sample_rate=FORMAT["sample_rate"],
)
i2s.play(mixer, loop=True)

# --- Chain switching ---


def teardown(ctx):
    """Stop the current voice, free the current chain's effects"""
    mixer.voice[0].stop()
    for _effect in ctx.chain:
        _effect.deinit()
    ctx.chain = []
    if ctx.tune is not None:
        if ctx.tune.shifter is not None:
            ctx.tune.shifter.deinit()
        ctx.tune.feed.deinit()
        ctx.tune = None
    if ctx.recorder is not None:
        ctx.recorder.deinit()
        ctx.recorder = None
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
    """
    if not preset:
        ctx.chain = []
        mixer.voice[0].play(mic, loop=True)
        return

    # Build first: a config error or an out-of-memory here must not leave a
    # half-wired graph running. create_preset() builds without wiring, which
    # is what lets the wiring below run output-first.
    specs = preset.get("list", ())
    fmt = AUTOTUNE_FORMAT if any(is_autotune(spec) for spec in specs) else FORMAT
    tune_args, ctx.chain = create_preset(
        preset, blocks={"handle": handle_control}, **fmt
    )

    if tune_args is not None:
        # Autotune reads the mic itself, so it, not the mic, heads the chain.
        # Python reads speed / humanize each block; "$handle" is offered to
        # them as the raw handle position, since a synthio Math block only
        # updates while an effect ticks it.
        for name in ("speed", "humanize"):
            if tune_args.get(name) is handle_control:
                tune_args[name] = handle_value
        ctx.tune = Autotune(mic, fmt, **tune_args)
        source = ctx.tune.output
    else:
        source = mic

    if not ctx.chain:
        mixer.voice[0].play(source, loop=True)
    else:
        mixer.voice[0].play(ctx.chain[-1], loop=True)
        for index in range(len(ctx.chain) - 1, 0, -1):
            ctx.chain[index].play(ctx.chain[index - 1])
        ctx.chain[0].play(source, loop=ctx.tune is not None)

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
        print("record mode, {} bytes free".format(gc.mem_free()))
    else:
        print(
            "preset {} of {} ({} effects{}), {} bytes free".format(
                ctx.active,
                len(PRESETS),
                len(ctx.chain),
                ", autotune via " + ctx.tune.shifter_name if ctx.tune else "",
                gc.mem_free(),
            )
        )
    codec.dac_soft_mute = muted


# --- Record mode ---


def enter_record_mode(ctx):
    """Set up record mode: nothing plays on mixer voice 0, the mic is free.

    Only one consumer can read the I2SIn, so the mic is left unwired here and
    the recorder calls ``mic.record()`` itself while the paddle is held.
    """
    ctx.chain = []
    if CAN_RECORD:
        ctx.recorder = Recorder(mic, seconds=RECORD_SECONDS, sample_rate=RATE)
    else:
        print("record mode: drive is read-only (edit mode), recording is off")
    ctx.blink_on = True
    ctx.blink_since = time.monotonic()


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
        print("saved {} ({:.2f} s)".format(path, frames / RATE))
    except OSError as error:
        print("{} not saved: {}".format(path, error))
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
    """Light the red LED belonging to the active preset, if any.

    In record mode all four light, counting down the time left while a take
    runs. In edit mode, where nothing can record, they flash together.
    """
    if ctx.active == RECORD_SPOT:
        if ctx.recorder is None:
            lit = len(red_leds) if ctx.blink_on else 0
        elif ctx.recorder.recording:
            # ceil() without math: 4 while more than 3/4 is left, ... 1, then 0
            lit = -int(-ctx.recorder.remaining * len(red_leds) // 1)
        else:
            lit = len(red_leds)
        for index, _led in enumerate(red_leds):
            _led.value = index < lit
        return
    for index, _led in enumerate(red_leds):
        _led.value = ctx.active == index + 1


def update_white_leds(ctx):
    """Light exactly the currently selected white LED; blink it in record mode."""
    on = ctx.blink_on or ctx.active != RECORD_SPOT
    for index, _led in enumerate(white_leds):
        _led.value = on and ctx.white_active == index + 1


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
    try:
        ctx.current_wave = audiocore.WaveFile(path)
    except OSError as error:
        print("{} not loaded: {}".format(path, error))
        return

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


def stop_current_wave(ctx):
    """Stop whatever mixer voice 1 is playing, if anything.
    .
    """
    mixer.voice[1].stop()
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
        mixer.voice[1].play(ctx.current_source)
        print("playing {} (oneshot)".format(ctx.white_active))
    elif ctx.current_playmode == "hold":
        mixer.voice[1].play(ctx.current_source, loop=True)
        ctx.wave_playing = True
        print("playing {} (hold)".format(ctx.white_active))
    elif ctx.current_playmode == "startstop":
        if ctx.wave_playing:
            stop_current_wave(ctx)
            print("stopped {} (startstop)".format(ctx.white_active))
        else:
            mixer.voice[1].play(ctx.current_source, loop=True)
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


def read_handle():
    """Update `handle_control` from the handle position, 0.0 out to 1.0 in."""
    total = 0
    for _ in range(HANDLE_SAMPLES):
        total += handle_pot.value
    raw = total / HANDLE_SAMPLES

    fraction = (HANDLE_OUT_COUNTS - raw) / (HANDLE_OUT_COUNTS - HANDLE_IN_COUNTS)
    fraction = min(1.0, max(0.0, fraction))
    handle_control.a += (fraction - handle_control.a) * HANDLE_SMOOTHING


# --- Main demo setup and loop ---

data_context = DataContext()
data_context.paddle_held = paddle.value

# Initialize the first LED and load its wave file.
update_white_leds(data_context)
load_selected_wave_sample(data_context)

print(
    "demo v2: pack {!r}, {} preset(s), mic gain {:.0f} dB, codec ADC HPF {:d} Hz".format(
        PACK_NAME, len(PRESETS), codec.mic_gain, HPF_HZ
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
                    data_context.recorder.start()
                    print("recording into slot {}".format(data_context.white_active))
            else:
                mixer.voice[0].level = 1.0 if held else 0.0

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
            print("overflow")

        # Autotune and the recorder each block on one 16 ms mic read, which
        # paces the loop; otherwise nothing here needs to run faster.
        if data_context.tune is not None:
            data_context.tune.step()
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
