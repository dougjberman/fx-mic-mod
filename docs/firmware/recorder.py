# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors
#
# SPDX-License-Identifier: MIT
"""
On-device sample recorder for the EP-2350.

Records the mic into RAM (in 16 KB pieces) while the handle is held, then writes a 16-bit mono
WAV over the slot's file. Writing happens only after the take, because flash
writes on the RP2350 stall the CPU and would drop audio mid-recording.

Usage::

    rec = Recorder(mic, seconds=4)
    rec.start()
    while rec.recording:
        rec.step()          # blocks for one BLOCK_SIZE of audio
    rec.save("/1.wav")
    rec.deinit()

Needs the filesystem writable by CircuitPython; see boot.py.
"""

import array
import os
import struct
import time

try:
    from ulab import numpy as np  # CircuitPython build with ulab
except ImportError:
    try:
        import numpy as np  # desktop CPython, for testing
    except ImportError:
        np = None

SAMPLE_RATE = 16000
BLOCK_SIZE = 256        # samples per record() call (16 ms)
TRIM_MS = 50            # dropped from the start of each take: handle click
FADE_MS = 5             # fade-out at the end of each take, against a click
CHUNK = 8192            # samples per buffer piece (16 KB)
MIN_SECONDS = 1         # shortest take worth offering when memory is short


def wav_header(frames, sample_rate=SAMPLE_RATE):
    """44-byte header for a 16-bit mono PCM WAV of ``frames`` samples."""
    data = frames * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", data,
    )


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


class Recorder:
    """Records up to ``seconds`` of mic audio into preallocated buffers.

    The take lives in CHUNK-sized pieces rather than one buffer: at 24 kHz a
    4 s take is 192 KB, and after a few preset switches the heap rarely has
    one free block that big even with plenty free in total. If the heap runs
    out part way the take is shortened to what fit, down to MIN_SECONDS.
    """

    def __init__(self, mic, seconds=4, sample_rate=SAMPLE_RATE):
        self.mic = mic
        self.sample_rate = sample_rate
        wanted = int(seconds * sample_rate)
        self._block = array.array("h", [0] * BLOCK_SIZE)
        # Allocated up front, so a take cannot fail half way with
        # MemoryError. ulab allocates each chunk directly; a plain array
        # would need a temporary list as big as the chunk.
        self._chunks = []
        size = 0
        try:
            while size < wanted:
                n = min(CHUNK, wanted - size)
                if np is not None:
                    self._chunks.append(np.zeros(n, dtype=np.int16))
                else:
                    self._chunks.append(array.array("h", bytes(n * 2)))
                size += n
        except MemoryError:
            if size < MIN_SECONDS * sample_rate:
                self._chunks = None
                raise
        self.capacity = size
        self.seconds = size / sample_rate
        self.frames = 0
        self.recording = False
        self._started = 0.0

    @property
    def remaining(self):
        """Fraction of the take still available, 1.0 .. 0.0."""
        return 1.0 - self.frames / self.capacity

    def start(self):
        self.frames = 0
        self.recording = True
        self._started = time.monotonic()

    def stop(self):
        self.recording = False

    def step(self):
        """Record one block. Stops by itself when the buffers are full."""
        if not self.recording:
            return 0
        block = self._block
        got = self.mic.record(block, BLOCK_SIZE)
        got = min(got, self.capacity - self.frames)
        done = 0
        while done < got:
            chunk = self._chunks[(self.frames + done) // CHUNK]
            at = (self.frames + done) % CHUNK
            n = min(got - done, len(chunk) - at)
            if np is not None:
                chunk[at:at + n] = np.frombuffer(block, dtype=np.int16)[done:done + n]
            else:
                chunk[at:at + n] = block[done:done + n]
            done += n
        self.frames += got
        if self.frames >= self.capacity:
            self.recording = False
        return got

    def take(self):
        """(start, end) sample indices of the usable take, after trimming."""
        start = min(self.frames, self.sample_rate * TRIM_MS // 1000)
        return start, self.frames

    def save(self, path):
        """Write the take to ``path`` as a WAV. Returns the frames written.

        Writes ``path + ".tmp"`` first and renames it over ``path``, so a power
        cut mid-write leaves the old sample in place, not a broken file.
        """
        start, end = self.take()
        frames = end - start
        if frames <= 0:
            return 0

        chunks = self._chunks
        fade = min(frames, self.sample_rate * FADE_MS // 1000)
        for i in range(fade):
            index = end - fade + i
            chunk = chunks[index // CHUNK]
            chunk[index % CHUNK] = int(chunk[index % CHUNK]) * (fade - i) // fade

        tmp = path + ".tmp"
        with open(tmp, "wb") as file:
            file.write(wav_header(frames, self.sample_rate))
            at = start
            while at < end:
                chunk = chunks[at // CHUNK]
                lo = at % CHUNK
                hi = min(len(chunk), lo + end - at)
                # A 1-D slice of either buffer type is contiguous and exposes
                # the buffer protocol, so it is written without a copy.
                file.write(chunk[lo:hi] if np is not None else memoryview(chunk)[lo:hi])
                at += hi - lo
        if _exists(path):
            os.remove(path)
        os.rename(tmp, path)
        if hasattr(os, "sync"):
            os.sync()
        return frames

    def deinit(self):
        self._chunks = None
        self._block = None
