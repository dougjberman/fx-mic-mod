"""Recorder buffer tests. Run: python3 -m unittest discover tests"""

import array
import os
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "firmware"))

import recorder  # noqa: E402


class RampMic:
    """Hands out a rising sample count, so every sample's place is checkable."""

    def __init__(self):
        self.n = 0

    def record(self, block, count):
        for i in range(count):
            block[i] = (self.n + i) % 30000
        self.n += count
        return count


class RecorderTests(unittest.TestCase):
    def test_take_spans_chunks_in_order(self):
        rec = recorder.Recorder(RampMic(), seconds=1, sample_rate=24000)
        self.assertGreater(len(rec._chunks), 2)
        # one mic read is 16 ms at any rate, so the loop keeps pace
        self.assertEqual(len(rec._block), 384)
        rec.start()
        while rec.recording:
            rec.step()
        path = os.path.join(tempfile.mkdtemp(), "1.wav")
        frames = rec.save(path)
        with wave.open(path) as take:
            data = array.array("h", take.readframes(take.getnframes()))
        start = 24000 * recorder.TRIM_MS // 1000
        self.assertEqual(frames, 24000 - start)
        fade = 24000 * recorder.FADE_MS // 1000
        self.assertEqual(list(data[fade:-fade]),
                         [(start + i) % 30000 for i in range(fade, frames - fade)])
        # faded in from silence, against a click
        self.assertEqual(data[0], 0)
        self.assertLess(data[fade // 2], start + fade // 2)

    def test_silence_before_the_voice_is_cut(self):
        class LateMic:
            """Silent for half a second, then loud."""
            n = 0

            def record(self, block, count):
                for i in range(count):
                    block[i] = 0 if self.n + i < 12000 else 5000
                self.n += count
                return count

        rec = recorder.Recorder(LateMic(), seconds=1, sample_rate=24000)
        rec.start()
        while rec.recording:
            rec.step()
        start, end = rec.take()
        self.assertEqual(start, 12000 - 24000 * recorder.PRE_ROLL_MS // 1000)
        self.assertEqual(end, 24000)

    def _burst_take(self, trim):
        class BurstMic:
            """Silent, loud from 0.3 s to 0.5 s, then silent again."""
            n = 0

            def record(self, block, count):
                for i in range(count):
                    block[i] = 5000 if 7200 <= self.n + i < 12000 else 30
                self.n += count
                return count

        rec = recorder.Recorder(BurstMic(), seconds=1, sample_rate=24000, trim=trim)
        rec.start()
        while rec.recording:
            rec.step()
        return rec

    def test_silence_after_the_sound_is_cut(self):
        start, end = self._burst_take(True).take()
        self.assertEqual(start, 7200 - 24000 * recorder.PRE_ROLL_MS // 1000)
        self.assertEqual(end, 12000 + 24000 * recorder.POST_ROLL_MS // 1000)

    def test_trim_off_keeps_the_silence(self):
        rec = self._burst_take(False)
        rec.stop()  # let go: the release is cut even with trimming off
        self.assertEqual(rec.take(), (24000 * recorder.TRIM_MS // 1000,
                                      24000 - 24000 * recorder.RELEASE_MS // 1000))

    def test_a_quiet_take_is_kept_whole(self):
        class QuietMic:
            def record(self, block, count):
                for i in range(count):
                    block[i] = 100 if i % 2 else -100
                return count

        rec = recorder.Recorder(QuietMic(), seconds=1, sample_rate=24000)
        rec.start()
        while rec.recording:
            rec.step()
        self.assertEqual(rec.take(), (24000 * recorder.TRIM_MS // 1000, 24000))

    def test_save_ticks_while_it_writes(self):
        rec = self._take()
        ticks = []
        rec.save(os.path.join(tempfile.mkdtemp(), "1.wav"), tick=lambda: ticks.append(1))
        self.assertGreaterEqual(len(ticks), (24000 - 1200) // recorder.WRITE_SAMPLES)

    def _take(self):
        rec = recorder.Recorder(RampMic(), seconds=1, sample_rate=24000)
        rec.start()
        while rec.recording:
            rec.step()
        return rec

    def test_failed_save_leaves_no_tmp(self):
        rec = self._take()
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "4.wav")
        with open(path + ".tmp", "wb") as stale:
            stale.write(b"x" * 1000)
        with open(path, "wb") as old:
            old.write(b"old")
        real_open = recorder.open if hasattr(recorder, "open") else open

        class Full:
            def __init__(self, f):
                self.f = f
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.f.close()
            def write(self, data):
                raise OSError(28, "No space left on device")

        recorder.open = lambda p, m: Full(real_open(p, m))
        try:
            with self.assertRaises(OSError):
                rec.save(path)
        finally:
            del recorder.open
        self.assertEqual(sorted(os.listdir(folder)), ["4.wav"])
        with open(path, "rb") as old:
            self.assertEqual(old.read(), b"old")

    def test_low_drive_gives_up_old_file_first(self):
        rec = self._take()
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "4.wav")
        with open(path, "wb") as old:
            old.write(b"old" * 1000)
        seen = []
        real_free = recorder.drive_free

        def free(p="/"):
            seen.append(os.path.exists(path))
            return 1000

        recorder.drive_free = free
        try:
            frames = rec.save(path)
        finally:
            recorder.drive_free = real_free
        self.assertEqual(seen, [True])
        with wave.open(path) as take:
            self.assertEqual(take.getnframes(), frames)
        self.assertEqual(os.listdir(folder), ["4.wav"])

    def test_short_memory_shortens_the_take(self):
        made = []
        real = recorder.array.array

        def tight(kind, init):
            if len(made) == 4:  # the record block, then three chunks
                raise MemoryError
            made.append(1)
            return real(kind, init)

        np, recorder.np = recorder.np, None
        recorder.array.array = tight
        try:
            rec = recorder.Recorder(RampMic(), seconds=4, sample_rate=24000)
        finally:
            recorder.array.array = real
            recorder.np = np
        self.assertEqual(rec.capacity, 3 * recorder.CHUNK)
        self.assertLess(rec.seconds, 4)


if __name__ == "__main__":
    unittest.main()
