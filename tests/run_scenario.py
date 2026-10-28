"""Run firmware/code.py once against the fakes and print a JSON summary.

    python3 tests/run_scenario.py <scenario> <root dir>
"""

import json
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# FX_FIRMWARE runs another copy, such as the slimmed one the site installs.
FIRMWARE = os.environ.get("FX_FIRMWARE") or os.path.join(os.path.dirname(HERE), "firmware")
sys.path.insert(0, HERE)
sys.path.insert(0, FIRMWARE)

import fakes  # noqa: E402

TOP, MIDDLE, BOTTOM = 0, 1, 2


def tap(t, key):
    return [(t, key, True), (t + 0.05, key, False)]


SCENARIOS = {
    # TOP + MIDDLE held 2 s on USB power: the mic can't turn off, and code.py
    # must keep running rather than stop on claiming POWER_HOLD twice.
    "shutdown_on_usb": dict(
        presses=[(0.5, TOP, True), (0.5, MIDDLE, True), (2.5, TOP, False), (2.5, MIDDLE, False)],
        end=4.0,
    ),
    # TOP x1 -> preset 1 (autotune + effects), sing a flat note with the paddle
    # held; TOP x2 more -> record mode; 1 s take into slot 1; then a held
    # take that runs out of time into slot 2.
    "record": dict(
        presses=tap(0.5, TOP) + tap(3.0, TOP) + tap(3.5, TOP)
        + tap(6.0, MIDDLE) + tap(12.0, BOTTOM),
        paddle=[(1.0, 2.5), (4.0, 5.0), (7.0, 12.0)],
        end=13.0,
    ),
    # Same, but booted in edit mode: record mode must not record or write.
    "readonly": dict(
        presses=tap(0.5, TOP) + tap(1.0, TOP) + tap(1.5, TOP),
        paddle=[(2.0, 3.0)],
        end=4.0,
        readonly=True,
    ),
    # sample_fx "preset": TOP -> autotune preset, grey plays 1.wav with the
    # paddle released (so only the sample reaches autotune); TOP -> echo
    # preset, grey plays it into the chain's input mixer.
    # TOP -> preset 1, squeezed and still held at the end: the output mixer
    # sits at the preset's "level".
    "level": dict(
        presses=tap(0.5, TOP),
        paddle=[(1.0, 3.0)],
        end=2.0,
    ),
    # The sample button on the clean spot: a tap, a double tap, a hold, then
    # two presses too far apart to be a double tap.
    "auto": dict(
        presses=tap(1.0, BOTTOM) + tap(2.0, BOTTOM) + tap(2.2, BOTTOM)
        + [(3.0, BOTTOM, True), (3.6, BOTTOM, False)]
        + tap(5.0, BOTTOM) + tap(5.5, BOTTOM),
        paddle=[],
        end=6.0,
    ),
    # A double tap, then TOP to preset 1 while it plays backwards, then
    # back to clean and another double tap.
    "auto_preset": dict(
        presses=tap(1.0, BOTTOM) + tap(1.2, BOTTOM) + tap(1.5, TOP)
        + tap(2.5, TOP) + tap(2.8, TOP) + tap(3.5, BOTTOM) + tap(3.7, BOTTOM),
        paddle=[],
        end=4.5,
    ),
    # The same double tap on an autotune preset with samples through it.
    "auto_through": dict(
        presses=tap(0.5, TOP) + tap(1.0, BOTTOM) + tap(1.2, BOTTOM),
        paddle=[],
        end=2.0,
    ),
    # effects_only, two pages: TOP -> page 1 preset 1; TOP -> 2; MIDDLE ->
    # page 2 preset 2; TOP -> page 2 preset 1 (round the page, no clean);
    # BOTTOM -> clean; MIDDLE -> page 1 preset 1; TOP x2 -> preset 3;
    # MIDDLE -> page 2, which stops at preset 2; MIDDLE -> page 1 again;
    # BOTTOM -> clean; TOP -> page 1 preset 1.
    "pages": dict(
        presses=tap(0.5, TOP) + tap(1.0, TOP) + tap(1.5, MIDDLE) + tap(2.0, TOP)
        + tap(2.5, BOTTOM) + tap(3.0, MIDDLE) + tap(3.2, TOP) + tap(3.4, TOP)
        + tap(4.0, MIDDLE) + tap(4.5, MIDDLE) + tap(5.0, BOTTOM) + tap(5.2, BOTTOM)
        + tap(5.4, TOP),
        paddle=[],
        end=5.8,
    ),
    # The clean spot: hold the sample button down, shake the mic for 0.6 s
    # while it plays, hold still, then let go (which stops the sample).
    "shake": dict(
        presses=[(0.5, BOTTOM, True), (2.1, BOTTOM, False)],
        paddle=[],
        shakes=[(1.0, 1.6)],
        end=2.5,
    ),
    # The same shake on an autotune preset that the sample plays through.
    "shake_through": dict(
        presses=tap(0.5, TOP) + tap(1.0, BOTTOM),
        paddle=[],
        shakes=[(1.3, 2.0)],
        end=2.8,
    ),
    # Singing through an autotune preset and a native one, shaking, with no
    # sample played at all.
    "shake_idle": dict(
        presses=tap(0.5, TOP) + tap(1.5, TOP),
        paddle=[(0.8, 2.4)],
        shakes=[(1.0, 2.2)],
        end=2.5,
    ),
    # A tap and a shake on a board where nothing answers at 0x18.
    "shake_no_chip": dict(
        presses=tap(0.5, BOTTOM),
        paddle=[],
        end=1.5,
    ),
    "through": dict(
        presses=tap(0.5, TOP) + tap(1.0, BOTTOM) + tap(3.0, TOP) + tap(3.5, BOTTOM),
        paddle=[],
        end=4.0,
    ),
}


def track_vocoders():
    """Every autotune.Vocoder made from here on, each logging its band
    levels on each update."""
    import autotune
    made = []
    init, update = autotune.Vocoder.__init__, autotune.Vocoder.update

    def tracked_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.level_log = []
        made.append(self)

    def tracked_update(self, *args):
        update(self, *args)
        self.level_log.append(list(self.levels))

    autotune.Vocoder.__init__, autotune.Vocoder.update = tracked_init, tracked_update
    # FAKE_VOCODER_VOICE=1 keeps the voice beside a vocoder, as VOCODER_VOICE does
    autotune.VOCODER_VOICE = os.environ.get("FAKE_VOCODER_VOICE") == "1"
    return made


def squeeze_reversed():
    """FAKE_REVERSE_FAILS=n: the first n backwards copies run out of RAM, as
    on a fragmented heap."""
    import autotune
    fails = [int(os.environ.get("FAKE_REVERSE_FAILS", "0"))]
    load = autotune.load_reversed

    def squeezed(path):
        if fails[0]:
            fails[0] -= 1
            raise MemoryError("memory allocation failed, allocating 144346 bytes")
        return load(path)

    autotune.load_reversed = squeezed


def track_rolls():
    """Every WaveReader.roll() call, as its block count."""
    import autotune
    rolls = []
    roll = autotune.WaveReader.roll

    def tracked(self, blocks):
        rolls.append(blocks)
        roll(self, blocks)

    autotune.WaveReader.roll = tracked
    return rolls


def main():
    name, root = sys.argv[1], sys.argv[2]
    script = fakes.Script(**SCENARIOS[name])
    fakes.install(script, root)
    vocoders = track_vocoders()
    rolls = track_rolls()
    squeeze_reversed()
    env = runpy.run_path(os.path.join(FIRMWARE, "code.py"), run_name="__main__")
    ctx = env["data_context"]
    summary = {
        "active": ctx.active,
        "record_spot": env["RECORD_SPOT"],
        "files": sorted(os.listdir(root)),
        "shifters": [
            {"log_len": len(s.semitone_log),
             "min": min(s.semitone_log) if s.semitone_log else None,
             "max": max(s.semitone_log) if s.semitone_log else None,
             "kwargs": sorted(k for k in s.kwargs),
             "grain_size": s.kwargs.get("grain_size"),
             "density": s.kwargs.get("density"),
             "sample_rate": s.kwargs.get("sample_rate")}
            for s in script.shifters
        ],
        "mic_reads": script.mic_reads,
        # mixers after the output one are preset input mixers
        "heads": [
            {"mic_level": m.voice[0].level,
             "sample": type(m.voice[1].playing).__name__,
             "sample_level": m.voice[1].level}
            for m in script.mixers[1:]
            if type(m.voice[0].playing).__name__ != "GranularPitchShift"
        ],
        "out_level": env["mixer"].voice[0].level,
        # the harmonizer's mixer: the one whose voices play pitch shifters
        "tune_mixer": next((
            {"playing": [type(v.playing).__name__ for v in m.voice],
             # voice 0's level is the render counter LFO; its offset is the level
             "levels": [getattr(v.level, "offset", v.level) for v in m.voice]}
            for m in script.mixers
            if type(m.voice[0].playing).__name__ == "GranularPitchShift"), None),
        # the vocoder: carrier pitch, band levels, and what plays it
        "vocoders": [
            {"hz": [n.frequency for n in v.synth.pressed if n is not v.noise],
             "peaks": [max(levels) for levels in zip(*v.level_log)] if v.level_log else None,
             "bands": len(v.levels),
             "chord": v.chord,
             "table": len(v.notes[0].waveform),
             "rate": v.synth.kwargs.get("sample_rate")}
            for v in vocoders
        ],
        "plays": script.plays,
        "looped": script.looped,
        # the shake Echo: the one whose delay is an LFO
        "shake_echo": next((
            {"freq_shift": e.kwargs.get("freq_shift"),
             "max_delay_ms": e.kwargs.get("max_delay_ms"),
             "mix_max": max(e.mix_log) if e.mix_log else None,
             "mix_last": e.mix_log[-1] if e.mix_log else None,
             "clears": e.clears,
             "retriggered": getattr(e.delay_ms, "retriggered", 0),
             "playing": type(e.playing).__name__}
            for e in script.echoes
            if type(e.kwargs.get("delay_ms")).__name__ == "LFO"), None),
        "rolls": rolls,
        "accel_power": script.accel_power,
        # the vocoder's mixer: the voice on voice 0, the synth on voice 1
        "vocoder_mixer": next((
            {"playing": [type(v.playing).__name__ for v in m.voice],
             "levels": [v.level for v in m.voice]}
            for m in script.mixers
            if len(m.voice) > 1 and type(m.voice[1].playing).__name__ == "Synthesizer"), None),
        "voice0": type(env["mixer"].voice[0].playing).__name__,
        "voice1": type(env["mixer"].voice[1].playing).__name__,
        "red_counts": [
            [round(t, 3), sum(v for k, v in leds.items() if k.startswith("LED_RED"))]
            for t, leds in script.led_log
        ],
        "white_log": [
            [round(t, 3), [k for k, v in sorted(leds.items()) if v and k.startswith("LED_WHITE")]]
            for t, leds in script.led_log
        ],
        "lit_log": [
            [round(t, 3), [k for k, v in sorted(leds.items()) if v and k.startswith("LED_")]]
            for t, leds in script.led_log
        ],
    }
    print("SUMMARY " + json.dumps(summary))


main()
