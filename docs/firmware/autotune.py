# autotune.py - chromatic autotune stage for the Teenage Engineering EP-2350 "ting"
#
# Usable on its own (see code.py) or as the first stage of a longer effects
# chain. Python has to see the audio to find the pitch, so this stage owns the
# mic: it records blocks, detects pitch, snaps to the scale, and plays the
# corrected voice out of `Autotune.output`, which any audiodelays /
# audiofilters / audiofreeverb effect (or a mixer voice) can play.
#
#   mic.record() -> detect + snap -> double-buffered RawSample
#       -> audiodelays.GranularPitchShift (C, OLA grains) -> Autotune.output
#
# Call Autotune.step() once per loop pass. It blocks for one BLOCK_SIZE of
# audio (16 ms), which also paces the caller's loop to real time.

import array
import math
import struct

try:
    from ulab import numpy as np  # CircuitPython build with ulab
except ImportError:
    try:
        import numpy as np  # desktop CPython, for testing the DSP
    except ImportError:
        np = None

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000     # also what the codec clocking in adafruit_nau88l21 expects
BUFFER_SIZE = 512       # pitch analysis window (32 ms)
BLOCK_SIZE = 256        # audio hop per loop pass (16 ms); BUFFER_SIZE must be 2x this

MIN_HZ = 80.0           # lowest pitch the detector looks for
MAX_HZ = 500.0          # highest pitch the detector looks for
VOICED_THRESHOLD = 0.15 # YIN threshold: lower = stricter about what counts as a note
MIN_RMS = 250           # below this input level (int16 units) treat as silence

# "native": audiodelays.GranularPitchShift (or PitchShift on older firmware)
# "python": pure-Python OLA shifter below (reference, likely too slow)
SHIFT_ENGINE = "native"
GRAIN_SIZE = 512        # native grain length in samples; bigger = smoother, more latency
GRAIN_DENSITY = 2       # native overlapping grains (1..8)

# If the output crackles steadily, flip this between 0 and 1. It picks which
# half of the double-buffered RawSample is written while the other one plays.
FEED_PHASE = 1

# Retune speed. `speed` 0..1 (0 = natural, 1 = hard snap) is mapped to the
# fraction of the remaining correction applied per 16 ms block, from
# NATURAL_RATE (about a 200 ms glide) up to 1.0 (instant).
NATURAL_RATE = 0.08
HUMANIZE_CENTS = 30     # humanize = 1 leaves up to this much of the error alone
NOTE_HYSTERESIS = 0.15  # semitones past the halfway point before switching notes

KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
SCALES = (
    ("chromatic", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)),
    ("major", (0, 2, 4, 5, 7, 9, 11)),
    ("minor", (0, 2, 3, 5, 7, 8, 10)),
)

INV_LN2 = 1.0 / math.log(2.0)


def log2(x):
    # math.log2 is not in every CircuitPython build
    return math.log(x) * INV_LN2


# ---------------------------------------------------------------------------
# Pitch detection
# ---------------------------------------------------------------------------
#
# Doug's detector summed |x[i] - x[i + offset]| over BUFFER_SIZE - offset
# samples and took the smallest sum. Two problems on real voices:
#   1. Longer offsets sum fewer samples, so they win too often and the
#      detector reads an octave (or more) low (196 Hz came back as 98 Hz).
#   2. The integer offset is too coarse for autotune: at 400 Hz one sample of
#      period is about 40 cents.
# This version keeps the same idea (difference between the waveform and a
# delayed copy of itself) with these changes:
#   - every offset compares the same number of samples (fixes the octave bias)
#   - squared differences + YIN normalisation, taking the FIRST dip under the
#     threshold rather than the global minimum (fewer octave errors)
#   - search at 8 kHz (decimated by 2), then refine at 16 kHz around the hit
#     and use parabolic interpolation for a fractional period
#   - ulab does the inner loop in C. The original pure-Python double loop is
#     ~170 x 400 = 68k operations per call, roughly a second on an RP2350.

_DEC = 2
_MIN_LAG = int(SAMPLE_RATE / MAX_HZ)            # 32 samples at 16 kHz
_MAX_LAG = int(SAMPLE_RATE / MIN_HZ)            # 200
_MAX_LAG_D = _MAX_LAG // _DEC + 1               # 101 at 8 kHz
_MIN_LAG_D = max(2, _MIN_LAG // _DEC)
_WIN_D = BUFFER_SIZE // _DEC - _MAX_LAG_D       # samples compared per lag at 8 kHz
_WIN_F = BUFFER_SIZE - _MAX_LAG - 3             # samples compared per lag at 16 kHz

_FLOAT = getattr(np, "float", None) or getattr(np, "float32", None) if np else None


def _diff_at(x, lag, width):
    d = x[0:width] - x[lag:lag + width]
    return float(np.sum(d * d))


def _diff_at_py(x, lag, width):
    total = 0.0
    for i in range(width):
        d = x[i] - x[i + lag]
        total += d * d
    return total


def detect_pitch_autocorrelation(buffer):
    """Return the fundamental in Hz of the last BUFFER_SIZE samples, or 0.0.

    `buffer` is an int16 array (or list) of BUFFER_SIZE samples.
    """
    if np is not None:
        x = np.array(buffer, dtype=_FLOAT)
        x = x - np.mean(x)
        if math.sqrt(float(np.sum(x * x)) / BUFFER_SIZE) < MIN_RMS:
            return 0.0
        xd = (x[0::2] + x[1::2]) * 0.5   # crude low-pass + decimate to 8 kHz
        diff = _diff_at
    else:
        mean = sum(buffer) / len(buffer)
        x = [s - mean for s in buffer]
        if math.sqrt(sum(s * s for s in x) / BUFFER_SIZE) < MIN_RMS:
            return 0.0
        xd = [(x[i] + x[i + 1]) * 0.5 for i in range(0, BUFFER_SIZE, 2)]
        diff = _diff_at_py

    # Cumulative-mean-normalised difference (YIN) at 8 kHz
    running = 0.0
    best_lag = 0
    best_val = 2.0
    prev = 2.0
    found = False
    for lag in range(1, _MAX_LAG_D):
        d = diff(xd, lag, _WIN_D)
        running += d
        norm = d * lag / running if running > 0 else 1.0
        if lag < _MIN_LAG_D:
            prev = norm
            continue
        if norm < VOICED_THRESHOLD:
            # walk down to the bottom of this dip, then stop
            found = True
            if norm < best_val:
                best_val, best_lag = norm, lag
            elif norm > prev:
                break
        elif found:
            break
        elif norm < best_val:
            best_val, best_lag = norm, lag
        prev = norm
    if best_lag == 0 or (not found and best_val > 0.4):
        return 0.0

    # Refine at 16 kHz around the coarse hit
    centre = best_lag * _DEC
    lo = max(_MIN_LAG, centre - 3)
    hi = min(_MAX_LAG + 1, centre + 4)
    vals = [diff(x, lag, _WIN_F) for lag in range(lo, hi)]
    i = vals.index(min(vals))
    period = float(lo + i)
    if 0 < i < len(vals) - 1:
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        denom = a - 2.0 * b + c
        if denom > 0:
            period += 0.5 * (a - c) / denom
    return SAMPLE_RATE / period


# ---------------------------------------------------------------------------
# Note snapping
# ---------------------------------------------------------------------------

def calculate_chromatic_ratio(current_hz):
    """
    Calculates the exact pitch-shift multiplier to snap to the nearest semitone
    """
    if current_hz < 50.0 or current_hz > 1000.0:  # Ignore background noise/screeches
        return 1.0

    # Step 1: Convert Hz to a fractional MIDI note number
    midi_note_fractional = 12 * log2(current_hz / 440.0) + 69

    # Step 2: Snap to the nearest whole integer chromatic note
    target_midi_note = round(midi_note_fractional)

    # Step 3: Convert the perfect target note back into Hz
    target_hz = 440.0 * (2.0 ** ((target_midi_note - 69) / 12.0))

    # Step 4: The ratio used to stretch/squash the audio buffer
    shift_ratio = target_hz / current_hz
    return shift_ratio


class NoteSnapper:
    """Same maths as calculate_chromatic_ratio, plus scales and hysteresis.

    Hysteresis keeps a note held at the boundary between two semitones from
    flickering back and forth (the warble you hear from naive autotune).
    """

    def __init__(self):
        self.allowed = SCALES[0][1]
        self.root = 0           # 0 = C, 2 = D, 9 = A ...
        self.target = None

    def set_scale(self, pitch_classes, root=None):
        self.allowed = pitch_classes
        if root is not None:
            self.root = root
        self.target = None

    def _nearest(self, midi):
        best = None
        for octave in range(int(midi // 12) - 1, int(midi // 12) + 2):
            for pc in self.allowed:
                note = octave * 12 + (pc + self.root) % 12
                if best is None or abs(note - midi) < abs(best - midi):
                    best = note
        return best

    def correction_semitones(self, current_hz):
        """Semitones to shift by (target - sung), or None when there is no note."""
        if current_hz < 50.0 or current_hz > 1000.0:
            self.target = None
            return None
        midi = 12 * log2(current_hz / 440.0) + 69
        nearest = self._nearest(midi)
        if self.target is None or nearest == self.target:
            self.target = nearest
        elif abs(midi - self.target) > abs(midi - nearest) + 2 * NOTE_HYSTERESIS:
            self.target = nearest
        elif abs(midi - self.target) > 1.5:  # jumped well away, follow at once
            self.target = nearest
        return self.target - midi


# ---------------------------------------------------------------------------
# Pure-Python OLA granular pitch shifter (SHIFT_ENGINE = "python")
# ---------------------------------------------------------------------------
#
# Two Hann-windowed read taps run through a short history buffer, half a grain
# apart. Each tap reads at `ratio` x the write speed, so its delay drifts; when
# a tap's delay wraps around the grain, its window is at zero, so the jump is
# silent. The two windows always sum to 1 (sin^2 + cos^2), which is overlap-add
# of 50%-overlapped grains, each resampled by `ratio`.
#
# The grain is kept at a whole number of detected pitch periods, so the two
# taps stay in phase with each other. Without that, mixing two copies of the
# voice 16 ms apart gives a hollow comb-filter sound even at ratio 1.0.

class OLAPitchShifter:
    WIN_LEN = 512

    def __init__(self, grain=512, history=1024):
        self.grain = float(grain)
        self.target_grain = float(grain)
        self.base_half = grain // 2
        self.size = history
        self.mask = history - 1
        self.hist = array.array("h", [0] * history)
        self.w = 0
        self.p = 0.25  # tap A grain position, 0..1 (0.25 + 0.5 offsets tap B)
        n = self.WIN_LEN
        self.win = array.array("f", [math.sin(math.pi * i / n) ** 2 for i in range(n)])

    def lock_to_period(self, period_samples):
        if period_samples and period_samples > 0:
            k = max(1, round(self.base_half / period_samples))
            self.target_grain = 2.0 * k * period_samples

    def process(self, buf, n, ratio):
        hist, mask, size, win = self.hist, self.mask, self.size, self.win
        wl = self.WIN_LEN
        g = self.grain
        # glide the grain length toward the pitch-locked length across the
        # block; changing it in one step makes both taps jump and click
        dg = (self.target_grain - g) * 0.25 / n
        dp = (1.0 - ratio) / g
        p = self.p
        w = self.w
        for i in range(n):
            hist[w] = buf[i]
            pb = p + 0.5
            if pb >= 1.0:
                pb -= 1.0
            # tap A
            r = w - p * g - 2.0 + size
            i0 = int(r)
            s0 = hist[i0 & mask]
            va = s0 + (hist[(i0 + 1) & mask] - s0) * (r - i0)
            # tap B
            r = w - pb * g - 2.0 + size
            i0 = int(r)
            s0 = hist[i0 & mask]
            vb = s0 + (hist[(i0 + 1) & mask] - s0) * (r - i0)
            out = va * win[int(p * wl)] + vb * win[int(pb * wl)]
            if out > 32767:
                out = 32767
            elif out < -32768:
                out = -32768
            buf[i] = int(out)
            w = (w + 1) & mask
            g += dg
            p += dp
            if p >= 1.0:
                p -= 1.0
            elif p < 0.0:
                p += 1.0
        self.p = p
        self.w = w
        self.grain = g


def _make_native_shifter(audiodelays, fmt, mix, grain_size, density):
    if hasattr(audiodelays, "GranularPitchShift"):
        return audiodelays.GranularPitchShift(
            semitones=0.0, mix=mix, grain_size=grain_size, density=density,
            spread=0.0, **fmt), "GranularPitchShift"
    if hasattr(audiodelays, "PitchShift"):
        return audiodelays.PitchShift(
            semitones=0.0, mix=mix, window=grain_size * 2, overlap=256, **fmt), "PitchShift"
    return None, None


class WaveReader:
    """Streams a 16-bit mono WAV from the drive one block at a time.

    Lets a sample be mixed into the autotune feed, and so be tuned like the
    voice, without loading the whole file into RAM: each 16 ms block reads
    512 bytes.
    """

    def __init__(self, path, loop=False):
        self.loop = loop
        self.done = False
        self.buf = array.array("h", [0] * BLOCK_SIZE)
        self._view = memoryview(self.buf)
        self.file = open(path, "rb")
        try:
            self.start, self.size = self._find_data()
        except Exception:
            self.file.close()
            raise
        self.left = self.size
        self.file.seek(self.start)

    def _find_data(self):
        f = self.file
        head = f.read(12)
        if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                raise ValueError("WAV file has no data")
            name, size = chunk[0:4], struct.unpack("<I", chunk[4:8])[0]
            if name == b"fmt ":
                fmt = f.read(size)
                code, channels, rate = struct.unpack("<HHI", fmt[0:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
                if code != 1 or channels != 1 or bits != 16 or rate != SAMPLE_RATE:
                    raise ValueError("needs 16-bit mono {} Hz".format(SAMPLE_RATE))
            elif name == b"data":
                return f.tell(), size // 2
            else:
                f.seek(size + (size & 1), 1)

    def read(self):
        """Fill ``buf`` with the next block. Returns how many samples."""
        if self.done:
            return 0
        if self.left <= 0:
            if not self.loop:
                self.done = True
                return 0
            self.file.seek(self.start)
            self.left = self.size
        want = min(BLOCK_SIZE, self.left)
        got = (self.file.readinto(self._view[0:want]) or 0) // 2
        self.left = self.left - got if got else 0
        return got

    def close(self):
        self.done = True
        self.file.close()


def _value(param):
    # plain number, or a synthio block (LFO / Math) that reports .value
    return getattr(param, "value", param)


def scale_by_name(name):
    for scale_name, pitch_classes in SCALES:
        if scale_name == name:
            return pitch_classes
    raise ValueError("unknown scale %r" % (name,))


def key_by_name(name):
    return KEYS.index(name.upper().replace("DB", "C#").replace("EB", "D#")
                      .replace("GB", "F#").replace("AB", "G#").replace("BB", "A#"))


class Autotune:
    """One autotune stage: mic in, corrected voice out of self.output.

    Parameters match the "autotune" effect in the preset config.json:
      key        "C".."B" (sharps or flats)
      scale      "chromatic", "major" or "minor"
      speed      0 = natural glide .. 1 = hard snap (number or synthio block)
      humanize   0..1, leaves small errors (vibrato, scoops) partly uncorrected
      mix        dry/corrected balance (number or synthio block)
      grain_size, density   native GranularPitchShift grain settings
    fmt is the shared audio format dict (sample_rate, bits_per_sample,
    samples_signed, channel_count, buffer_size). All but grain_size, density
    and engine can be changed later through the attributes / set_key_scale().
    """

    def __init__(self, mic, fmt, key="C", scale="chromatic", speed=1.0,
                 humanize=0.0, mix=1.0, grain_size=GRAIN_SIZE,
                 density=GRAIN_DENSITY, engine=SHIFT_ENGINE):
        import audiocore

        self.mic = mic
        self.enabled = True
        self.speed = speed
        self.humanize = humanize
        self.snapper = NoteSnapper()
        self.set_key_scale(key, scale)
        self.hz = 0.0
        self.cents = None        # how far the sung note is from its target
        self.correction = 0.0    # semitones currently applied
        self._target = 0.0
        self._window = array.array("h", [0] * BUFFER_SIZE)
        self._block = array.array("h", [0] * BLOCK_SIZE)
        self._half = 0
        # Mic level, 0 or 1, applied before pitch detection. Used when
        # samples are mixed into this stage, so the paddle can mute the mic
        # without muting the sample.
        self.gate = 1
        # The WaveReader being mixed in, or None.
        self.sample = None
        self._zeros = array.array("h", [0] * BLOCK_SIZE)

        # Python writes each processed block into the half of this buffer
        # that is not playing; the audio pipeline loops over it.
        self._out = array.array("h", [0] * (BLOCK_SIZE * 2))
        self.feed = audiocore.RawSample(
            self._out, sample_rate=fmt["sample_rate"], single_buffer=False)

        self.shifter = None
        self.shifter_name = None
        if engine == "native":
            import audiodelays
            self.shifter, self.shifter_name = _make_native_shifter(
                audiodelays, fmt, mix, grain_size, density)
        self.ola = None
        if self.shifter is None:
            self.ola = OLAPitchShifter(grain=grain_size)
            self.shifter_name = "python OLA"
            self.output = self.feed
        else:
            self.output = self.shifter
        self._started = False

    @property
    def mix(self):
        return self.shifter.mix if self.shifter is not None else 1.0

    @mix.setter
    def mix(self, value):
        if self.shifter is not None:
            self.shifter.mix = value

    def set_key_scale(self, key="C", scale="chromatic"):
        self.snapper.set_scale(scale_by_name(scale), key_by_name(key))

    def play_sample(self, path, loop=False):
        """Mix the WAV at ``path`` into the voice from the next block on, so
        it is tuned and goes through the rest of the chain. Replaces any
        sample already playing."""
        self.stop_sample()
        self.sample = WaveReader(path, loop)

    def stop_sample(self):
        if self.sample is not None:
            self.sample.close()
            self.sample = None

    def start(self):
        """Wire the feed into the shifter. Call after the downstream chain is
        wired, mirroring Adafruit's demo where the mic is connected last."""
        if self.shifter is not None and not self._started:
            self.shifter.play(self.feed, loop=True)
        self._started = True

    def step(self, speed_override=None):
        """Process one block. Blocks for BLOCK_SIZE samples of audio."""
        block = self._block
        got = self.mic.record(block, BLOCK_SIZE)
        for i in range(got, BLOCK_SIZE):
            block[i] = 0
        if not self.gate:
            block[0:BLOCK_SIZE] = self._zeros
        sample = self.sample
        if sample is not None:
            extra, n = sample.buf, sample.read()
            for i in range(n):
                value = block[i] + extra[i]
                block[i] = 32767 if value > 32767 else (-32768 if value < -32768 else value)
            if sample.done:
                self.stop_sample()
        window = self._window
        window[0:BLOCK_SIZE] = window[BLOCK_SIZE:BUFFER_SIZE]
        window[BLOCK_SIZE:BUFFER_SIZE] = block

        self.cents = None
        self.hz = 0.0
        if self.enabled:
            self.hz = detect_pitch_autocorrelation(window)
            semis = self.snapper.correction_semitones(self.hz) if self.hz > 0 else None
            if semis is not None:
                self.cents = -semis * 100
                # humanize: leave up to HUMANIZE_CENTS of the error alone
                slack = _value(self.humanize) * HUMANIZE_CENTS / 100.0
                if semis > slack:
                    semis -= slack
                elif semis < -slack:
                    semis += slack
                else:
                    semis = 0.0
                self._target = semis
            else:
                self._target *= 0.9   # silence / noise: relax back toward no shift
        else:
            self._target = 0.0
            self.snapper.target = None

        speed = _value(self.speed) if speed_override is None else speed_override
        speed = min(1.0, max(0.0, speed))
        rate = NATURAL_RATE + (1.0 - NATURAL_RATE) * speed * speed
        self.correction += (self._target - self.correction) * rate

        if self.ola is not None:
            if self.hz > 0:
                self.ola.lock_to_period(SAMPLE_RATE / self.hz)
            self.ola.process(block, BLOCK_SIZE, 2.0 ** (self.correction / 12.0))
        else:
            self.shifter.semitones = self.correction
        start = (self._half ^ FEED_PHASE) * BLOCK_SIZE
        self._out[start:start + BLOCK_SIZE] = block
        self._half ^= 1
        return got
