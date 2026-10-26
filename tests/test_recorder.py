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
        self.assertEqual(list(data[:-fade]), [(start + i) % 30000 for i in range(frames - fade)])

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
