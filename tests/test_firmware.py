"""Desktop tests for the fork. Run: python3 -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.join(os.path.dirname(HERE), "firmware")

TEST_CONFIG = {
    "name": "TEST",
    "presets": [
        {"name": "T-Pain", "list": [
            {"effect": "distortion", "drive": 0.3, "mode": "overdrive", "mix": 0.5},
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": "$handle"},
            {"effect": "reverb", "roomsize": 0.6, "mix": 0.3}]},
        {"name": "Echo", "list": [
            {"effect": "echo", "max_delay_ms": 500, "delay_ms": 250, "decay": 0.5, "mix": 0.3}]},
    ],
    "samples": [{"file": "1.wav", "effects": [{"effect": "freeverb", "mix": 0.4}]}],
}


def run(scenario):
    root = tempfile.mkdtemp()
    with open(os.path.join(root, "config.json"), "w") as file:
        json.dump(TEST_CONFIG, file)
    out = subprocess.run(
        [sys.executable, os.path.join(HERE, "run_scenario.py"), scenario, root],
        capture_output=True, text=True, timeout=600, check=True,
    ).stdout
    summary = json.loads(out.split("SUMMARY ", 1)[1])
    return root, out, summary


def transitions(log):
    out, prev = [], None
    for t, value in log:
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
        self.assertEqual(tune, {"key": "A", "scale": "minor", "speed": 0.5})
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

    def test_autotune_not_allowed_in_samples(self):
        with self.assertRaises(ValueError):
            self.cl.create_effects({"list": [{"effect": "autotune"}]})


class DeviceScenarioTests(unittest.TestCase):
    def test_autotune_preset_then_record_two_takes(self):
        root, out, s = run("record")
        self.assertIn("autotune via GranularPitchShift", out)
        # 207 Hz in A minor snaps down to G3 (196 Hz), about -0.95 semitones.
        shifter = s["shifters"][0]
        self.assertAlmostEqual(shifter["min"], -0.95, delta=0.05)
        self.assertEqual(s["active"], s["record_spot"])

        with wave.open(os.path.join(root, "1.wav")) as take:
            self.assertEqual((take.getnchannels(), take.getsampwidth(), take.getframerate()),
                             (1, 2, 16000))
            # held 1.0 s, minus the 0.1 s paddle settle and 0.05 s trim
            self.assertAlmostEqual(take.getnframes() / 16000, 0.85, delta=0.03)
        with wave.open(os.path.join(root, "2.wav")) as take:
            # held 5 s, capped at 4 s, minus the trim
            self.assertAlmostEqual(take.getnframes() / 16000, 3.95, delta=0.03)
        self.assertFalse(any(name.endswith(".tmp") for name in s["files"]))

        # the red LEDs count the second take down 4 -> 3 -> 2 -> 1 -> 0, a second each
        reds = [(t, c) for t, c in transitions(s["red_counts"]) if 7.0 < t < 11.2]
        self.assertEqual([c for _, c in reds[:4]], [3, 2, 1, 0])
        for (t0, _), (t1, _) in zip(reds, reds[1:4]):
            self.assertAlmostEqual(t1 - t0, 1.0, delta=0.05)

        # the selected white LED blinks in record mode
        whites = [w for t, w in transitions(s["white_log"]) if 3.6 < t < 6.0]
        self.assertIn([], whites)
        self.assertIn(["LED_WHITE1"], whites)

    def test_edit_mode_never_writes(self):
        root, out, s = run("readonly")
        self.assertIn("recording is off", out)
        self.assertEqual(s["files"], ["config.json"])


if __name__ == "__main__":
    unittest.main()
