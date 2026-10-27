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

    def test_unknown_params_are_skipped(self):
        tune, effects = self.cl.create_preset({"list": [
            {"effect": "autotune", "pitch": 3},
            {"effect": "harmonizer", "chord": "major", "sparkle": 1},
            {"effect": "echo", "sparkle": 1}]})
        self.assertNotIn("pitch", tune)
        self.assertNotIn("sparkle", tune)
        self.assertEqual(len(effects), 1)

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
        tune, effects = self.cl.create_preset({"list": [{"effect": "harmonizer", "chord": "major", "double": "above"}]})
        self.assertEqual(tune["harmony_double"], "above")

    def test_harmonizer_without_autotune_leaves_the_voice_untuned(self):
        tune, effects = self.cl.create_preset({"list": [{"effect": "Harmonizer"}]})
        self.assertEqual(tune, {"correct": False, "harmony": "major"})
        self.assertEqual(effects, [])

    def test_harmonizer_checks(self):
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [{"effect": "harmonizer"}, {"effect": "harmony"}]})
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [
                {"effect": "autotune", "squeeze_scale": "minor"},
                {"effect": "harmonizer", "level": "$handle"}]}, blocks={"handle": 0.5})
        with self.assertRaises(ValueError):
            self.cl.create_effects({"list": [{"effect": "harmonizer"}]})

    def test_vocoder_joins_the_autotune_stage(self):
        tune, effects = self.cl.create_preset({"list": [
            {"effect": "autotune", "key": "C", "scale": "major"},
            {"effect": "vocoder", "waveform": "square", "bands": 10, "octave": -1,
             "breath": 0.3, "mix": 0.8},
            {"effect": "echo"}]})
        self.assertEqual(tune, {"key": "C", "scale": "major", "vocoder": "square",
                                "vocoder_bands": 10, "vocoder_octave": -1,
                                "vocoder_breath": 0.3, "vocoder_mix": 0.8})
        self.assertEqual(len(effects), 1)
        tune, effects = self.cl.create_preset({"list": [{"effect": "Vocoder"}]})
        self.assertEqual(tune, {"correct": False, "vocoder": "saw"})

    def test_vocoder_checks(self):
        for preset in (
                [{"effect": "vocoder"}, {"effect": "vocoder"}],
                [{"effect": "vocoder"}, {"effect": "harmonizer"}],
                [{"effect": "vocoder", "bands": "many"}]):
            with self.assertRaises(ValueError):
                self.cl.create_preset({"list": preset})
        with self.assertRaises(ValueError):
            self.cl.create_preset({"list": [
                {"effect": "autotune", "squeeze_key": "D"},
                {"effect": "vocoder", "mix": "$handle"}]}, blocks={"handle": 0.5})
        with self.assertRaises(ValueError):
            self.cl.create_effects({"list": [{"effect": "vocoder"}]})
        # like every effect from 1.0 on, an unknown setting is left out
        tune, _ = self.cl.create_preset({"list": [{"effect": "vocoder", "carrier": "saw"}]})
        self.assertEqual(tune, {"correct": False, "vocoder": "saw"})

    def test_vocoder_squeeze_chord(self):
        import array
        import autotune
        rate = autotune.SAMPLE_RATE
        voc = autotune.Vocoder(rate, chord="major", squeeze_chord="minor")
        self.assertEqual(voc.chord, "major")
        voc.update(array.array("h", [0] * autotune.BUFFER_SIZE), 220.0)
        voc.squeeze(True)
        self.assertEqual(voc.chord, "minor")
        self.assertAlmostEqual(voc.notes[0].frequency, 22.0)  # 220 / 10
        voc.squeeze(False)
        self.assertEqual(voc.chord, "major")
        # one note, squeezing into a key chord
        voc = autotune.Vocoder(rate, squeeze_chord="key_triad")
        self.assertIsNone(voc.chord)
        self.assertIsNone(voc.key_steps)
        voc.squeeze(True)
        self.assertEqual(voc.key_steps, (0, 2, 4))
        voc.set_chord("dim")
        voc.squeeze(False)
        self.assertIsNone(voc.chord)
        # and back to one note from a chord
        voc = autotune.Vocoder(rate, chord="m7", squeeze_chord="none")
        voc.squeeze(True)
        self.assertIsNone(voc.chord)
        with self.assertRaises(ValueError):
            autotune.Vocoder(rate, squeeze_chord="aug")
        # the loader: the handle does one job
        tune, _ = self.cl.create_preset({"list": [{"effect": "vocoder", "squeeze_chord": "minor"}]})
        self.assertEqual(tune["vocoder_squeeze_chord"], "minor")
        for bad in ({"list": [{"effect": "autotune", "squeeze_scale": "minor"},
                              {"effect": "vocoder", "squeeze_chord": "minor"}]},
                    {"list": [{"effect": "vocoder", "squeeze_chord": "minor", "mix": "$handle"}]}):
            with self.assertRaises(ValueError):
                self.cl.create_preset(bad)

    def test_vocoder_bands_follow_the_voice(self):
        import array
        import math
        import autotune
        rate = autotune.SAMPLE_RATE
        for shape in autotune.VOCODER_WAVEFORMS:
            table, density = autotune.carrier_wave(shape)
            self.assertLessEqual(max(abs(v) for v in table), 30000)
            self.assertGreater(density, 0)
        with self.assertRaises(ValueError):
            autotune.carrier_wave("sine")
        # a chord carrier is one waveform: the chord's notes as whole
        # multiples of a sub-note, and about as many times the power
        for name, ratios in autotune.VOCODER_CHORDS:
            table, root, density = autotune.chord_wave("saw", name)
            self.assertEqual(root, ratios[0])
            self.assertEqual(len(table), autotune.VOCODER_CHORD_SAMPLES * ratios[-1])
            self.assertLessEqual(max(abs(v) for v in table), 30000)
            # its spectrum: energy at each chord note, none at the root's 2nd harmonic's neighbours
            import numpy
            spec = abs(numpy.fft.rfft(numpy.array(table, dtype=float)))
            for m in ratios:
                self.assertGreater(spec[m], 0.1 * spec.max(), (name, m))
        with self.assertRaises(ValueError):
            autotune.chord_wave("saw", "aug")
        # key chords: the chord the scale builds on the snapped note
        snap = autotune.NoteSnapper()
        snap.set_scale(autotune.scale_by_name("major"), autotune.key_by_name("C"))
        triad, seventh = (dict(autotune.VOCODER_KEY_CHORDS)[n] for n in ("key_triad", "key_seventh"))
        names = {note: autotune.vocoder_key_chord(autotune.key_tones(snap, note, triad), triad)
                 for note in (60, 62, 64, 65, 67, 71, 61)}
        self.assertEqual(names, {60: "major", 62: "minor", 64: "minor", 65: "major",
                                 67: "major", 71: "dim", 61: "major"})
        names = {note: autotune.vocoder_key_chord(autotune.key_tones(snap, note, seventh), seventh)
                 for note in (60, 62, 67, 71, 61)}
        self.assertEqual(names, {60: "maj7", 62: "m7", 67: "7", 71: "m7b5", 61: "7"})
        voc = autotune.Vocoder(rate, chord="key_triad")
        self.assertEqual(voc.chord, "major")
        voc.update(array.array("h", [0] * autotune.BUFFER_SIZE), 220.0)
        self.assertAlmostEqual(voc.notes[0].frequency, 55.0)  # 220 / 4
        voc.set_chord("dim")
        self.assertAlmostEqual(voc.notes[0].frequency, 44.0)  # 220 / 5, the 5:6:7 sub-note
        self.assertEqual(len(voc.notes[0].waveform), autotune.VOCODER_CHORD_SAMPLES * 7)
        voc = autotune.Vocoder(rate, chord="minor")
        voc.update(array.array("h", [0] * autotune.BUFFER_SIZE), 220.0)
        self.assertAlmostEqual(voc.notes[0].frequency, 22.0)  # 220 / 10, the minor chord's sub-note
        with self.assertRaises(ValueError):
            autotune.Vocoder(rate, bands=12)
        voc = autotune.Vocoder(rate, bands=8, breath=0.5)
        self.assertEqual(len(voc.notes), 8)
        centres = [n.filter.kwargs["frequency"] for n in voc.notes]
        self.assertAlmostEqual(centres[0], autotune.VOCODER_LOW_HZ)
        self.assertAlmostEqual(centres[-1], autotune.VOCODER_HIGH_HZ)

        def tone(hz, amp=6000):
            return array.array("h", [int(amp * math.sin(2 * math.pi * hz * i / rate))
                                     for i in range(autotune.BUFFER_SIZE)])

        # a tone lights the band it is in, and only that one (and its neighbours a little)
        for band in (2, 5):
            voc._levels = [0.0] * 8
            voc.update(tone(centres[band]), 150.0)
            levels = [n.amplitude for n in voc.notes]
            self.assertEqual(levels.index(max(levels)), band)
            self.assertLess(sorted(levels)[-3], 0.05 * max(levels))
        # the carrier plays the note, and holds it through unvoiced blocks
        self.assertEqual(voc.notes[0].frequency, 150.0)
        voc.update(tone(1000), 0.0)
        self.assertEqual(voc.notes[0].frequency, 150.0)
        # silence closes every band, after the release
        for _ in range(20):
            voc.update(array.array("h", [0] * autotune.BUFFER_SIZE), 0.0)
        self.assertTrue(all(n.amplitude < 1e-6 for n in voc.notes))
        # a band under the note stays shut: the carrier has nothing there
        voc.update(tone(centres[0]), 400.0)
        self.assertEqual(voc.notes[0].amplitude, 0.0)
        # breath follows the highs
        voc.update(tone(5500), 150.0)
        self.assertGreater(voc.noise.amplitude, 0.0)
        # the noise gate: a quiet hum (about -65 dB) doesn't play the carrier,
        # unless the gate is off; singing opens it, and it holds through a gap
        hum = tone(centres[3], 25)
        for _ in range(autotune.VOCODER_GATE_HOLD + 16):
            voc.update(hum, 150.0)
        self.assertTrue(all(n.amplitude < 1e-6 for n in voc.notes))
        self.assertEqual(voc.noise.amplitude, 0.0)
        open_voc = autotune.Vocoder(rate, bands=8, gate=autotune.VOCODER_GATE_OFF)
        open_voc.update(hum, 150.0)
        self.assertGreater(open_voc.notes[3].amplitude, 0.0)
        voc.update(tone(centres[3]), 150.0)
        voc.update(hum, 150.0)
        self.assertGreater(voc.notes[3].amplitude, 0.0)
        for _ in range(autotune.VOCODER_GATE_HOLD + 6):
            voc.update(hum, 150.0)
        self.assertLess(voc.notes[3].amplitude, 1e-3)
        voc.update(tone(300), 150.0)
        self.assertLess(voc.noise.amplitude, 0.001)

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
        h._double = 0
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

    def test_old_playmodes_still_load(self):
        samples = self.cl.load_samples({"samples": [
            {"file": "1.wav", "playmode": "auto"}, {"file": "2.wav", "playmode": "Hold"},
            {"file": "3.wav"}]})
        # older playmodes still load (code.py reads them and plays every slot the same way)
        self.assertEqual([s["playmode"] for s in samples], ["auto", "hold", "oneshot"])

    def test_wave_reader_plays_backwards(self):
        import autotune
        root = tempfile.mkdtemp()
        path = os.path.join(root, "ramp.wav")
        n = 3 * autotune.BLOCK_SIZE + 37
        with wave.open(path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(autotune.SAMPLE_RATE)
            out.writeframes(b"".join(int(i - 500).to_bytes(2, "little", signed=True) for i in range(n)))
        forwards = list(range(-500, n - 500))
        for block in (None, 100):
            reader = autotune.WaveReader(path, reverse=True, block=block)
            got = []
            while True:
                count = reader.read()
                if not count:
                    break
                got.extend(reader.buf[0:count])
            reader.close()
            self.assertEqual(got, forwards[::-1], block)
        self.assertEqual(list(autotune.load_reversed(path)), forwards[::-1])

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

    def test_feed_sync_keeps_every_burst_in_order(self):
        # The chain renders two blocks every two blocks, reading half 0 then
        # half 1 in one burst. Each burst must read the next two blocks in
        # order, wherever it falls, including just after a block edge (on a
        # Ting the harmonizer's burst starts 1 ms after it) with writes that
        # sometimes run a little late.
        import array
        import random
        import autotune
        A, n, B = autotune.Autotune, autotune.BLOCK_SIZE, autotune.BLOCK_NS
        rng = random.Random(1)
        for burst_after_edge in (0.3, 1.0, 1.3, 3, 7, 12, 16.5, 17, 25, 31.5):
            for length_ms in (3, 12, 14.8):
                for gate_ms in (0.2, 0.5, 2.0):
                    for phase in (0, 1):
                        clock = {"t": 0}
                        first = 5000000 + int(burst_after_edge * 1000000)
                        length = int(length_ms * 1000000)
                        bursts = [first + k * 2 * B for k in range(30)]

                        def advance(t):
                            for b in bursts:  # Python is held while a burst runs
                                if b <= t < b + length:
                                    return b + length
                            return t

                        def now():
                            t = clock["t"]
                            clock["t"] = advance(clock["t"] + 100000)  # 0.1 ms a pass
                            return t

                        class Mic:
                            def record(self, block, count):
                                # data is ready at the next block edge, 5 ms in
                                clock["t"] = advance(clock["t"] + B - (clock["t"] - 5000000) % B)
                                clock["edge"] = 5000000 + ((clock["t"] - 5000000) // B) * B
                                return count

                        h = A.__new__(A)
                        h.mic, h._block, h._zeros = Mic(), array.array("h", [0] * n), array.array("h", [0] * n)
                        h._out, h._half, h._burst_due, h._guard = array.array("h", [-1] * (2 * n)), 0, None, 0
                        h._step = 0
                        h._noise_gate = lambda block: clock.__setitem__("t", clock["t"] + int(gate_ms * 1000000))
                        saved = autotune.time.monotonic_ns, autotune.FEED_PHASE
                        autotune.time.monotonic_ns, autotune.FEED_PHASE = now, phase
                        try:
                            h._sync_feed()
                            edge = clock["edge"]  # the block edge the sync lined up on
                            writes = []  # (time, half, block)
                            for m in range(1, 40):
                                late = rng.choice((0, 0, 0, 0.3, 1, 2)) * 1000000
                                clock["t"] = max(clock["t"], advance(edge + m * B + int(gate_ms * 1000000) + int(late)))
                                h._wait_out_burst()
                                t = clock["t"]
                                h._feed_block(array.array("h", [m] * n))
                                writes.append((t, 0 if h._out[0] == m else 1, m))
                        finally:
                            autotune.time.monotonic_ns, autotune.FEED_PHASE = saved
                        read = []
                        for b in bursts:
                            if writes[2][0] < b < writes[-1][0]:
                                for half in (0, 1):
                                    read.append([blk for t, hf, blk in writes if t < b and hf == half][-1])
                        self.assertEqual(read, list(range(read[0], read[0] + len(read))),
                                         (burst_after_edge, length_ms, gate_ms, phase))

    def test_counted_renders_keep_every_burst_in_order(self):
        # With the render counter, each burst reads two blocks in a row,
        # older first, wherever the burst falls and whichever half the first
        # render reads. On time, that is every block once; late writes only
        # repeat or skip one.
        import array
        import random
        import autotune
        A, n, B = autotune.Autotune, autotune.BLOCK_SIZE, autotune.BLOCK_NS
        rng = random.Random(2)
        for trial in range(200):
            h = A.__new__(A)
            h._out = array.array("h", [-1] * (2 * n))
            h._step = 1 / 64
            h._probe = type("Probe", (), {"phase": 0.0})()
            h._probe_phase, h._newest = 0.0, 0
            view = memoryview(h._out)
            h._front, h._back = view[:n], view[n:]
            late = (0, 0, 0, 0.5, 2, 6, 10) if trial % 2 else (0.5,)
            renders = h._renders = rng.choice((0, 1))  # one render short, or not
            length = rng.choice((3, 12, 14.8)) * 1000000
            offset = rng.randrange(0, 2 * B)
            bursts = [offset + k * 2 * B for k in range(40)]
            reads, done = [], 0
            for m in range(1, 70):
                t = m * B + rng.choice(late) * 1000000
                for b in bursts:  # Python is held while a burst runs
                    if b <= t < b + length:
                        t = b + length
                while done < len(bursts) and bursts[done] < t:
                    pair = []
                    for _ in (0, 1):
                        half = renders % 2
                        pair.append(h._out[half * n])
                        renders += 1
                        h._probe.phase = (h._probe.phase + h._step) % 1.0
                    reads.append(pair)
                    done += 1
                h._feed_block(array.array("h", [m] * n))
            reads = reads[2:]
            # two blocks in a row, older first, never going back
            self.assertTrue(all(b == a + 1 for a, b in reads), (trial, reads))
            self.assertEqual([b for a, b in reads], sorted(b for a, b in reads), trial)
            if not trial % 2:  # on time: every block, once
                got = [blk for pair in reads for blk in pair]
                self.assertEqual(got, list(range(got[0], got[0] + len(got))), trial)

    def test_counted_renders_wait_for_a_render_due_at_the_write(self):
        # On the harmonizer the chain renders about 1 ms after a mic block
        # lands, while the block is being written. Writes that fall either
        # side of it at random repeat and skip blocks; the guard makes them
        # all land after it, so every block is read once, in order. That
        # holds with the pitch work done in the wait, even when it overruns.
        import array
        import random
        import autotune
        A, n, B = autotune.Autotune, autotune.BLOCK_SIZE, autotune.BLOCK_NS
        rng = random.Random(3)
        for render_after in (0.2, 0.6, 1.0, 1.5, 2.5, 5.0, 8.0, 11.0, 15.5, 20.0, 30.0):
            for length_ms, work_ms in ((3, 0), (12, 0), (14.5, 0), (3, 4), (3, 7), (12, 4)):
                h = A.__new__(A)
                h._out = array.array("h", [-1] * (2 * n))
                view = memoryview(h._out)
                h._front, h._back = view[:n], view[n:]
                h._step, h._period, h._newest, h._waiting = 1 / 64, 2 * B, 0, False
                h._probe = type("Probe", (), {"phase": 0.0})()
                h._probe_phase, h._renders = 0.0, 0
                first = B + int(render_after * 1000000)
                length = int(length_ms * 1000000)
                h._burst_due = first + int(rng.uniform(-0.3, 0.3) * 1000000)  # as timed
                state = {"t": 0, "next": first, "renders": 0}
                reads = []

                def run_to(t):
                    while state["next"] <= t:  # a render holds Python up
                        pair = []
                        for _ in (0, 1):
                            pair.append(h._out[(state["renders"] % 2) * n])
                            state["renders"] += 1
                            h._probe.phase = (h._probe.phase + h._step) % 1.0
                        reads.append(pair)
                        t = max(t, state["next"] + length)
                        state["next"] += 2 * B
                    state["t"] = t

                def now():
                    t = state["t"]
                    run_to(t + 100000)
                    return t

                saved = autotune.time.monotonic_ns
                autotune.time.monotonic_ns = now
                try:
                    for m in range(1, 60):
                        run_to(max(state["t"], m * B + int(rng.uniform(0.3, 1.1) * 1000000)))
                        if work_ms and h._render_ahead() > work_ms * 1000000:
                            # the pitch work, done in the wait (it can overrun)
                            run_to(state["t"] + int(work_ms * rng.uniform(0.5, 1.6) * 1000000))
                        h._wait_out_burst()
                        h._feed_block(array.array("h", [m] * n))
                finally:
                    autotune.time.monotonic_ns = saved
                got = [blk for pair in reads[2:-1] for blk in pair]
                self.assertEqual(got, list(range(got[0], got[0] + len(got))),
                                 (render_after, length_ms, work_ms))

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

    def test_install_page_lists_this_version(self):
        # The install page's update check names what changed since the mic's
        # version from its "What's new" rows, so each release needs one.
        import re
        with open(os.path.join(FIRMWARE, "code.py")) as file:
            version = re.search(r'^FIRMWARE_VERSION = "([\d.]+)"', file.read(), re.M).group(1)
        with open(os.path.join(os.path.dirname(HERE), "docs", "install.html")) as file:
            page = file.read()
        self.assertIn('<tr data-version="%s">' % version, page,
                      "add a row for firmware %s to the What's new table in docs/install.html" % version)

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
            # held 1.0 s, minus the 0.1 s paddle settle, 0.05 s trim and 0.1 s release
            self.assertAlmostEqual(take.getnframes() / 24000, 0.75, delta=0.03)
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

    def test_sample_button_taps_holds_and_double_taps(self):
        config = json.loads(json.dumps(TEST_CONFIG))
        # an old playmode is read and ignored: every slot plays tap / hold / double tap
        config["samples"] = [{"file": "1.wav", "playmode": "startstop"}]
        root, out, s = run("auto", config, tone=(340.0, 1.0))
        lines = [l for l in out.splitlines() if l.startswith(("playing 1", "stopped 1"))]
        self.assertEqual(lines, [
            "playing 1",                          # tap: plays on to the end
            "playing 1",
            "playing 1 (double tap: backwards)",  # second press 200 ms after the first
            "playing 1",
            "stopped 1 (held 600 ms)",            # held: let go stops it
            "playing 1",
            "playing 1",                          # 500 ms apart: two taps
        ])
        self.assertEqual(out.count("turned round"), 1)  # built once, then kept
        # with the sample's own effects, the backwards copy feeds their chain
        config["samples"][0]["effects"] = [{"effect": "freeverb", "mix": 0.4}]
        root, out, s = run("auto", config, tone=(340.0, 1.0))
        self.assertIn(["_Obj", "_Obj"], [list(p) for p in s["plays"]])
        self.assertIn("playing 1 (double tap: backwards)", out)

    def test_double_tap_backwards_through_autotune(self):
        config = json.loads(json.dumps(THROUGH_CONFIG))
        config["samples"] = [{"file": "1.wav"}]
        root, out, s = run("auto_through", config, tone=(340.0, 1.0))
        self.assertIn("playing 1 (double tap: backwards)", out)
        self.assertNotIn("can't", out)
        self.assertNotIn("turned round", out)  # streamed from flash instead

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

    def test_harmonizer_double_adds_the_voice_an_octave_below(self):
        config = {"name": "H", "presets": [{"list": [
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0},
            {"effect": "harmonizer", "chord": "minor", "double": "below", "level": 0.8}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("harmonizer 3 voices", out)
        lead, third, fifth, octave = s["shifters"]
        # the triad as before, plus G2: the sung (snapped) note an octave down
        self.assertAlmostEqual(third["min"], 3 - 0.95, delta=0.05)
        self.assertAlmostEqual(fifth["min"], 7 - 0.95, delta=0.05)
        self.assertAlmostEqual(octave["min"], -12 - 0.95, delta=0.05)

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

    def test_vocoder_plays_the_tuned_note(self):
        config = {"name": "V", "presets": [{"list": [
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0},
            {"effect": "vocoder", "bands": 6, "octave": -1},
            {"effect": "reverb", "mix": 0.3}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("autotune via no shifter, vocoder 6 bands", out)
        # no voice in the mix, so no shifter: the synth heads the chain
        self.assertEqual(s["shifters"], [])
        self.assertIn(["_Obj", "Synthesizer"], s["plays"])
        voc, = s["vocoders"]
        # 207 Hz snaps down to G3 (196 Hz), an octave down
        self.assertAlmostEqual(voc["hz"][0], 98.0, delta=0.5)
        self.assertEqual(voc["rate"], 24000)
        # a 207 Hz voice opens the two bands around it (FFT bins are 47 Hz
        # wide, so it spills from the first into the second) and no others
        peaks = voc["peaks"]
        self.assertGreater(min(peaks[:2]), 0.2)
        self.assertLess(max(peaks[2:]), 0.05)

    def test_vocoder_key_chord(self):
        config = {"name": "V", "presets": [{"list": [
            {"effect": "autotune", "key": "A", "scale": "minor", "speed": 1.0},
            {"effect": "vocoder", "bands": 6, "octave": -1, "chord": "key_seventh"}]}],
            "samples": []}
        root, out, s = run("level", config)
        voc, = s["vocoders"]
        # 207 Hz snaps to G, and G in A minor is G B D F, a dominant 7th
        # (4:5:6:7): the carrier plays its sub-note, two octaves under 98 Hz
        self.assertAlmostEqual(voc["hz"][0], 24.5, delta=0.2)
        self.assertEqual(voc["table"], 128 * 7)  # VOCODER_CHORD_SAMPLES per multiple

    def test_vocoder_squeeze_switches_chord(self):
        config = dict(TEST_CONFIG, presets=[{"list": [
            {"effect": "autotune", "key": "A", "scale": "chromatic"},
            {"effect": "vocoder", "chord": "major", "squeeze_chord": "minor"}]}]
            + TEST_CONFIG["presets"][1:])
        root, out, s = run("record", config)
        self.assertIn("vocoder chord now minor", out)
        self.assertLess(out.index("vocoder chord now minor"), out.index("vocoder chord now major"))

    def test_vocoder_bands_cap_with_the_voice(self):
        config = {"name": "V", "presets": [{"list": [
            {"effect": "vocoder", "bands": 11, "mix": 0.9}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("using 8", out)
        voc, = s["vocoders"]
        self.assertEqual(len(voc["hz"]), 8)

    def test_vocoder_with_the_voice(self):
        config = {"name": "V", "presets": [{"list": [
            {"effect": "vocoder", "waveform": "pulse", "mix": "$handle"}]}], "samples": []}
        root, out, s = run("level", config)
        self.assertIn("voice via GranularPitchShift, vocoder 8 bands", out)
        # the voice stays untuned and the carrier plays it as sung
        lead, = s["shifters"]
        self.assertEqual((lead["min"], lead["max"]), (0.0, 0.0))
        voc, = s["vocoders"]
        self.assertAlmostEqual(voc["hz"][0], 207.0, delta=1.0)
        # the voice (the shifter's mixer, with its render counter) and the synth
        mixer = s["vocoder_mixer"]
        self.assertEqual(mixer["playing"], ["Mixer", "Synthesizer"])
        # squeezed all the way: vocoder only
        self.assertAlmostEqual(mixer["levels"][0], 0.0, places=5)
        self.assertAlmostEqual(mixer["levels"][1], 1.0, places=5)

    def test_effects_only_pages(self):
        config = {"name": "PAGES", "effects_only": True, "sample_fx": "preset",
                  "presets": [{"name": "P%d" % (i + 1), "list": [
                      {"effect": "echo", "max_delay_ms": 200, "delay_ms": 100 + i, "mix": 0.3}]}
                      for i in range(6)],
                  "samples": [{"file": "1.wav", "playmode": "auto"}]}
        root, out, s = run("pages", config, tone=(340.0, 1.0))
        picks = [l.split(" (")[0] for l in out.splitlines() if l.startswith("page ")]
        self.assertEqual(picks, [
            "page 1 of 2, preset 0 of 4",     # boot: clean
            "page 1 of 2, preset 1 of 4: P1",
            "page 1 of 2, preset 2 of 4: P2",
            "page 2 of 2, preset 2 of 2: P6",  # MIDDLE keeps the slot
            "page 2 of 2, preset 1 of 2: P5",  # TOP goes round the page, no clean
            "page 2 of 2, preset 0 of 2",      # BOTTOM: clean
            "page 1 of 2, preset 1 of 4: P1",  # MIDDLE from clean: page 1, preset 1
            "page 1 of 2, preset 2 of 4: P2",
            "page 1 of 2, preset 3 of 4: P3",
            "page 2 of 2, preset 2 of 2: P6",  # a shorter page stops at its last
            "page 1 of 2, preset 2 of 4: P2",
            "page 1 of 2, preset 0 of 4",      # BOTTOM: clean (a second press does nothing)
            "page 1 of 2, preset 1 of 4: P1",  # TOP from clean: page 1, preset 1
        ])
        self.assertIsNone(s["record_spot"])
        # no sample is loaded or played, and the presets get no input mixer
        self.assertNotIn("loaded /1.wav", out)
        self.assertNotIn("playing 1", out)
        self.assertEqual(s["heads"], [])
        # the white LEDs count the page, and go dark with the red ones on clean
        whites = [len(w) for t, w in transitions(s["white_log"]) if t < 5.8]
        self.assertEqual([w for i, w in enumerate(whites) if i == 0 or w != whites[i - 1]],
                         [0, 1, 2, 0, 1, 2, 1, 0, 1])
        reds = [c for t, c in transitions(s["red_counts"]) if 2.4 < t < 2.9]
        self.assertEqual(reds[-1], 0)

    def test_effects_only_caps_at_four_pages(self):
        config = {"effects_only": True,
                  "presets": [{"list": []} for _ in range(20)]}
        root, out, s = run("pages", config)
        self.assertIn("effects only: 4 page(s) of presets", out)
        self.assertIn("BOTTOM = clean", out)
        self.assertNotIn("0 = clean", out)
        # without effects_only only four presets are read, as before
        root, out, s = run("pages", dict(config, effects_only=False))
        self.assertIn("4 preset(s)", out)
        self.assertEqual(s["record_spot"], 5)

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
