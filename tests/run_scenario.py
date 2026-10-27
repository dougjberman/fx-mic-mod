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
    # The same double tap on an autotune preset with samples through it.
    "auto_through": dict(
        presses=tap(0.5, TOP) + tap(1.0, BOTTOM) + tap(1.2, BOTTOM),
        paddle=[],
        end=2.0,
    ),
    # effects_only, two pages: TOP x2 -> page 1 preset 2; MIDDLE -> page 2
    # preset 2; TOP -> clean (page 2 has two presets, and no record mode);
    # MIDDLE -> page 1 clean; TOP x3 -> preset 3; MIDDLE -> page 2, which
    # stops at preset 2; MIDDLE -> page 1 again. BOTTOM plays nothing.
    "pages": dict(
        presses=tap(0.5, TOP) + tap(1.0, TOP) + tap(1.5, MIDDLE) + tap(2.0, TOP)
        + tap(2.5, MIDDLE) + tap(3.0, TOP) + tap(3.2, TOP) + tap(3.4, TOP)
        + tap(4.0, MIDDLE) + tap(4.5, BOTTOM) + tap(5.0, MIDDLE),
        paddle=[],
        end=5.5,
    ),
    "through": dict(
        presses=tap(0.5, TOP) + tap(1.0, BOTTOM) + tap(3.0, TOP) + tap(3.5, BOTTOM),
        paddle=[],
        end=4.0,
    ),
}


def main():
    name, root = sys.argv[1], sys.argv[2]
    script = fakes.Script(**SCENARIOS[name])
    fakes.install(script, root)
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
            {"hz": [n.frequency for n in v.pressed],
             "peaks": [max(n.amplitude_log) if n.amplitude_log else None for n in v.pressed],
             "filters": [n.filter.kwargs.get("frequency") for n in v.pressed],
             "table": len(v.pressed[0].waveform) if v.pressed else None,
             "rate": v.kwargs.get("sample_rate")}
            for v in script.synths
        ],
        "plays": script.plays,
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
