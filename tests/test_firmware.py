"""Desktop tests for the fork. Run: python3 -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import math
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.join(os.path.dirname(HERE), "firmware")

TEST_CONFIG = {
    "name": "TEST",
    "presets": [
        {"name": "T-Pain", "list": [
            {"effect": "distortion", "drive": 0.3, "mode": "overdrive", "mix": 0.5},
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0,
             "squeeze_key": "C", "squeeze_scale": "major"},
            {"effect": "reverb", "roomsize": 0.6, "mix": 0.3}]},
        {"name": "Echo", "list": [
            {"effect": "echo", "max_delay_ms": 500, "delay_ms": 250, "decay": 0.5, "mix": 0.3}]},
    ],
    "samples": [{"file": "1.wav", "effects": [{"effect": "freeverb", "mix": 0.4}]}],
}


THROUGH_CONFIG = {
    "name": "THRU",
    "sample_fx": "preset",
    "presets": [
        {"list": [{"effect": "autotune", "key": "A", "scale": "minor"},
                  {"effect": "reverb", "mix": 0.3}]},
        {"list": [{"effect": "echo", "max_delay_ms": 500, "delay_ms": 250, "mix": 0.3}]},
    ],
    "samples": [{"file": "1.wav", "effects": [{"effect": "freeverb", "mix": 0.4}]}],
}


def write_tone(path, hz, seconds, rate=24000):
    import math
    import struct
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                      for i in range(int(seconds * rate)))
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def run(scenario, config=TEST_CONFIG, tone=None, handle=None):
    root = tempfile.mkdtemp()
    with open(os.path.join(root, "config.json"), "w") as file:
        json.dump(config, file)
    if tone:
        write_tone(os.path.join(root, "1.wav"), *tone)
    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "run_scenario.py"), scenario, root],
        capture_output=True, text=True, timeout=600, check=True,
        env=dict(os.environ, FAKE_HANDLE="%d,%d" % handle) if handle else None,
    ).stdout
    summary = json.loads(out.split("SUMMARY ", 1)[1])
    return root, out, summary


def transitions(log):
    """Changes in an LED log, keeping only the last state at each instant
    (the LEDs are set one pin at a time)."""
    settled = {}
    for t, value in log:
        settled[t] = value
    out, prev = [], None
    for t, value in sorted(settled.items()):
        if value != prev:
            out.append((t, value))
            prev = value
    return out


class LoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERE)
        sys.path.insert(0, FIRMWARE)
        import fakes
        fakes.install(fakes.Script(), tempfile.mkdtemp())
        import config_loader
        cls.cl = config_loader

    def test_adafruit_demo_pack_still_loads(self):
        with open(os.path.join(os.path.dirname(HERE), "examples", "adafruit-demo.json")) as file:
            config = json.load(file)
        for preset in config["presets"]:
            tune, effects = self.cl.create_preset(preset, blocks={"handle": 0.0})
            self.assertIsNone(tune)
            self.assertEqual(len(effects), len(preset["list"]))

    def test_starter_pack_loads(self):
        with open(os.path.join(FIRMWARE, "config.json")) as file:
            config = json.load(file)
        tunes = [self.cl.create_preset(p, blocks={"handle": 0.0})[0] for p in config["presets"]]
        self.assertEqual([t is not None for t in tunes], [True, True, False, False])
        self.assertEqual(len(self.cl.load_samples(config)), 4)

    def test_autotune_is_split_out_wherever_it_sits(self):
        tune, effects = self.cl.create_preset(TEST_CONFIG["presets"][0], blocks={"handle": 0.5})
        self.assertEqual(tune, {"key": "A", "scale": "minor", "speed": 1.0,
                                "squeeze_key": "C", "squeeze_scale": "major"})
        self.assertEqual(len(effects), 2)

    def test_disabled_autotune_is_skipped(self):
        tune, effects = self.cl.create_preset(
            {"list": [{"effect": "autotune", "enabled": False}, {"effect": "echo"}]})
        self.assertIsNone(tune)
        self.assertEqual(len(effects), 1)

    def test_two_autotunes_rejected(self):
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "autotune"}, {"effect": "AutoTune"}]})

    def test_unknown_autotune_param_rejected(self):
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "autotune", "pitch": 3}]})

    def test_squeeze_key_must_be_text(self):
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "autotune", "squeeze_key": 3}]})

    def test_handle_drives_key_or_setting_not_both(self):
        both = {"list": [{"effect": "autotune", "speed": "$handle", "squeeze_scale": "minor"}]}
        with self.assertRaises(ValueError):
            self.cl.create_preset(both, blocks={"handle": 0.5})
        lerp = {"block": "math", "operation": "constrained_lerp", "a": 0, "b": 1, "c": "$handle"}
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "autotune", "humanize": lerp, "squeeze_key": "D"}]},
                                  blocks={"handle": 0.5})
        named = {"blocks": {"b": lerp, "c": {"block": "math", "operation": "sum", "a": "$b"}},
                 "list": [{"effect": "autotune", "speed": "$c", "squeeze_key": "D"}]}
        with self.assertRaises(ValueError):
            self.cl.create_preset(named, blocks={"handle": 0.5})

    def test_sample_fx_setting(self):
        self.assertEqual(self.cl.sample_routing({}), "own")
        self.assertEqual(self.cl.sample_routing({"sample_fx": "Preset"}), "preset")
        with self.assertRaises(ValueError):
            self.cl.sample_routing({"sample_fx": "chain"})

    def test_preset_level(self):
        self.assertEqual(self.cl.preset_level({"list": []}), 1.0)
        self.assertEqual(self.cl.preset_level({"level": 0.5, "list": []}), 0.5)
        for bad in (1.5, -0.1, "loud", True):
            with self.assertRaises(ValueError):
                self.cl.preset_level({"level": bad, "list": []})

    def test_harmonizer_joins_the_autotune_stage(self):
        tune, effects = self.cl.create_preset({"list": [
            {"effect": "autotune", "key": "C", "scale": "major"},
            {"effect": "echo"},
            {"effect": "harmonizer", "chord": "maj7", "inversion": 1, "octave": -1, "level": 0.5}]})
        self.assertEqual(tune, {"key": "C", "scale": "major", "harmony": "maj7",
                                "inversion": 1, "harmony_octave": -1, "harmony_level": 0.5})
        self.assertEqual(len(effects), 1)

    def test_harmonizer_without_autotune_leaves_the_voice_untuned(self):
        tune, effects = self.cl.create_preset({"list": [{"effect": "Harmonizer"}]})
        self.assertEqual(tune, {"correct": False, "harmony": "major"})
        self.assertEqual(effects, [])

    def test_harmonizer_checks(self):
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "harmonizer"}, {"effect": "harmony"}]})
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "harmonizer", "voices": 3}]})
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [
                {"effect": "autotune", "squeeze_scale": "minor"},
                {"effect": "harmonizer", "level": "$handle"}]}, blocks={"handle": 0.5})
        with self.assertRaises(ValueError):
            self.cl.create_effects({"list": [{"effect": "harmonizer"}]})

    def test_chord_voicings(self):
        import autotune
        v = autotune.voicing
        major = autotune.chord_by_name("major")[0]
        self.assertEqual(v(major), [4, 7])
        self.assertEqual(v(major, 1), [-8, -5])
        self.assertEqual(v(major, 2), [-5, 4])
        self.assertEqual(v(major, 0, -1), [-8, -5])
        self.assertEqual(v(autotune.chord_by_name("maj7")[0], 3), [-1, 4, 7])
        self.assertEqual(autotune.max_inversion(autotune.chord_by_name("power")[0]), 0)
        names = [name for name, _ in autotune.CHORDS]
        self.assertEqual(len(names), len(set(names)))
        for name, tones in autotune.CHORDS:
            self.assertLessEqual(len(tones), 4, name)
            self.assertEqual(tones[0], 0, name)
        with self.assertRaises(ValueError):
            autotune.chord_by_name("maj6")

    def test_key_chord_ignores_octave_errors(self):
        import autotune
        A = autotune.Autotune
        h = A.__new__(A)
        h.snapper = autotune.NoteSnapper()
        h.snapper.set_scale(autotune.scale_by_name("major"), 0)
        h._tones, h._inversion, h._octave = autotune.chord_by_name("key_triad")[0], 2, 0
        h._chord_note = h._chord_next = None
        h._chord_count = 0
        g3 = h._chord_intervals(55)
        # a G3 read an octave or two out for a moment keeps the same harmony
        self.assertEqual(h._chord_intervals(43), g3)
        self.assertEqual(h._chord_intervals(79), g3)
        # a new note still takes over the chord once it holds
        for _ in range(autotune.HARMONY_SETTLE - 1):
            self.assertEqual(h._chord_intervals(57), [i - 2 for i in g3])
        self.assertEqual(h._chord_intervals(57), h._key_intervals(57))
        self.assertEqual(h._chord_intervals(45), h._key_intervals(57))

    def test_feed_window_holds_the_last_two_blocks_in_order(self):
        # feed_phase null: after every write the buffer reads oldest block first.
        import array
        import autotune
        A, n = autotune.Autotune, autotune.BLOCK_SIZE
        h = A.__new__(A)
        h._out = array.array("h", [0] * (2 * n))
        view = memoryview(h._out)
        h._front, h._back, h._half = view[:n], view[n:], 0
        saved, autotune.FEED_PHASE = autotune.FEED_PHASE, None
        try:
            for k in range(1, 5):
                h._feed_block(array.array("h", [k] * n))
                self.assertEqual(list(h._out), [k - 1] * n + [k] * n)
        finally:
            autotune.FEED_PHASE = saved

    def test_autotune_not_allowed_in_samples(self):
        with self.assertRaises(ValueError):
            self.cl.create_effects({"list": [{"effect": "autotune"}]})


class SiteCopyTests(unittest.TestCase):
    SITE = os.path.join(os.path.dirname(HERE), "docs", "firmware")

    def test_site_serves_the_current_firmware(self):
        # docs/firmware/ is what the install page copies to the Ting: firmware/
        # without comments or docstrings.
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import slim_firmware
        names = sorted(n for n in os.listdir(self.SITE) if n != "__pycache__")
        self.assertEqual(names, slim_firmware.site_names())
        for name in names:
            with open(os.path.join(self.SITE, name), "rb") as file:
                self.assertEqual(file.read(), slim_firmware.site_copy(name),
                                 "docs/firmware/%s is stale: run tools/slim_firmware.py" % name)
            with open(os.path.join(FIRMWARE, name), "rb") as file:
                lines = file.read().count(b"\n")
            if name.endswith(".py"):
                # line for line, so a traceback from the mic matches firmware/
                self.assertEqual(slim_firmware.site_copy(name).count(b"\n"), lines, name)

    def test_site_copy_runs(self):
        os.environ.update(FX_FIRMWARE=self.SITE, PYTHONDONTWRITEBYTECODE="1")
        try:
            root, out, s = run("record")
        finally:
            del os.environ["FX_FIRMWARE"], os.environ["PYTHONDONTWRITEBYTECODE"]
        self.assertIn("autotune now C major", out)
        self.assertEqual(s["active"], s["record_spot"])
        with wave.open(os.path.join(root, "2.wav")) as take:
            self.assertAlmostEqual(take.getnframes() / 24000, 3.95, delta=0.03)


class DeviceScenarioTests(unittest.TestCase):
    def test_autotune_preset_then_record_two_takes(self):
        root, out, s = run("record")
        self.assertIn("autotune via GranularPitchShift", out)
        # "$handle" starts where the paddle click opens the mic, learned on each squeeze
        self.assertIn("handle opens the mic at", out)
        # squeezing the handle in switches A minor -> C major, releasing switches back
        self.assertIn("autotune now C major", out)
        self.assertLess(out.index("autotune now C major"), out.index("autotune now A minor"))
        # 207 Hz in A minor snaps down to G3 (196 Hz), about -0.95 semitones.
        shifter = s["shifters"][0]
        self.assertAlmostEqual(shifter["min"], -0.95, delta=0.05)
        self.assertEqual(s["active"], s["record_spot"])

    def test_handle_range_is_learned(self):
        # A Ting's handle pot sits ~1100 counts above an FX Mic's. Its full
        # squeeze must still read as fully in, so the squeeze key switches.
        root, out, s = run("record", handle=(33470, 30570))
        self.assertIn("raw 33470 out, 30570 in", out)
        self.assertIn("autotune now C major", out)

        with wave.open(os.path.join(root, "1.wav")) as take:
            self.assertEqual((take.getnchannels(), take.getsampwidth(), take.getframerate()),
                             (1, 2, 24000))
            # held 1.0 s, minus the 0.1 s paddle settle and 0.05 s trim
            self.assertAlmostEqual(take.getnframes() / 24000, 0.85, delta=0.03)
        with wave.open(os.path.join(root, "2.wav")) as take:
            # held 5 s, capped at 4 s, minus the trim
            self.assertAlmostEqual(take.getnframes() / 24000, 3.95, delta=0.03)
        self.assertFalse(any(name.endswith(".tmp") for name in s["files"]))

        # the red LEDs count the second take down 4 -> 3 -> 2 -> 1, a second each
        reds = [(t, c) for t, c in transitions(s["red_counts"]) if 7.0 < t < 11.2]
        counts = [c for _, c in reds]
        # before the take the reds flash 0/4; the countdown starts at the first 3
        reds = reds[counts.index(3):]
        self.assertEqual([c for _, c in reds[:3]], [3, 2, 1])
        for (t0, _), (t1, _) in zip(reds, reds[1:3]):
            self.assertAlmostEqual(t1 - t0, 1.0, delta=0.05)

        # while each take saves, one LED at a time runs top to bottom
        sweep = ["LED_RED1", "LED_RED2", "LED_RED3", "LED_RED4",
                 "LED_WHITE1", "LED_WHITE2", "LED_WHITE3", "LED_WHITE4"]
        lit = [tuple(names) for _, names in transitions(s["lit_log"])]
        singles = [names[0] for names in lit if len(names) == 1]
        text = " ".join(singles)
        self.assertEqual(text.count(" ".join(sweep)), 2, singles)

        # in record mode the red LEDs flash all together while no take runs,
        # and the white LEDs hold the slot count steady
        reds_idle = [c for t, c in transitions(s["red_counts"]) if 3.6 < t < 5.0]
        self.assertIn(0, reds_idle)
        self.assertIn(4, reds_idle)
        self.assertTrue(set(reds_idle) <= {0, 4}, reds_idle)
        whites = [w for t, w in transitions(s["white_log"]) if 3.6 < t < 5.0]
        self.assertNotIn([], whites)

    def test_samples_through_the_preset(self):
        # 340 Hz is 46 cents flat of F4, which is in A minor.
        root, out, s = run("through", THROUGH_CONFIG, tone=(340.0, 1.0))
        self.assertIn("0 effect(s)", out)  # the sample's own reverb is ignored
        shifter = s["shifters"][0]
        # with the paddle released the mic is muted, so only the sample was tuned
        self.assertEqual(shifter["min"], 0.0)
        self.assertAlmostEqual(shifter["max"], 0.46, delta=0.05)
        # clean and the echo preset take the sample on an input mixer, mic gated off
        self.assertEqual(s["heads"][-1], {"mic_level": 0.0, "sample": "WaveFile", "sample_level": 1.0})
        self.assertEqual(s["out_level"], 1.0)

    def test_a_sample_in_another_format_is_refused(self):
        # a 44.1 kHz file would stop code.py if it reached the mixer
        root, out, s = run("through", THROUGH_CONFIG, tone=(340.0, 1.0, 44100))
        self.assertIn("/1.wav not loaded: it is 44100 Hz, 1 ch, 16 bit", out)
        self.assertIn("no wave loaded for LED 1", out)

    def test_preset_level_sets_the_output(self):
        config = json.loads(json.dumps(TEST_CONFIG))
        config["presets"][0]["level"] = 0.5
        root, out, s = run("level", config)
        self.assertEqual(s["active"], 1)
        self.assertEqual(s["out_level"], 0.5)
        through = json.loads(json.dumps(THROUGH_CONFIG))
        through["presets"][1]["level"] = 0.6
        root, out, s = run("through", through, tone=(340.0, 1.0))
        self.assertEqual(s["out_level"], 0.6)

    def test_detector_at_every_rate(self):
        import autotune
        try:
            for rate in autotune.RATES:
                autotune.set_rate(rate)
                for hz in (82.4, 147.0, 262.0, 494.0):
                    buf = [int(6000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(autotune.BUFFER_SIZE)]
                    found = autotune.detect_pitch_autocorrelation(buf)
                    self.assertAlmostEqual(1200 * math.log2(found / hz), 0, delta=3, msg=(rate, hz))
            with self.assertRaises(ValueError):
                autotune.set_rate(44100)
        finally:
            autotune.set_rate(16000)

    def test_runs_at_32k(self):
        config = {"name": "H", "sample_rate": 32000, "presets": [{"list": [
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0, "grain_size": 512},
            {"effect": "harmonizer", "chord": "minor", "level": 0.8}]}], "samples": []}
        root, out, s = run("level", config)
        lead = s["shifters"][0]
        # 207 Hz still snaps down to G3, and the grains keep their length in time
        self.assertAlmostEqual(lead["min"], -0.95, delta=0.05)
        self.assertEqual((lead["sample_rate"], lead["grain_size"]), (32000, 1024))

    def test_harmonizer_voices_follow_the_tuned_note(self):
        config = {"name": "H", "presets": [{"list": [
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0},
            {"effect": "harmonizer", "chord": "minor", "level": 0.8},
            {"effect": "reverb", "mix": 0.3}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("harmonizer 2 voices", out)
        lead, third, fifth = s["shifters"]
        # 207 Hz snaps down to G3 (-0.95 st); the harmony sits a minor 3rd and a 5th above it
        self.assertAlmostEqual(lead["min"], -0.95, delta=0.05)
        self.assertAlmostEqual(third["min"], 3 - 0.95, delta=0.05)
        self.assertAlmostEqual(fifth["min"], 7 - 0.95, delta=0.05)
        mixer = s["tune_mixer"]
        self.assertEqual(mixer["playing"], ["GranularPitchShift"] * 3)
        self.assertAlmostEqual(mixer["levels"][1] / mixer["levels"][0], 0.8, delta=0.01)

    def test_key_harmonizer_without_autotune(self):
        config = {"name": "H", "presets": [{"list": [
            {"effect": "harmonizer", "chord": "key_triad"}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("voice via GranularPitchShift, harmonizer 2 voices", out)
        lead, third, fifth = s["shifters"]
        # the voice stays untuned; 207 Hz is nearest G in C major, so the harmony is B and D above
        self.assertEqual((lead["min"], lead["max"]), (0.0, 0.0))
        self.assertEqual(third["max"], 4.0)
        self.assertEqual(fifth["max"], 7.0)

    def test_shutdown_combo_over_usb_keeps_running(self):
        root, out, s = run("shutdown_on_usb")
        self.assertEqual(out.count("shutting down"), 1)
        self.assertIn("Unplug USB and it turns off", out)

    def test_edit_mode_never_writes(self):
        root, out, s = run("readonly")
        self.assertIn("recording is off", out)
        self.assertEqual(s["files"], ["config.json"])


if __name__ == "__main__":
    unittest.main()
