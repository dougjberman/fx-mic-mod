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
* an effects-only mode (``"effects_only": true``): no samples and no record
  mode, and up to four pages of four presets instead, MIDDLE stepping the page.
* a shake effect on sample playback (``"shake"``, see shake.py): shake the
  mic while a sample plays and it zips, warbles or glitches.

Up to four presets are used (one per red LED). Only one chain exists at a time:
switching tears the old chain down and builds the new one.

Controls:

* TOP side button -- steps to the next spot in the preset cycle when let go.
                     The cycle is the configured presets plus a "clean" spot
                     with no effects at all, where the mic feeds the output
                     directly. The LEDs change on letting go, and the preset
                     loads BUTTON_GAP_SECONDS later; a press in that time is a
                     double tap and steps back one spot instead. A press
                     while a preset is still loading is ignored.
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
                        LEDs. Like TOP, it steps when let go, and a double
                        tap steps back one sample instead.
* BOTTOM side button -- plays the current sample wave file with any effects
                        configured for it. A tap plays it once, a hold
                        plays it until let go, and a double tap plays it
                        backwards.
* TOP + MIDDLE, held together for 800ms -- shuts the unit down by driving
                        POWER_HOLD low.
* Left alone on batteries for ``"auto_off_minutes"`` (10 by default), the mic
  turns itself off the same way. Every LED blinks for the last 10 seconds;
  anything done then keeps it on.

Record mode (TOP past the last preset):

* All four red LEDs flash together; the white LEDs show the selected slot.
* MIDDLE picks the slot to record into, BOTTOM plays it back as usual.
* Hold the handle to record, release to stop and save over the slot's file.
  The red LEDs stop flashing and count the time left down from 4 to 0; at 0
  the take stops.
* Needs the drive writable by CircuitPython (the default, see boot.py). In
  edit mode (TOP held at power-on) the red LEDs flash in alternating pairs
  and nothing records.

Effects-only mode (``"effects_only": true`` in config.json):

* Up to 16 presets, in pages of four. TOP steps through the current page's
  presets (the red LEDs, as above), from the last back to the first; there is
  no Clean spot in the cycle and no record mode. A double tap steps back one.
* MIDDLE steps to the next page, wrapping from the last back to page 1, and a
  double tap steps back a page. The white LEDs count the page: page 2 lights
  two, all four is page 4. The mic lands on the new page's first preset.
* BOTTOM goes to Clean, with every LED dark. It is the only way there. From
  Clean, TOP or MIDDLE goes to preset 1 of page 1. The mic starts on Clean.

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
try:
    import supervisor
except ImportError:  # desktop tests
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

# The firmware's release number. The editor reads this line from the mic's
# code.py before Save to mic, to check the presets only use settings this
# firmware knows, so keep it on one line in this form. Raise it with every
# release that adds a config setting, add that setting to the editor's
# FIRMWARE_NEEDS table with the new number, and add a row for the release to
# the "What's new" table on the install page, which its update check lists.
FIRMWARE_VERSION = "1.2.0"
print("fx-mic-mod firmware", FIRMWARE_VERSION)

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
# "feed_phase": 0 or 1 for the double buffer, or null for the two-block
# window (see autotune.FEED_PHASE).
_phase = CONFIG.get("feed_phase", autotune.FEED_PHASE)
if _phase is None or (_phase in (0, 1) and not isinstance(_phase, bool)):
    autotune.FEED_PHASE = _phase
else:
    print("feed_phase must be 0, 1 or null; using {}".format(autotune.FEED_PHASE))
# Sample counts in config.json (grain sizes, pitch_shift windows) are written
# for 16 kHz, so they keep their length in time at other rates.
RATE_SCALE = RATE / 16000
SAMPLE_COUNT_PARAMS = ("grain_size", "window", "overlap")

MAX_PRESETS = 4
MAX_SAMPLES = 4

# "effects_only": true gives up the samples and record mode for MAX_PAGES
# pages of MAX_PRESETS presets, the page stepped by MIDDLE and shown on the
# white LEDs. Only the selected preset's effects are ever built, so more
# presets cost only their JSON in RAM.
MAX_PAGES = 4
EFFECTS_ONLY = CONFIG.get("effects_only", False)
if not isinstance(EFFECTS_ONLY, bool):
    print("effects_only must be true or false; using false")
    EFFECTS_ONLY = False

# How long TOP + MIDDLE must be held together, in seconds, to shut down.
SHUTDOWN_HOLD_SECONDS = 0.8

# --- Auto-off ---

# Minutes the mic may sit unused before it turns itself off, to save the
# batteries; config.json's "auto_off_minutes" sets it, 0 for never. Unused
# means no button press, no squeeze (singing needs one), no knob turn, no
# sample playing and no take recording. Over USB to a computer it stays on.
AUTO_OFF_MINUTES = 10
# For the last AUTO_OFF_WARN_SECONDS every LED blinks, toggling every
# AUTO_OFF_BLINK_SECONDS; anything done then keeps the mic on.
AUTO_OFF_WARN_SECONDS = 10
AUTO_OFF_BLINK_SECONDS = 0.25

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

# Fraction of the travel at the counterclockwise end that means silence, so the
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
HANDLE_DEAD_ZONE = 0.4

# --- The sample button ---

# A press always starts the sample at once, so a tap never waits. Held at
# least TAP_HOLD_SECONDS, letting go stops it (hold to play); let go sooner
# and it plays to the end (a tap). A press within DOUBLE_TAP_SECONDS of a
# tap's press is a double tap: it plays the sample backwards from the end.
# The forward start of the first tap is cut off by it, so keep the window
# short: a slower second press starts the sample again forwards. Every
# sample plays this way; a config's "playmode" is read but no longer used.
TAP_HOLD_SECONDS = 0.25
DOUBLE_TAP_SECONDS = 0.3

# --- The preset and sample (or page) buttons ---

# TOP and MIDDLE step forward when let go, not when pressed, so holding one
# does nothing until it comes up, and TOP + MIDDLE held to power off steps
# neither. On letting go the LEDs show the next spot at once, and the step
# itself waits BUTTON_GAP_SECONDS: a press in that time is a double tap, and
# goes back one at once instead (lights and all), so nothing is loaded twice.
# A press that came in while a preset was still loading is ignored, and so
# is its release.
BUTTON_GAP_SECONDS = 0.2

# --- Record mode ---

# Longest take, in seconds. Held in RAM (48 KB per second at 24 kHz) and
# written to flash after the take, so it is bounded by free memory.
RECORD_SECONDS = 4
# How fast the red LEDs flash in record mode, in seconds per toggle.
RECORD_BLINK_SECONDS = 0.25

# Handle travel (0 out .. 1 in) at which an autotune preset's "squeeze_key"
# (or a vocoder's "squeeze_chord") takes over, and below which it lets go again.
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
        # Index into the cycle: 0 == clean, 1..4 == that preset of the page,
        # RECORD_SPOT == record mode.
        self.active = 0
        # The page of presets, 0-based. Always 0 unless EFFECTS_ONLY.
        self.page = 0

        # Active white LED, 1..4 (top to bottom).
        self.white_active = 1

        # WaveFile currently loaded,  ``None`` if there is no matching sample
        # configured or its file could not be loaded.
        self.current_wave = None
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
        # True from a press until the sample is stopped, so a release knows
        # there is something to stop.
        self.wave_playing = False
        # The sample button: when the current press began, whether
        # it is a double tap, and when the last tap began (None unless the
        # last press was a tap that a second press could follow), in
        # milliseconds as `event_ms()` gives them.
        self.press_at = 0
        self.press_double = False
        self.tap_at = None
        # The loaded sample backwards, streamed from the drive (an
        # autotune.BackwardsStream), built on its first double tap, or None.
        self.backwards_stream = None
        # The shake effect on samples the native mixer plays (a
        # shake.ShakeFX), and the stutter roll for samples autotune streams
        # (a shake.Roll). None with the shake off or no accelerometer.
        self.shake_fx = None
        self.shake_roll = None

        # A TOP or MIDDLE step waiting out BUTTON_GAP_SECONDS after its
        # release: [button, when it was let go (as `event_ms()` gives it),
        # where it steps to (see `step_target()`)], or None.
        self.pending_step = None
        # The TOP / MIDDLE presses whose release does nothing: the second
        # press of a double tap, one pressed while a preset loaded, and both
        # of a TOP + MIDDLE combo.
        self.skip_release = set()

        # Whether TOP / MIDDLE are currently held down, for the shutdown combo.
        self.top_held = False
        self.middle_held = False
        # When the last preset switch finished, in ms as `event_ms()` gives
        # them. A TOP press from before then came in while the preset loaded.
        self.loaded_at = 0
        # the `time.monotonic()` the combo started, or None while it is not both-down.
        self.combo_since = None
        # board.POWER_HOLD, claimed the first time the mic powers off
        self.power_hold = None
        # Auto-off: the `time.monotonic()` the mic was last in use, whether
        # the LEDs are blinking the warning, and whether it has powered off
        # (and is still running, so something else powers it).
        self.used_at = 0.0
        self.auto_off_warning = False
        self.auto_off_done = False

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
PRESETS = list(CONFIG.get("presets", ())[:MAX_PRESETS * (MAX_PAGES if EFFECTS_ONLY else 1)])
# How many pages of presets MIDDLE steps through: one unless EFFECTS_ONLY.
PAGES = max(1, (len(PRESETS) + MAX_PRESETS - 1) // MAX_PRESETS)


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

# One wave file per white LED slot. See config_loader.load_samples().
SAMPLES = [] if EFFECTS_ONLY else load_samples(CONFIG, max_samples=MAX_SAMPLES)
SAMPLES = [dict(e, effects=at_rate(e.get("effects"))) for e in SAMPLES]

# "preset": samples play dry into the selected preset, like the mic does.
# "own": each sample keeps its own effects (Adafruit's behavior).
try:
    THROUGH_PRESET = sample_routing(CONFIG) == "preset" and not EFFECTS_ONLY
except ValueError as error:
    print("config: {}".format(error))
    THROUGH_PRESET = False

# "handle_dead_zone": how much travel past the mic-open click still reads 0.
_dead_zone = CONFIG.get("handle_dead_zone", HANDLE_DEAD_ZONE)
if isinstance(_dead_zone, (int, float)) and not isinstance(_dead_zone, bool) and 0 <= _dead_zone <= 0.5:
    HANDLE_DEAD_ZONE = _dead_zone
else:
    print("handle_dead_zone must be a number from 0 to 0.5; using {}".format(HANDLE_DEAD_ZONE))

# "trim_takes": false keeps the silence before and after the sound in a take
# recorded on the mic. On by default (see recorder.ONSET_LEVEL).
TRIM_TAKES = CONFIG.get("trim_takes", True)
if not isinstance(TRIM_TAKES, bool):
    print("trim_takes must be true or false; using true")
    TRIM_TAKES = True

# "normalize_takes": true scales each take recorded on the mic so its loudest
# moment sits at -3 dBFS, boosting by at most +12 dB. Off by default (see
# recorder.NORMALIZE_PEAK).
NORMALIZE_TAKES = CONFIG.get("normalize_takes", False)
if not isinstance(NORMALIZE_TAKES, bool):
    print("normalize_takes must be true or false; using false")
    NORMALIZE_TAKES = False

# "sample_level": how loud samples play, 0..1. Full level by default, so a
# take recorded on the mic plays back as loud as it was sung; the install
# guide's converter brings imported files down to about the mic's level.
SAMPLE_LEVEL = 1.0
_sample_level = CONFIG.get("sample_level", SAMPLE_LEVEL)
if isinstance(_sample_level, (int, float)) and not isinstance(_sample_level, bool) and 0 <= _sample_level <= 1:
    SAMPLE_LEVEL = _sample_level
else:
    print("sample_level must be a number from 0 to 1; using {}".format(SAMPLE_LEVEL))

_auto_off = CONFIG.get("auto_off_minutes", AUTO_OFF_MINUTES)
if isinstance(_auto_off, (int, float)) and not isinstance(_auto_off, bool) and 0 <= _auto_off <= 240:
    AUTO_OFF_MINUTES = _auto_off
else:
    print("auto_off_minutes must be a number from 0 to 240; using {}".format(AUTO_OFF_MINUTES))
# None: never.
AUTO_OFF_SECONDS = AUTO_OFF_MINUTES * 60 if AUTO_OFF_MINUTES else None

# "shake": what shaking the mic does to a sample as it plays (see shake.py).
# None for off; effects-only mode has no samples to shake.
SHAKE = None if EFFECTS_ONLY else shake.settings(CONFIG)

# The cycle position of record mode: one past the last preset. None in
# effects-only mode, which has no record mode.
RECORD_SPOT = None if EFFECTS_ONLY else len(PRESETS) + 1


def page_presets(ctx):
    """The presets TOP steps through now: the current page's."""
    return PRESETS[ctx.page * MAX_PRESETS:(ctx.page + 1) * MAX_PRESETS]


def next_spot(ctx):
    """The cycle position TOP steps to: clean, the page's presets, then
    record mode, then round to clean. In effects-only mode the page's
    presets go round without clean, and clean steps to page 1's first."""
    if EFFECTS_ONLY:
        if not ctx.active:
            ctx.page = 0
        return ctx.active % len(page_presets(ctx)) + 1 if PRESETS else 0
    return (ctx.active + 1) % (len(page_presets(ctx)) + 2)


def prev_spot(ctx, spot):
    """The cycle position a TOP double tap goes back to from ``spot``: the
    one `next_spot()` would step from to get there. In effects-only mode,
    clean goes back to page 1's last preset."""
    if EFFECTS_ONLY:
        if not spot:
            ctx.page = 0
            return len(page_presets(ctx)) if PRESETS else 0
        return (spot - 2) % len(page_presets(ctx)) + 1
    return (spot - 1) % (len(page_presets(ctx)) + 2)

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

# The accelerometer shares the codec's I2C bus. Looked for only with the
# shake on.
shake_meter = shake.ShakeMeter.find(board.I2C()) if SHAKE else None

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
        for name in ("speed", "humanize", "harmony_level", "vocoder_mix"):
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
    # Light the new spot's LEDs first: a heavy preset (the vocoder) takes
    # most of a second to build, and dark LEDs meanwhile look like a missed
    # press. Record mode's flashing waits for its own setup.
    if index != RECORD_SPOT:
        for _index, _led in enumerate(red_leds):
            _led.value = _index < index

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
    # Building the chain takes longer than one block, so the mic always
    # overflows once during a switch. Reading .overflow clears that, so the
    # console only reports overflows while a preset is actually running.
    mic.overflow
    ctx.loaded_at = now_ms()


# --- Record mode ---


def enter_record_mode(ctx):
    """Set up record mode: nothing plays on mixer voice 0, the mic is free.

    Only one consumer can read the I2SIn, so the mic is left unwired here and
    the recorder calls ``mic.record()`` itself while the paddle is held.
    """
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


# While a take saves, one LED at a time runs down the mic, red 1 at the top
# to white 4 at the bottom, then starts again at the top.
WATERFALL_STEP_SECONDS = 0.07


class SaveWaterfall:
    """Steps one LED at a time, never skipping one: a flash write can stall
    longer than a step, and then the next step just comes late."""

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
        """Run out the current pass to the bottom LED, so even a quick save
        shows a full sweep."""
        while self.lit % len(self.leds) != len(self.leds) - 1:
            time.sleep(max(0.0, WATERFALL_STEP_SECONDS - (time.monotonic() - self.moved)))
            self.step()
        time.sleep(WATERFALL_STEP_SECONDS)
        for _led in self.leds:
            _led.value = False


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
#   2 = BOTTOM    -- plays the selected sample: tap, hold or double tap.
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
    """Light as many white LEDs as the selected sample's number, or in
    effects-only mode the page's (none on clean)."""
    if EFFECTS_ONLY:
        lit = ctx.page + 1 if ctx.active else 0  # all dark on clean
    else:
        lit = ctx.white_active
    for index, _led in enumerate(white_leds):
        _led.value = index < lit


def build_sample_chain(wave, sample):
    """Build and wire the effect chain for a sample's wave file, if it has one.

    WIRING ORDER MATTERS, same as `build()`: work from the end backwards, so
    the call that hands the wave to something is the last one.

    :param wave: The `audiocore.WaveFile` to feed into the chain, or straight
        to the mixer if there are no effects.
    :param dict sample: One entry from `SAMPLES`, i.e. a
        ``load_samples()`` dict with "effects" and "blocks" keys.
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
        chain[index].play(chain[index - 1])
    chain[0].play(wave)
    return chain, chain[-1]


def load_selected_wave_sample(ctx):
    """Load the selected sample wave file.

    Points ``ctx.current_wave``, ``ctx.current_effects`` and ``ctx.current_source`` at it, stopping and
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
    """Stop and free the loaded sample, its effects and its open file."""
    stop_current_wave(ctx)
    for _effect in ctx.current_effects:
        _effect.deinit()
    ctx.current_effects = []
    if ctx.current_wave is not None:
        ctx.current_wave.deinit()
        ctx.current_wave = None
    ctx.current_source = None
    if ctx.backwards_stream is not None:
        ctx.backwards_stream.deinit()
        ctx.backwards_stream = None
    ctx.tap_at = None
    gc.collect()


def sample_for_slot(slot):
    """The config entry for white LED ``slot`` (1-based), or a plain default."""
    if slot <= len(SAMPLES):
        return SAMPLES[slot - 1]
    return {
        "file": "{}.wav".format(slot),
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


def backwards_stream(ctx):
    """The loaded sample backwards, as a BackwardsStream from the drive, or
    None if it can't be had. Built on the first double tap and kept until
    the sample is unloaded. (Through autotune the sample streams backwards
    inside autotune instead, so this isn't needed there.)

    Turning a sample round whole in RAM needs one unbroken block as big as
    the file, and the heap's biggest is about 120 KB even at power-on, so a
    3 s take never fit; the copy also stalled the mic for up to half a
    second and held its RAM while the slot was loaded. The stream starts at
    once and takes 16 KB whatever the length."""
    if ctx.backwards_stream is None:
        try:
            ctx.backwards_stream = autotune.BackwardsStream(ctx.current_path, RATE)
        except (MemoryError, OSError, ValueError) as error:
            print("{} can't play backwards: {}".format(ctx.current_path, error))
            return None
    return ctx.backwards_stream


def shake_echo(ctx):
    """The shake Echo for samples on the native mixer, built the first time
    one plays, or None with the shake off (or out of RAM for it)."""
    global shake_meter
    if ctx.shake_fx is None and shake_meter is not None:
        try:
            ctx.shake_fx = shake.ShakeFX(SHAKE, FORMAT)
        except (ValueError, MemoryError) as error:
            print("shake: off ({})".format(error))
            shake_meter = None
    return ctx.shake_fx


def start_sample(ctx, reverse=False):
    """Play the loaded sample from the top on whatever plays samples now,
    or from the end, backwards, with ``reverse``."""
    if THROUGH_PRESET and ctx.tune is not None:
        try:
            ctx.tune.play_sample(ctx.current_path, reverse=reverse)
        except (OSError, ValueError) as error:
            print("{} can't go through autotune: {}".format(ctx.current_path, error))
        return
    if ctx.backwards_stream is not None:
        ctx.backwards_stream.stop()
    stream = backwards_stream(ctx) if reverse else None
    wave = ctx.current_wave
    # The stream is a looped double buffer, refilled by the main loop, that
    # stops itself at the end; whatever plays it straight loops it.
    looped = stream is not None
    if looped:
        stream.start(time.monotonic())
        wave = stream.sample
    source = wave
    if ctx.current_effects:
        # Re-hand the wave to the head of the chain so the press restarts it.
        ctx.current_effects[0].play(wave, loop=looped)
        source = ctx.current_source
    if shake_echo(ctx) is not None:
        # Last on the sample's way to the mixer: its own effects, or the
        # preset it plays into, take the shaken sound.
        source = ctx.shake_fx.play(source, loop=looped and source is wave)
    try:
        voice = sample_voice(ctx)
        voice.level = SAMPLE_LEVEL
        voice.play(source, loop=looped and source is wave)
    except (ValueError, RuntimeError) as error:
        print("{} can't play: {}".format(ctx.current_path, error))


def stop_current_wave(ctx):
    """Stop whatever the sample voice is playing, if anything."""
    if ctx.tune is not None:
        ctx.tune.stop_sample()
    sample_voice(ctx).stop()
    if ctx.shake_fx is not None:
        ctx.shake_fx.stop()
    if ctx.backwards_stream is not None:
        ctx.backwards_stream.stop()
    ctx.wave_playing = False


# supervisor.ticks_ms() wraps at 2**29.
TICKS_MASK = (1 << 29) - 1


def event_ms(event):
    """When a button event happened, in ms: keypad's own timestamp, taken
    when it scanned the button, so a loop pass that ran long (a preset
    switch, or a 16 ms autotune block) doesn't blur a tap into a hold."""
    stamp = getattr(event, "timestamp", None)
    if stamp is None:
        stamp = int(time.monotonic() * 1000)
    return stamp & TICKS_MASK


def ms_between(earlier, later):
    return (later - earlier) & TICKS_MASK


def now_ms():
    """The time now, on the same clock as `event_ms()`."""
    if supervisor is not None:
        return supervisor.ticks_ms() & TICKS_MASK
    return int(time.monotonic() * 1000) & TICKS_MASK


def pressed_while_loading(ctx, at):
    """Whether a press at ``at`` came in while the last preset switch was
    loading. Its LEDs were already lit, so such a press is a repeat of the one
    being loaded, not a step on from it."""
    return 0 < ms_between(at, ctx.loaded_at) < TICKS_MASK // 2


def step_target(ctx, key, back=False):
    """Where a TOP or MIDDLE press steps to from here, on (or ``back`` one
    for a double tap), without stepping: (page, cycle position) for TOP and
    for MIDDLE in effects-only mode, else the white LED (sample slot)."""
    page = ctx.page
    if key == MIDDLE and not EFFECTS_ONLY:
        slots = len(white_leds)
        return (ctx.white_active - 2) % slots + 1 if back else ctx.white_active % slots + 1
    if key == TOP:
        spot = prev_spot(ctx, ctx.active) if back else next_spot(ctx)
        target = (ctx.page, spot)
    elif not ctx.active:
        # A page turn from clean: page 1 (or a double tap's last page).
        target = (PAGES - 1 if back else 0, 1)
    else:
        target = ((ctx.page + (-1 if back else 1)) % PAGES, 1)
    ctx.page = page
    return target


def show_step(ctx, key, target):
    """Light the LEDs for ``target`` ahead of the step itself."""
    if key == MIDDLE and not EFFECTS_ONLY:
        lit = target
    else:
        page, spot = target
        if spot != RECORD_SPOT:
            for index, _led in enumerate(red_leds):
                _led.value = index < spot
        if not EFFECTS_ONLY:
            return
        lit = page + 1 if spot else 0
    for index, _led in enumerate(white_leds):
        _led.value = index < lit


def take_step(ctx, key, target, back=False):
    """Step to ``target`` (from `step_target()`)."""
    if key == MIDDLE and not EFFECTS_ONLY:
        ctx.white_active = target
        update_white_leds(ctx)
        load_selected_wave_sample(ctx)
        print("white LED {}{}".format(target, " (double tap: back one)" if back else ""))
        return
    page, spot = target
    if key == MIDDLE and ctx.active and PAGES == 1:
        print("one page of presets: add a fifth to get a second page")
        update_red_leds(ctx)
        update_white_leds(ctx)
        return
    if back:
        print("{} double tap: back {}".format("TOP" if key == TOP else "MIDDLE",
                                              "one" if key == TOP else "a page"))
    ctx.page = page
    select_preset(ctx, spot)


def finish_pending_step(ctx):
    """Take the step waiting after a TOP or MIDDLE release, if any."""
    if ctx.pending_step is not None:
        key, _, target = ctx.pending_step
        ctx.pending_step = None
        take_step(ctx, key, target)


def press_step_button(ctx, key, at):
    """Handle a TOP or MIDDLE press: the second press of a double tap goes
    back one at once; any other press waits for its release.

    :param ctx: The mutable state container.
    :param key: TOP or MIDDLE.
    :param at: When the button went down, from `event_ms()`.
    """
    if ctx.top_held and ctx.middle_held:
        # TOP + MIDDLE: the power-off combo steps neither.
        ctx.skip_release.update((TOP, MIDDLE))
        ctx.pending_step = None
        return
    pending = ctx.pending_step
    if pending is not None and pending[0] == key and ms_between(pending[1], at) <= BUTTON_GAP_SECONDS * 1000:
        ctx.pending_step = None
        ctx.skip_release.add(key)
        target = step_target(ctx, key, back=True)
        show_step(ctx, key, target)
        take_step(ctx, key, target, back=True)
        return
    finish_pending_step(ctx)
    if pressed_while_loading(ctx, at):
        print("{} pressed while a preset loaded: ignored".format("TOP" if key == TOP else "MIDDLE"))
        ctx.skip_release.add(key)
    else:
        ctx.skip_release.discard(key)


def release_step_button(ctx, key, at):
    """Handle a TOP or MIDDLE release: light the next spot's LEDs, and leave
    the step to `finish_pending_step()` once BUTTON_GAP_SECONDS pass with no
    second press.

    :param ctx: The mutable state container.
    :param key: TOP or MIDDLE.
    :param at: When the button came up, from `event_ms()`.
    """
    if key in ctx.skip_release:
        ctx.skip_release.discard(key)
        return
    target = step_target(ctx, key)
    show_step(ctx, key, target)
    ctx.pending_step = [key, at, target]


def play_current_wave_sample(ctx, at=0):
    """Handle a BOTTOM button press: play the sample through once, at once
    (a press while it plays starts it again). `release_current_wave()` stops
    it if the press turns out to be a hold. A press soon after a tap plays it
    backwards (a double tap).

    :param ctx: The mutable state container.
    :param at: When the button went down, from `event_ms()`.
    """
    if ctx.current_source is None:
        print("no wave loaded for LED {}".format(ctx.white_active))
        return

    double = ctx.tap_at is not None and ms_between(ctx.tap_at, at) <= DOUBLE_TAP_SECONDS * 1000
    ctx.press_at, ctx.press_double, ctx.tap_at = at, double, None
    start_sample(ctx, reverse=double)
    ctx.wave_playing = True
    print("playing {}{}".format(ctx.white_active, " (double tap: backwards)" if double else ""))


def release_current_wave(ctx, at=0):
    """Handle a BOTTOM button release: a hold stops the sample, a tap lets it
    play on.

    :param ctx: The mutable state container.
    :param at: When the button came up, from `event_ms()`.
    """
    if not ctx.wave_playing:
        return
    held = ms_between(ctx.press_at, at)
    if held >= TAP_HOLD_SECONDS * 1000:
        stop_current_wave(ctx)
        print("stopped {} (held {} ms)".format(ctx.white_active, held))
    elif not ctx.press_double:
        # A tap: it plays on, and a second press soon is a double tap.
        ctx.tap_at = ctx.press_at


# The handle travel pot, offered to the presets as the block named
# "$handle".
handle_pot = analogio.AnalogIn(board.HANDLE_POSITION)
handle_control = synthio.Math(synthio.MathOperation.SUM, 0.0, 0.0, 0.0)


# The volume knob is a plain potentiometer across 3V3 with its wiper on GP29.
knob = analogio.AnalogIn(board.VOLUME)


def apply_volume(ctx, force=False):
    """Set the DAC digital volume from the knob, if the knob has moved.

    The knob's travel maps linearly onto `VOLUME_MIN_DB` .. `VOLUME_MAX_DB`,
    except for `VOLUME_OFF_FRACTION` at the counterclockwise end, which mutes.

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
    ctx.used_at = time.monotonic()

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


def power_off(ctx):
    """Drive POWER_HOLD low. On batteries the mic is off before this returns.

    Over USB it stays powered and code.py carries on, so the pin is claimed
    only once (claiming it again raised "in use" and stopped code.py).
    """
    if ctx.power_hold is None:
        ctx.power_hold = digitalio.DigitalInOut(board.POWER_HOLD)
        ctx.power_hold.direction = digitalio.Direction.OUTPUT
    ctx.power_hold.value = False
    print("still on: USB is powering the mic. Unplug USB and it turns off.")


def in_use(ctx, held):
    """Whether the mic is in use this pass, for the auto-off: squeezed, a
    take recording, or a sample playing. (Presses and the knob count where
    they are read.)"""
    if held or (ctx.recorder is not None and ctx.recorder.recording):
        return True
    if ctx.tune is not None and ctx.tune.sample is not None:
        return True
    stream = ctx.backwards_stream
    if stream is not None and stream.playing:
        return True
    return bool(sample_voice(ctx).playing)


def check_auto_off(ctx, now, using):
    """Power off once the mic has gone AUTO_OFF_SECONDS unused, blinking
    every LED for the last AUTO_OFF_WARN_SECONDS.

    Plugged into a computer it never does, and the wait starts over once it
    is unplugged. If it is still running after powering off (a USB charger
    keeps it on), the next use takes POWER_HOLD high again, as CircuitPython
    set it at boot.
    """
    if AUTO_OFF_SECONDS is None:
        return
    if using or (supervisor is not None and supervisor.runtime.usb_connected):
        ctx.used_at = now
    idle = now - ctx.used_at
    if idle < AUTO_OFF_SECONDS - AUTO_OFF_WARN_SECONDS:
        if ctx.auto_off_warning:
            ctx.auto_off_warning = False
            update_red_leds(ctx)
            update_white_leds(ctx)
            print("auto-off: still in use, staying on")
        if ctx.auto_off_done:
            ctx.auto_off_done = False
            ctx.power_hold.value = True
        return
    if ctx.auto_off_done:
        return
    if idle >= AUTO_OFF_SECONDS:
        print("auto-off: unused for {} min, shutting down".format(AUTO_OFF_MINUTES))
        ctx.auto_off_done = True
        ctx.auto_off_warning = False
        update_red_leds(ctx)
        update_white_leds(ctx)
        power_off(ctx)
        return
    if not ctx.auto_off_warning:
        ctx.auto_off_warning = True
        print("auto-off in {:.0f} s: press a button or squeeze to stay on".format(
            AUTO_OFF_SECONDS - idle))
    # Written every pass: record mode sets the red LEDs every pass too.
    lit = int(idle / AUTO_OFF_BLINK_SECONDS) % 2 == 0
    for led in red_leds + white_leds:
        led.value = lit


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
if shake_meter is not None:
    # The shake Echo is built the first time a sample plays on the native
    # mixer (see shake_echo()), so until then the shake holds no RAM: free
    # memory at boot, and so how often gc runs, stays as it was before the shake (pre-launch 1.7).
    data_context.shake_roll = shake.Roll(SHAKE, autotune.BLOCK_SIZE / RATE)
    print("shake: {} found; shaking a sample plays it {}".format(
        shake_meter.name, shake.describe(SHAKE)))

# Initialize the first LED and load its wave file.
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
print("auto-off: after {} min unused on batteries".format(AUTO_OFF_MINUTES)
      if AUTO_OFF_SECONDS else "auto-off: never")
print("TOP / MIDDLE: step on letting go; pressed again within {:.0f} ms = back one".format(
    BUTTON_GAP_SECONDS * 1000))
if not EFFECTS_ONLY:
    print("BOTTOM: held {:.0f} ms+ = hold, two presses within {:.0f} ms = backwards".format(
        TAP_HOLD_SECONDS * 1000, DOUBLE_TAP_SECONDS * 1000))

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
            data_context.used_at = time.monotonic()
            if event.key_number == TOP:
                data_context.top_held = event.pressed
            if event.key_number == MIDDLE:
                data_context.middle_held = event.pressed
            if event.key_number in (TOP, MIDDLE):
                if event.pressed:
                    press_step_button(data_context, event.key_number, event_ms(event))
                else:
                    release_step_button(data_context, event.key_number, event_ms(event))
            elif event.pressed:
                # BOTTOM acts on the preset or sample a step just picked.
                finish_pending_step(data_context)
            if event.pressed and event.key_number == BOTTOM and EFFECTS_ONLY:
                # The way back to clean, and the only one.
                if data_context.active:
                    select_preset(data_context, 0)
            elif event.pressed and event.key_number == BOTTOM:
                play_current_wave_sample(data_context, event_ms(event))
            if event.released and event.key_number == BOTTOM and not EFFECTS_ONLY:
                release_current_wave(data_context, event_ms(event))
            event = keys.events.get()
        # A TOP or MIDDLE release with no second press in time steps on.
        pending = data_context.pending_step
        if pending is not None and BUTTON_GAP_SECONDS * 1000 < ms_between(pending[1], now_ms()) < TICKS_MASK // 2:
            finish_pending_step(data_context)

        # TOP + MIDDLE held together for SHUTDOWN_HOLD_SECONDS powers off.
        if data_context.top_held and data_context.middle_held:
            if data_context.combo_since is None:
                data_context.combo_since = time.monotonic()
            elif time.monotonic() - data_context.combo_since >= SHUTDOWN_HOLD_SECONDS:
                print(
                    "TOP+MIDDLE held {}s: shutting down".format(SHUTDOWN_HOLD_SECONDS)
                )
                # Over USB the mic stays on, and the combo waits to be let go.
                power_off(data_context)
                data_context.combo_since = float("inf")
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
        # A vocoder's "squeeze_chord" switches chord the same way.
        vocoder = data_context.tune.vocoder if data_context.tune is not None else None
        if vocoder is not None and vocoder.squeezable:
            position = handle_control.a
            if (not vocoder.squeezed and position >= SQUEEZE_KEY_ON) or (
                vocoder.squeezed and position <= SQUEEZE_KEY_OFF
            ):
                vocoder.squeeze(not vocoder.squeezed)
                print("vocoder chord now {}".format(vocoder.chord or "one note"))

        # A long sample playing backwards streams from the drive: refill
        # its buffer, and stop it once it has played through.
        stream = data_context.backwards_stream
        if stream is not None and stream.playing and not stream.update(now):
            stop_current_wave(data_context)
            print("{} backwards: longest refill {:.1f} ms".format(
                data_context.current_path, stream.worst_ms))

        check_auto_off(data_context, now, in_use(data_context, held))

        # Shaking the mic while a sample plays. The accelerometer is awake
        # and read only then, and goes back to sleep after. Through autotune
        # the sample streams from the drive, so the shake rolls it there;
        # elsewhere it mixes in the shake Echo. (A tap leaves wave_playing
        # set after the sample ends; the Echo's own playing goes off then,
        # but a shake under way rings out.) With no sample playing this is
        # two attribute checks a pass.
        if shake_meter is not None:
            tune = data_context.tune
            shake_fx = data_context.shake_fx
            if THROUGH_PRESET and tune is not None:
                if tune.sample is not None:
                    data_context.shake_roll.update(tune.sample, shake_meter.update(now), now)
                elif shake_meter.awake:
                    shake_meter.reset()
                    data_context.shake_roll.reset()
            elif (shake_fx is not None and data_context.wave_playing
                  and (shake_fx.echo.playing or shake_fx.on)):
                shake_fx.set_amount(shake_meter.update(now))
            elif shake_meter.awake:
                shake_meter.reset()
                if shake_fx is not None:
                    shake_fx.set_amount(0.0)

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
                tune = data_context.tune
                # The pass before: its work, its wait for the chain, the
                # time it had spare in mic.record, and the loop's time
                # outside autotune before it. With a sample playing through
                # it, the part of the work that read and mixed the sample.
                sample = ", {:.1f} of it the sample".format(tune.sample_ms) if tune.sample_ms else ""
                print("overflow (autotune took {:.1f} ms of 16{}, plus {:.1f} waiting for the chain; "
                      "{:.1f} spare, {:.1f} outside)".format(
                          tune.busy_ms, sample, tune.wait_ms, tune.idle_ms, tune.outside_ms))
            else:
                print("overflow")

        # Typing f and Enter in the serial console flips the autotune feed's
        # double buffer live (see autotune.FEED_PHASE), to test a crackle.
        if supervisor is not None and supervisor.runtime.serial_bytes_available:
            typed = sys.stdin.read(supervisor.runtime.serial_bytes_available)
            if "f" in typed.lower() and autotune.FEED_PHASE is not None:
                autotune.FEED_PHASE ^= 1
                print("feed_phase now {}".format(autotune.FEED_PHASE))

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
