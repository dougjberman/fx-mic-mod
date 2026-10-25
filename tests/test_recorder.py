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
