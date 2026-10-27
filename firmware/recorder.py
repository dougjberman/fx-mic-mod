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
        rec.step()          # blocks for one block (BLOCK_MS) of audio
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
_FLOAT = getattr(np, "float", float)  # ulab's float dtype; numpy takes float

SAMPLE_RATE = 16000
BLOCK_MS = 16           # audio per record() call, the loop's pace
TRIM_MS = 50            # dropped from the start of each take: handle click
RELEASE_MS = 100        # dropped from the end of a take ended by letting go: handle release
FADE_MS = 5             # fade-in and fade-out at the ends of each take, against a click
# With trimming on, silence around the sound is cut, so the sample sounds
# the moment it is triggered and stops when the sound does. The take starts
# PRE_ROLL_MS before the first sample at ONSET_LEVEL (about -35 dBFS, just
# over the mic's hiss), so the attack stays whole, and ends POST_ROLL_MS after
# the last one, so a decay or reverb tail isn't chopped. A take that never
# gets that loud is kept as it is.
ONSET_LEVEL = 600
PRE_ROLL_MS = 10
POST_ROLL_MS = 150
# With normalizing on, a take is scaled so its loudest sample sits at
# NORMALIZE_PEAK (-3 dBFS), but never boosted more than NORMALIZE_MAX_GAIN
# (+12 dB), so a near-silent take doesn't come back as loud hiss. A take with
# nothing at ONSET_LEVEL is left alone.
NORMALIZE_PEAK = 23197
NORMALIZE_MAX_GAIN = 4.0
WRITE_SAMPLES = 2048    # samples per file write, so the save animation keeps moving
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


def drive_free(path="/"):
    """Bytes free on the drive holding ``path``, or None if unknown."""
    try:
        stat = os.statvfs(path)
    except (AttributeError, OSError):
        return None
    return stat[0] * stat[4]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


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

    def __init__(self, mic, seconds=4, sample_rate=SAMPLE_RATE, trim=True, normalize=False):
        self.mic = mic
        self.trim = trim
        self.normalize = normalize
        self.sample_rate = sample_rate
        wanted = int(seconds * sample_rate)
        # Sized by time, not samples: a fixed 256 was 16 ms at 16 kHz but
        # only 10.7 ms at 24 kHz, too short for the main loop's other work,
        # so the mic overflowed mid-take and dropped audio.
        self._block_size = sample_rate * BLOCK_MS // 1000
        self._block = array.array("h", [0] * self._block_size)
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
        self.released = False
        self._started = 0.0

    @property
    def remaining(self):
        """Fraction of the take still available, 1.0 .. 0.0."""
        return 1.0 - self.frames / self.capacity

    def start(self):
        self.frames = 0
        self.recording = True
        self.released = False
        self._started = time.monotonic()

    def stop(self):
        """End the take early, when the handle is let go."""
        self.recording = False
        self.released = True

    def step(self):
        """Record one block. Stops by itself when the buffers are full."""
        if not self.recording:
            return 0
        block = self._block
        got = self.mic.record(block, self._block_size)
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
        rate = self.sample_rate
        start = min(self.frames, rate * TRIM_MS // 1000)
        end = self.frames
        if self.released:
            end = max(start, end - rate * RELEASE_MS // 1000)
        if self.trim:
            onset = self._onset(start, end)
            if onset is not None:
                start = max(start, onset - rate * PRE_ROLL_MS // 1000)
                last = self._last_loud(start, end)
                end = min(end, last + 1 + rate * POST_ROLL_MS // 1000)
        return start, end

    def _sample(self, index):
        return int(self._chunks[index // CHUNK][index % CHUNK])

    def _last_loud(self, start, end):
        """Index of the last sample at ONSET_LEVEL or louder (one exists)."""
        at = end
        while at > start:
            lo = max(start, (at - 1) // CHUNK * CHUNK)
            if self._onset(lo, at) is not None:
                for index in range(at - 1, lo - 1, -1):
                    value = self._sample(index)
                    if value >= ONSET_LEVEL or value <= -ONSET_LEVEL:
                        return index
            at = lo
        return start

    def _onset(self, start, end):
        """Index of the first sample at ONSET_LEVEL or louder, or None."""
        at = start
        while at < end:
            chunk = self._chunks[at // CHUNK]
            lo = at % CHUNK
            hi = min(len(chunk), lo + end - at)
            if np is not None:
                part = chunk[lo:hi]
                loud = (part >= ONSET_LEVEL) + (part <= -ONSET_LEVEL)
                if np.max(loud):
                    return at + int(np.argmax(loud))
            else:
                for i in range(lo, hi):
                    if chunk[i] >= ONSET_LEVEL or chunk[i] <= -ONSET_LEVEL:
                        return at + i - lo
            at += hi - lo
        return None

    def _parts(self, start, end):
        """(chunk, lo, hi) pieces covering samples start..end."""
        at = start
        while at < end:
            chunk = self._chunks[at // CHUNK]
            lo = at % CHUNK
            hi = min(len(chunk), lo + end - at)
            yield chunk, lo, hi
            at += hi - lo

    def peak(self, start, end):
        """Largest absolute sample in start..end."""
        peak = 0
        for chunk, lo, hi in self._parts(start, end):
            if np is not None:
                part = chunk[lo:hi]
                peak = max(peak, int(np.max(part)), -int(np.min(part)))
            else:
                for i in range(lo, hi):
                    value = chunk[i]
                    peak = max(peak, value, -value)
        return peak

    def _normalize(self, start, end, tick=None):
        """Scale start..end in place (see NORMALIZE_PEAK)."""
        peak = self.peak(start, end)
        if peak < ONSET_LEVEL:
            return
        gain = min(NORMALIZE_PEAK / peak, NORMALIZE_MAX_GAIN)
        for chunk, lo, hi in self._parts(start, end):
            if tick is not None:
                tick()
            if np is not None:
                scaled = np.array(chunk[lo:hi], dtype=_FLOAT) * gain
                chunk[lo:hi] = np.array(np.clip(scaled, -32768, 32767), dtype=np.int16)
            else:
                for i in range(lo, hi):
                    chunk[i] = max(-32768, min(32767, int(chunk[i] * gain)))

    def save(self, path, tick=None):
        """Write the take to ``path`` as a WAV. Returns the frames written.

        Writes ``path + ".tmp"`` first and renames it over ``path``, so a power
        cut mid-write leaves the old sample in place, not a broken file. When
        the drive has no room for both, the old file goes first. A failed
        write removes its partial tmp, so it never holds space.

        ``tick``, if given, is called between writes, for a busy animation.
        """
        start, end = self.take()
        frames = end - start
        if frames <= 0:
            return 0

        chunks = self._chunks
        if self.normalize:
            self._normalize(start, end, tick)
        fade = min(frames, self.sample_rate * FADE_MS // 1000)
        if start > 0:
            for i in range(fade):
                index = start + i
                chunk = chunks[index // CHUNK]
                chunk[index % CHUNK] = int(chunk[index % CHUNK]) * i // fade
        for i in range(fade):
            index = end - fade + i
            chunk = chunks[index // CHUNK]
            chunk[index % CHUNK] = int(chunk[index % CHUNK]) * (fade - i) // fade

        tmp = path + ".tmp"
        # A take that failed before leaves a partial tmp; it only eats space.
        _remove(tmp)
        size = 44 + 2 * frames
        free = drive_free(path)
        if free is not None and free < size + 4096 and _exists(path):
            # No room for the new take beside the old one: give up the old one.
            _remove(path)
        try:
            with open(tmp, "wb") as file:
                file.write(wav_header(frames, self.sample_rate))
                at = start
                while at < end:
                    chunk = chunks[at // CHUNK]
                    lo = at % CHUNK
                    hi = min(len(chunk), lo + end - at, lo + WRITE_SAMPLES)
                    if tick is not None:
                        tick()
                    # A 1-D slice of either buffer type is contiguous and exposes
                    # the buffer protocol, so it is written without a copy.
                    file.write(chunk[lo:hi] if np is not None else memoryview(chunk)[lo:hi])
                    at += hi - lo
        except OSError:
            _remove(tmp)
            raise
        if _exists(path):
            os.remove(path)
        os.rename(tmp, path)
        if hasattr(os, "sync"):
            os.sync()
        return frames

    def deinit(self):
        self._chunks = None
        self._block = None
