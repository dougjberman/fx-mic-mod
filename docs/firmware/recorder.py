# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors

# SPDX-License-Identifier: MIT
0


















import array
import os
import struct
import time

try:
    from ulab import numpy as np
except ImportError:
    try:
        import numpy as np
    except ImportError:
        np = None

SAMPLE_RATE = 16000
BLOCK_MS = 16
TRIM_MS = 50
FADE_MS = 5




ONSET_LEVEL = 600
PRE_ROLL_MS = 10
WRITE_SAMPLES = 2048
CHUNK = 8192
MIN_SECONDS = 1


def wav_header(frames, sample_rate=SAMPLE_RATE):
    0
    data = frames * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", data,
    )


def drive_free(path="/"):
    0
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
    0







    def __init__(self, mic, seconds=4, sample_rate=SAMPLE_RATE):
        self.mic = mic
        self.sample_rate = sample_rate
        wanted = int(seconds * sample_rate)



        self._block_size = sample_rate * BLOCK_MS // 1000
        self._block = array.array("h", [0] * self._block_size)



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
        0
        return 1.0 - self.frames / self.capacity

    def start(self):
        self.frames = 0
        self.recording = True
        self._started = time.monotonic()

    def stop(self):
        self.recording = False

    def step(self):
        0
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
        0
        start = min(self.frames, self.sample_rate * TRIM_MS // 1000)
        onset = self._onset(start, self.frames)
        if onset is not None:
            start = max(start, onset - self.sample_rate * PRE_ROLL_MS // 1000)
        return start, self.frames

    def _onset(self, start, end):
        0
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

    def save(self, path, tick=None):
        0








        start, end = self.take()
        frames = end - start
        if frames <= 0:
            return 0

        chunks = self._chunks
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

        _remove(tmp)
        size = 44 + 2 * frames
        free = drive_free(path)
        if free is not None and free < size + 4096 and _exists(path):

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
