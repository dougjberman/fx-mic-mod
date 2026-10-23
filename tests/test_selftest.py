"""Tests for the hardware test harness: tools/ting_test.py, tools/ting_analysis.py
and firmware/selftest.py."""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

import ting_analysis as A  # noqa: E402
import ting_test  # noqa: E402


def chord_take(intervals_by_lead, notes=A.SINE_NOTES):
    """What the mic would hear: each lead note with its harmony notes."""
    t = np.arange(int(0.8 * A.RATE)) / A.RATE
    parts = [np.zeros(int(A.LEAD_IN * A.RATE))]
    for midi, _ in notes:
        x = np.sin(2 * np.pi * A.midi_hz(midi) * t)
        for interval in intervals_by_lead[midi]:
            x += 0.7 * np.sin(2 * np.pi * A.midi_hz(midi + interval) * t)
        parts += [x / 4, np.zeros(int(A.GAP_SECONDS * A.RATE))]
    return (np.concatenate(parts) * 0.5 * 32767).astype(np.int16)


class AnalysisTests(unittest.TestCase):
    def test_tuning_tells_off_pitch_from_tuned(self):
        off = A.tuning(A.signals()["notes_off.wav"])
        tuned = A.tuning(A.tone([(m, 0) for m, _ in A.OFF_NOTES], partials=(1, 0.5, 0.3)))
        self.assertGreater(off["median_cents"], 20)
        self.assertLess(tuned["median_cents"], 5)

    def test_tuning_flags_notes_outside_the_scale(self):
        # F#4 is in the test notes; in C major it should not survive
        t = A.tuning(A.tone([(m, 0) for m, _ in A.OFF_NOTES]), "C", "major")
        self.assertGreater(t["out_of_scale"], 0.1)

    def test_harmony_passes_the_right_chord_and_fails_a_wrong_one(self):
        for spec in ({"chord": "major"}, {"chord": "maj7"}, {"chord": "sus2", "inversion": 2},
                     {"chord": "major", "octave": -1}):
            want = A.expected_harmony(spec, A.SINE_NOTES)
            self.assertTrue(A.harmony(chord_take(want), want)[0], spec)
            wrong = {lead: [i + 1 for i in v] for lead, v in want.items()}
            self.assertFalse(A.harmony(chord_take(wrong), want)[0], spec)

    def test_key_chord_expects_each_notes_own_triad(self):
        want = A.expected_harmony({"chord": "key_triad"}, A.SINE_NOTES, "C", "major")
        self.assertEqual(want[64], [3, 7])   # E minor on E
        self.assertEqual(want[65], [4, 7])   # F major on F
        self.assertTrue(A.harmony(chord_take(want), want)[0])
        plain = {lead: [4, 7] for lead in want}
        self.assertFalse(A.harmony(chord_take(plain), want)[0])

    def test_shift_measures_semitones(self):
        take = A.tone([(m + 7, c) for m, c in A.SINE_NOTES])
        self.assertGreater(A.shifted(take, A.SINE_NOTES, 7)[0], 0.9)
        self.assertLess(A.shifted(take, A.SINE_NOTES, 1)[0], 0.1)


class ChunkyLink:
    """Hands the raw REPL its replies a few bytes at a time."""

    def __init__(self, replies):
        self.data = b"".join(replies)

    def write(self, data):
        pass

    def read(self, size):
        out, self.data = self.data[:7], self.data[7:]
        return out

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


class ReplTests(unittest.TestCase):
    def test_output_split_across_reads(self):
        reply = b"OK@@{\"t\": \"x\"}\r\nplain line\r\nlast\x04\x04>"
        repl = ting_test.RawRepl(None, timeout=1, link=ChunkyLink([reply, reply]))
        lines = []
        out = repl.exec("print(1)", on_line=lines.append)
        self.assertEqual(lines, ['@@{"t": "x"}', "plain line", "last"])
        self.assertIn("plain line", out)
        # the second reply was not eaten by the first
        self.assertIn("last", repl.exec("print(2)"))

    def test_device_error_raises(self):
        reply = b"OK\x04Traceback...\r\nValueError: nope\x04>"
        repl = ting_test.RawRepl(None, timeout=1, link=ChunkyLink([reply]))
        with self.assertRaises(ting_test.ReplError):
            repl.exec("boom")


class SuiteTests(unittest.TestCase):
    def test_check_runs_through_on_the_fakes(self):
        root = tempfile.mkdtemp()
        results = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        self.addCleanup(shutil.rmtree, results)
        os.mkdir(os.path.join(root, "selftest"))
        shutil.copy(os.path.join(REPO, "firmware", "config.json"), root)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_selftest.py"), root, results],
            capture_output=True, text=True, timeout=300)
        self.assertIn("STATUS", proc.stdout, proc.stdout + proc.stderr)
        with open(glob.glob(os.path.join(results, "*", "report.md"))[0]) as f:
            report = f.read()
        self.assertNotIn("Suite stopped", report)
        # test files went onto the drive over the REPL
        self.assertEqual(sorted(os.listdir(os.path.join(root, "selftest"))),
                         ["notes_off.wav", "notes_sine.wav", "silence.wav"])
        self.assertTrue(os.path.exists(os.path.join(root, "selftest.py")))
        # every preset in the starter pack builds and gives its memory back
        for name in ("Snapped and Crunched", "Squeeze for backups", "Chipmunks in space", "Squeeze monster"):
            self.assertRegex(report, r"\| 3\.8 \| Preset \d %s builds \| PASS" % name)
        self.assertIn("| 4.1 | Pitch detector finds the test notes | PASS", report)
        # the fake mic hears a steady tone, not its speaker
        self.assertIn("| 3.4 | Speaker to mic loopback, clean | FAIL", report)
        for row in ("4.5 | Harmonizer maj7 with autotune: CPU | PASS", "5.1 | Preset 4 x2 | PASS"):
            self.assertIn(row, report)


if __name__ == "__main__":
    unittest.main()
