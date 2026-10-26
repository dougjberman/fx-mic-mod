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
import time

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

# The rate the mic runs at. code.py calls set_rate() with config.json's
# "sample_rate" before building anything; the sizes below are for 16 kHz and
# set_rate() scales them, so a block stays 16 ms and the window 32 ms.
SAMPLE_RATE = 16000
RATES = (16000, 24000, 32000, 48000)  # multiples of 8 kHz, so the detector can decimate evenly
BUFFER_SIZE = 512       # pitch analysis window (32 ms)
BLOCK_SIZE = 256        # audio hop per loop pass (16 ms); BUFFER_SIZE must be 2x this

MIN_HZ = 80.0           # lowest pitch the detector looks for
MAX_HZ = 500.0          # highest pitch the detector looks for
VOICED_THRESHOLD = 0.15 # YIN threshold: lower = stricter about what counts as a note
HARMONY_SETTLE = 3      # detections a new note must hold before a key chord moves to it (~100 ms)
DETECT_EVERY = 2        # find the pitch every N blocks (16 ms each); the chip can't do every one
MIN_RMS = 250           # below this input level (int16 units) treat as silence

# Noise gate on the voice, before it is shifted and before any effect after
# this stage. Distortion with tens of dB of pre-gain otherwise turns the
# mic's background hiss between phrases into loud noise. The gate opens when
# a block's RMS (int16 units) passes GATE_OPEN, and closes once it has stayed
# under GATE_CLOSE for GATE_HOLD_MS. 0 turns the gate off.
GATE_OPEN = 120         # about -49 dBFS
GATE_CLOSE = 80         # about -52 dBFS
GATE_HOLD_MS = 200

# "native": audiodelays.GranularPitchShift (or PitchShift on older firmware)
# "python": pure-Python OLA shifter below (reference, likely too slow)
SHIFT_ENGINE = "native"
GRAIN_SIZE = 512        # native grain length in samples (at 16 kHz); bigger = smoother, more latency
GRAIN_DENSITY = 2       # native overlapping grains (1..8)

# How processed blocks reach the pitch shifter. 0 or 1 is a double buffer,
# written half by half; the value picks which half is written while the other
# plays. Typing f and Enter in the serial console flips it live, to test. None keeps the feed as a two-block
# window instead, oldest block first; on a Ting that crackled on every load.
FEED_PHASE = 1
# On a Ting the right FEED_PHASE changed from one preset load to the next, and
# flipping it cured a crackling load. The chain renders in bursts, two halves
# at once, and the halves must hold the older block first; which half that is
# depends on where the burst falls between our writes, which is set afresh at
# each load. With FEED_SYNC, start() times the burst (the gap it leaves in a
# tight Python loop) against a mic block and picks the half to match.
FEED_SYNC = True
BLOCK_NS = 16000000     # one BLOCK_SIZE of audio, at any rate

# Retune speed. `speed` 0..1 (0 = natural, 1 = hard snap) is mapped to the
# fraction of the remaining correction applied per 16 ms block, from
# NATURAL_RATE (about a 200 ms glide) up to 1.0 (instant).
NATURAL_RATE = 0.08
HUMANIZE_CENTS = 30     # humanize = 1 leaves up to this much of the error alone
NOTE_HYSTERESIS = 0.3   # semitones past the halfway point before switching notes

KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
SCALES = (
    ("chromatic", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)),
    ("major", (0, 2, 4, 5, 7, 9, 11)),
    ("minor", (0, 2, 3, 5, 7, 8, 10)),
)

INV_LN2 = 1.0 / math.log(2.0)

# Harmonizer chords: name -> semitones above the sung note, which is the root
# and is included as 0. Four notes at most (the voice plus three harmony
# voices), so five-note chords leave out their 5th, the usual jazz voicing,
# and 11ths and 13ths keep only their colour tones.
CHORDS = (
    ("octave", (0, 12)),
    ("maj3", (0, 4)),
    ("min3", (0, 3)),
    ("fifth", (0, 7)),
    ("power", (0, 7, 12)),
    ("major", (0, 4, 7)),
    ("minor", (0, 3, 7)),
    ("dim", (0, 3, 6)),
    ("aug", (0, 4, 8)),
    ("sus2", (0, 2, 7)),
    ("sus4", (0, 5, 7)),
    ("6", (0, 4, 7, 9)),
    ("m6", (0, 3, 7, 9)),
    ("6/9", (0, 4, 9, 14)),
    ("maj7", (0, 4, 7, 11)),
    ("7", (0, 4, 7, 10)),
    ("m7", (0, 3, 7, 10)),
    ("m_maj7", (0, 3, 7, 11)),
    ("m7b5", (0, 3, 6, 10)),
    ("dim7", (0, 3, 6, 9)),
    ("7sus4", (0, 5, 7, 10)),
    ("aug7", (0, 4, 8, 10)),
    ("aug_maj7", (0, 4, 8, 11)),
    ("add9", (0, 4, 7, 14)),
    ("m_add9", (0, 3, 7, 14)),
    ("add11", (0, 4, 7, 17)),
    ("maj9", (0, 4, 11, 14)),
    ("9", (0, 4, 10, 14)),
    ("m9", (0, 3, 10, 14)),
    ("7b9", (0, 4, 10, 13)),
    ("7#9", (0, 4, 10, 15)),
    ("maj7#11", (0, 4, 11, 18)),
    ("11", (0, 10, 14, 17)),
    ("m11", (0, 3, 10, 17)),
    ("13", (0, 4, 10, 21)),
    ("maj13", (0, 4, 11, 21)),
    ("m13", (0, 3, 10, 21)),
    ("quartal", (0, 5, 10, 15)),
)
# Chords built from the autotune key instead: scale steps above the note
# autotune snapped to, so singing E in C major gives E minor, and F gives F
# major. They need a major or minor scale to say which 3rd is meant; with
# "Every note" (chromatic) they fall back to the key's major scale, and the
# editor offers them only with a major or minor key.
KEY_CHORDS = (
    ("key_third", (0, 2)),
    ("key_triad", (0, 2, 4)),
    ("key_seventh", (0, 2, 4, 6)),
)
# A key chord sung on a note outside the scale falls back to this shape.
OFF_KEY_SHAPE = (0, 4, 7, 10)


def chord_by_name(name):
    """(tones, is_key_chord) for a harmonizer chord name."""
    for chord_name, tones in CHORDS:
        if chord_name == name:
            return tones, False
    for chord_name, steps in KEY_CHORDS:
        if chord_name == name:
            return steps, True
    raise ValueError("unknown harmonizer chord %r" % (name,))


def max_inversion(tones):
    """Highest inversion a chord allows. Chords with an octave in them
    (octave, power) only come in root position."""
    return 0 if any(t % 12 == 0 for t in tones[1:]) else len(tones) - 1


def voicing(tones, inversion=0, octave=0):
    """Harmony intervals around the sung note for a chord in an inversion.

    Inversion n lifts the lowest n chord tones an octave, then drops the
    whole chord an octave, so the sung note is still in it and the harmony
    moves below the voice: C major sung on C gives E, G above (root
    position), E, G below (1st), or G below and E above (2nd). The sung note
    itself is left out; ``octave`` moves the harmony by whole octaves.
    """
    tones = list(tones)
    for _ in range(inversion):
        tones.append(tones.pop(0) + 12)
    if inversion:
        tones = [t - 12 for t in tones]
    tones.remove(0)
    return [t + 12 * octave for t in tones]


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
#   - search at 4 kHz, then refine at 8 kHz around the hit and use parabolic
#     interpolation for a fractional period
#   - ulab does the inner loop in C. The original pure-Python double loop is
#     ~170 x 400 = 68k operations per call, roughly a second on an RP2350.
#   - On a real Ting every ulab operation costs ~0.3 us per element, and the
#     first version (full 16 kHz refine, 8 kHz search) took 14 ms of a 16 ms
#     block on its own, more with effects running. So the search runs at
#     4 kHz and the refine at 8 kHz, and Autotune only detects every
#     DETECT_EVERY blocks.

_COARSE = 4     # search at SAMPLE_RATE / _COARSE = 4 kHz
_FINE = 2       # refine at 8 kHz


def _set_detector():
    global _COARSE, _FINE, _RATE_F, _MIN_LAG_F, _MAX_LAG_F, _N_D
    global _MAX_LAG_D, _MIN_LAG_D, _WIN_D, _WIN_F
    _COARSE = SAMPLE_RATE // 4000
    _FINE = SAMPLE_RATE // 8000
    _RATE_F = SAMPLE_RATE / _FINE
    _MIN_LAG_F = int(_RATE_F / MAX_HZ)              # 16 at 8 kHz
    _MAX_LAG_F = int(_RATE_F / MIN_HZ)              # 100
    _N_D = BUFFER_SIZE // _COARSE                   # 128 samples at 4 kHz
    _MAX_LAG_D = _MAX_LAG_F * _FINE // _COARSE + 1  # 51
    _MIN_LAG_D = max(2, _MIN_LAG_F * _FINE // _COARSE)
    _WIN_D = _N_D - _MAX_LAG_D                      # samples compared per lag at 4 kHz
    _WIN_F = BUFFER_SIZE // _FINE - _MAX_LAG_F - 3  # samples compared per lag at 8 kHz


_set_detector()


def _rms(block):
    """RMS of an int16 block, in int16 units."""
    if np is not None:
        return float(np.std(np.frombuffer(block, dtype=np.int16)))
    total = 0
    for i in range(0, len(block), 4):
        total += block[i] * block[i]
    return math.sqrt(total * 4 / len(block))


def set_rate(rate):
    """Run at ``rate`` Hz, one of RATES. Call before making an Autotune."""
    global SAMPLE_RATE, BUFFER_SIZE, BLOCK_SIZE, GRAIN_SIZE
    if rate not in RATES:
        raise ValueError("sample_rate must be one of {}".format(RATES))
    scale = rate // 16000 if rate % 16000 == 0 else rate / 16000
    BLOCK_SIZE = int(256 * scale)
    BUFFER_SIZE = 2 * BLOCK_SIZE
    GRAIN_SIZE = int(512 * scale)
    SAMPLE_RATE = rate
    _set_detector()


_FLOAT = getattr(np, "float", None) or getattr(np, "float32", None) if np else None
_CONVOLVE = np is not None and hasattr(np, "convolve")


def _diff_at(x, lag, width):
    d = x[0:width] - x[lag:lag + width]
    return float(np.sum(d * d))


def _diff_at_py(x, lag, width):
    total = 0.0
    for i in range(width):
        d = x[i] - x[i + lag]
        total += d * d
    return total


def _halve(x):
    """Crude low-pass and decimate by 2."""
    if np is not None:
        return (x[0::2] + x[1::2]) * 0.5
    return [(x[i] + x[i + 1]) * 0.5 for i in range(0, len(x) - 1, 2)]


def _third(x):
    """Crude low-pass and decimate by 3."""
    if np is not None:
        return (x[0::3] + x[1::3] + x[2::3]) * (1.0 / 3.0)
    return [(x[i] + x[i + 1] + x[i + 2]) / 3.0 for i in range(0, len(x) - 2, 3)]


def _decimate(x, factor):
    """Decimate by ``factor`` (1, 2, 3, 4, 6 or 8) in halving and third steps."""
    while factor > 1:
        if factor % 2 == 0:
            x, factor = _halve(x), factor // 2
        else:
            x, factor = _third(x), factor // 3
    return x


def detect_pitch_autocorrelation(buffer):
    """Return the fundamental in Hz of the last BUFFER_SIZE samples, or 0.0.

    `buffer` is an int16 array (or list) of BUFFER_SIZE samples.
    """
    if np is not None:
        xf = _decimate(np.array(buffer, dtype=_FLOAT), _FINE)   # 8 kHz
        xd = _halve(xf)                                         # 4 kHz
        xd = xd - np.mean(xd)
        sq = (xd * xd).tolist()
        diff = _diff_at
    else:
        xf = _decimate(list(buffer), _FINE)
        xd = _halve(xf)
        mean = sum(xd) / len(xd)
        xd = [s - mean for s in xd]
        sq = [s * s for s in xd]
        diff = _diff_at_py
    if math.sqrt(sum(sq) / _N_D) < MIN_RMS:
        return 0.0

    # Cumulative-mean-normalised difference (YIN) at 4 kHz. With ulab, the
    # difference at every lag comes from one convolution:
    #   d(lag) = sum x[i]^2 + sum x[i+lag]^2 - 2 sum x[i] x[i+lag]
    # so the loop below only does scalar sums.
    fast = np is not None and _CONVOLVE
    if fast:
        cross = np.convolve(xd, xd[_WIN_D - 1::-1])
        cross = cross[_WIN_D - 1:_WIN_D - 1 + _MAX_LAG_D].tolist()
        energy0 = sum(sq[0:_WIN_D])
        energy = energy0
    running = 0.0
    best_lag = 0
    best_val = 2.0
    prev = 2.0
    found = False
    for lag in range(1, _MAX_LAG_D):
        if fast:
            energy += sq[lag + _WIN_D - 1] - sq[lag - 1]
            d = energy0 + energy - 2.0 * cross[lag]
        else:
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

    # Refine at 8 kHz around the coarse hit
    centre = best_lag * _COARSE // _FINE
    lo = max(2, centre - 2)
    hi = min(_MAX_LAG_F + 1, centre + 3)
    vals = [diff(xf, lag, _WIN_F) for lag in range(lo, hi)]
    i = vals.index(min(vals))
    period = float(lo + i)
    if 0 < i < len(vals) - 1:
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        denom = a - 2.0 * b + c
        if denom > 0:
            period += 0.5 * (a - c) / denom
    return _RATE_F / period


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
        self._leap = None       # a far note heard once, waiting to be heard again

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
        elif abs(midi - self.target) > 1.5:
            # Jumped well away: follow once the same note is heard twice in a
            # row, so a one-off octave error doesn't swap the harmony chord.
            if self._leap == nearest:
                self.target = nearest
                self._leap = None
            else:
                self._leap = nearest
            return self.target - midi
        self._leap = None
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



def _feed_sample(buffer, fmt):
    """The RawSample the shifters read processed blocks from (see FEED_PHASE)."""
    import audiocore
    return audiocore.RawSample(buffer, sample_rate=fmt["sample_rate"],
                               single_buffer=FEED_PHASE is None)

class Autotune:
    """One autotune stage: mic in, corrected voice out of self.output.

    Parameters match the "autotune" effect in the preset config.json:
      key        "C".."B" (sharps or flats)
      scale      "chromatic", "major" or "minor"
      speed      0 = natural glide .. 1 = hard snap (number or synthio block)
      humanize   0..1, leaves small errors (vibrato, scoops) partly uncorrected
      mix        dry/corrected balance (number or synthio block)
      grain_size, density   native GranularPitchShift grain settings
    and, from a "harmonizer" effect in the same preset:
      harmony    a chord name from CHORDS or KEY_CHORDS, or None
      inversion  0 = root position, 1 = 1st inversion ...
      harmony_octave   moves the harmony voices by whole octaves
      harmony_level    harmony volume against the voice (number or block)
      correct    False runs the harmonizer without retuning the voice
    fmt is the shared audio format dict (sample_rate, bits_per_sample,
    samples_signed, channel_count, buffer_size). All but grain_size, density
    and engine can be changed later through the attributes / set_key_scale().
    """

    def __init__(self, mic, fmt, key="C", scale="chromatic", speed=1.0,
                 humanize=0.0, mix=1.0, grain_size=None,
                 density=GRAIN_DENSITY, engine=SHIFT_ENGINE, harmony=None,
                 inversion=0, harmony_octave=0, harmony_level=0.7, correct=True):
        import audiocore

        if grain_size is None:
            grain_size = GRAIN_SIZE
        self.mic = mic
        self.enabled = True
        self.speed = speed
        self.humanize = humanize
        self.correct = correct
        self.snapper = NoteSnapper()
        self.set_key_scale(key, scale)
        self.hz = 0.0
        self.cents = None        # how far the sung note is from its target
        self.correction = 0.0    # semitones currently applied
        self._target = 0.0
        self._window = array.array("h", [0] * BUFFER_SIZE)
        self._block = array.array("h", [0] * BLOCK_SIZE)
        self._half = 0
        self._turn = 0
        self.busy_ms = 0.0
        # Mic level, 0 or 1, applied before pitch detection. Used when
        # samples are mixed into this stage, so the paddle can mute the mic
        # without muting the sample.
        self.gate = 1
        # The WaveReader being mixed in, or None, and its level in 256ths.
        self.sample = None
        self.sample_gain = 256
        self._zeros = array.array("h", [0] * BLOCK_SIZE)
        self.gate_open = False
        self._gate_hold = 0
        self._gate_blocks = max(1, GATE_HOLD_MS * SAMPLE_RATE // (1000 * BLOCK_SIZE))

        # Python writes each processed block into the half of this buffer
        # that is not playing; the audio pipeline loops over it.
        self._out = array.array("h", [0] * (BLOCK_SIZE * 2))
        view = memoryview(self._out)
        self._front, self._back = view[:BLOCK_SIZE], view[BLOCK_SIZE:]
        self.feed = _feed_sample(self._out, fmt)

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

        # Harmonizer: one more shifter per harmony voice, each fed the same
        # blocks through its own RawSample over self._out, and all of them
        # summed with the corrected voice on a mixer.
        self.voices = []
        self._feeds = []
        self.mixer = None
        self.harmony_level = harmony_level
        self._level = None
        self._intervals = []
        self._key_chord = False
        # The note the key chord is built on, and a new note waiting to
        # take over (see _chord_intervals).
        self._chord_note = None
        self._chord_next = None
        self._chord_count = 0
        if harmony is not None:
            tones, self._key_chord = chord_by_name(harmony)
            if not 0 <= inversion <= max_inversion(tones):
                raise ValueError("harmonizer %s has no inversion %r" % (harmony, inversion))
            self._tones = tones
            self._inversion = inversion
            self._octave = harmony_octave
            if not self._key_chord:
                self._intervals = voicing(tones, inversion, harmony_octave)
            if self.shifter is None:
                print("harmonizer off: it needs audiodelays.GranularPitchShift")
            else:
                import audiodelays
                import audiomixer
                count = len(tones) - 1
                self.mixer = audiomixer.Mixer(voice_count=count + 1, **fmt)
                for _ in range(count):
                    self._feeds.append(_feed_sample(self._out, fmt))
                    self.voices.append(_make_native_shifter(
                        audiodelays, fmt, 1.0, grain_size, density)[0])
                if self._key_chord:
                    self._intervals = voicing(OFF_KEY_SHAPE[:count + 1], inversion, harmony_octave)
                self.output = self.mixer

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
            if self.mixer is not None:
                self.mixer.voice[0].play(self.shifter, loop=True)
                for index, voice in enumerate(self.voices):
                    self.mixer.voice[index + 1].play(voice, loop=True)
                self._set_levels()
                for voice, feed in zip(self.voices, self._feeds):
                    voice.play(feed, loop=True)
            self.shifter.play(self.feed, loop=True)
            if FEED_SYNC and FEED_PHASE is not None:
                self._sync_feed()
        self._started = True

    def _sync_feed(self):
        """Set ``_half`` so a burst always follows a write to the second half
        (see FEED_SYNC). Takes about 50 ms, once per load."""
        now = time.monotonic_ns
        # The longest gap in a tight loop over two burst periods is the burst.
        began = prev = now()
        gap = burst = 0
        while prev - began < 2 * BLOCK_NS + BLOCK_NS // 4:
            t = now()
            if t - prev > gap:
                gap, burst = t - prev, prev
            prev = t
        # Then line up on a fresh mic block: a read that had to wait for its
        # samples returns on a block boundary, where step()'s writes land.
        block = self._block
        for _ in range(6):
            t = now()
            self.mic.record(block, BLOCK_SIZE)
            edge = now()
            if edge - t > BLOCK_NS // 2:
                break
        # step() writes a block only after gating it, so time that too: a
        # burst that starts between the block edge and the write holds the
        # write back until it ends (on a Ting one started 1.1 ms after it).
        self._noise_gate(block)
        wrote = now() - edge
        # Writes land at edge + wrote + k blocks. The burst repeats every two
        # blocks, so the write just before it is block k, with k of this parity.
        k = ((burst - edge - wrote) // BLOCK_NS) % 2
        # Block k must land in the second half; block 0 is the one written here.
        self._half = (1 ^ k) ^ FEED_PHASE
        self._feed_block(self._zeros)
        print("feed sync: burst {:.1f} ms after a block, {:.1f} ms long; write at {:.1f} ms".format(
            ((burst - edge) % (2 * BLOCK_NS)) / 1e6, gap / 1e6, wrote / 1e6))

    def deinit(self):
        self.stop_sample()
        for part in [self.mixer, self.shifter] + self.voices + self._feeds + [self.feed]:
            if part is not None:
                part.deinit()

    def _set_levels(self):
        level = min(1.0, max(0.0, _value(self.harmony_level)))
        if level == self._level:
            return
        self._level = level
        # Keep the sum about as loud as the voice alone. The harmony voices
        # are copies of the voice, so they add up nearly in phase, not like
        # unrelated sounds: on the mic, a 1/sqrt gain here still came out
        # about 7 dB louder with the harmony fully in.
        gain = 1.0 / (1.0 + len(self.voices) * level)
        self.mixer.voice[0].level = gain
        for index in range(len(self.voices)):
            self.mixer.voice[index + 1].level = gain * level

    def _chord_intervals(self, note):
        """Key chord intervals around the snapped ``note``. The chord only
        moves to a new note once it has held for HARMONY_SETTLE detections;
        until then the harmony keeps singing the last chord's tones, so a
        passing or wavering note doesn't flip the whole chord.

        Notes are compared by pitch class: the detector sometimes reads a
        voice an octave (or two) out for a moment, and that is the same chord.
        The held chord keeps the octave it was heard in, so a real move from
        it stays the size it was; only an offset of an octave or more, which
        can only be such a misreading, is folded back, so the harmony doesn't
        jump an octave or two with it."""
        pc = note % 12
        held = self._chord_note
        if held is None or pc == held % 12:
            if held is None:
                self._chord_note = note
            self._chord_next = None
        elif pc == self._chord_next:
            self._chord_count += 1
            if self._chord_count >= HARMONY_SETTLE:
                self._chord_note = note
                self._chord_next = None
        else:
            self._chord_next = pc
            self._chord_count = 1
        offset = self._chord_note - note
        while offset > 11:
            offset -= 12
        while offset < -11:
            offset += 12
        return [offset + i for i in self._key_intervals(note + offset)]

    def _key_intervals(self, note):
        """Harmony intervals for a key chord on the snapped MIDI note."""
        allowed = self.snapper.allowed
        if len(allowed) == 12:
            allowed = SCALES[1][1]
        pcs = sorted((pc + self.snapper.root) % 12 for pc in allowed)
        pc = note % 12
        steps = self._tones
        if pc in pcs:
            i, n = pcs.index(pc), len(pcs)
            tones = [pcs[(i + s) % n] - pc + 12 * ((i + s) // n) for s in steps]
        else:
            tones = OFF_KEY_SHAPE[:len(steps)]
        return voicing(tones, self._inversion, self._octave)

    def _feed_block(self, block):
        if FEED_PHASE is None:
            self._front[:] = self._back
            self._back[:] = block
            return
        start = (self._half ^ FEED_PHASE) * BLOCK_SIZE
        self._out[start:start + BLOCK_SIZE] = block
        self._half ^= 1

    def _noise_gate(self, block):
        """Silence ``block`` in place between phrases (see GATE_OPEN). It
        costs the same every pass, so the feed write stays on time."""
        if not GATE_OPEN:
            return
        level = _rms(block)
        if level >= GATE_OPEN:
            self.gate_open = True
            self._gate_hold = self._gate_blocks
        elif level < GATE_CLOSE and self.gate_open:
            self._gate_hold -= 1
            if self._gate_hold <= 0:
                self.gate_open = False
        if not self.gate_open:
            block[0:BLOCK_SIZE] = self._zeros

    def step(self, speed_override=None):
        """Process one block. Blocks for BLOCK_SIZE samples of audio."""
        block = self._block
        got = self.mic.record(block, BLOCK_SIZE)
        began = time.monotonic_ns()
        for i in range(got, BLOCK_SIZE):
            block[i] = 0
        if not self.gate:
            block[0:BLOCK_SIZE] = self._zeros
        sample = self.sample
        if sample is not None:
            extra, n, gain = sample.buf, sample.read(), self.sample_gain
            for i in range(n):
                value = block[i] + ((extra[i] * gain) >> 8)
                block[i] = 32767 if value > 32767 else (-32768 if value < -32768 else value)
            if sample.done:
                self.stop_sample()
        # Hand the block to the pitch shifter straight away, at the same
        # moment every pass. Writing it after the pitch search made the write
        # land at a different point in the playing buffer on detect and
        # non-detect passes, sometimes in the half being played: crackle.
        # The shift amount set below applies from the next grain. (The Python
        # OLA fallback shifts the block itself, so it still writes at the end.)
        self._noise_gate(block)
        if self.ola is None:
            self._feed_block(block)
        window = self._window
        window[0:BLOCK_SIZE] = window[BLOCK_SIZE:BUFFER_SIZE]
        window[BLOCK_SIZE:BUFFER_SIZE] = block

        # Without retuning, only a key chord needs to know the note. Pitch
        # is found every DETECT_EVERY blocks; between, the last target holds.
        self._turn = (self._turn + 1) % DETECT_EVERY
        if not (self.enabled and (self.correct or self._key_chord)):
            self.cents = None
            self.hz = 0.0
            self._target = 0.0
            self.snapper.target = None
        elif self._turn == 0:
            self.cents = None
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
                # silence / noise: relax back toward no shift
                self._target *= 0.9 ** DETECT_EVERY
        if not self.correct:
            self._target = 0.0

        speed = _value(self.speed) if speed_override is None else speed_override
        speed = min(1.0, max(0.0, speed))
        rate = NATURAL_RATE + (1.0 - NATURAL_RATE) * speed * speed
        self.correction += (self._target - self.correction) * rate

        if self.ola is not None:
            if self.hz > 0:
                self.ola.lock_to_period(SAMPLE_RATE / self.hz)
            self.ola.process(block, BLOCK_SIZE, 2.0 ** (self.correction / 12.0))
            self._feed_block(block)
        else:
            self.shifter.semitones = self.correction
        if self.voices:
            if self._key_chord and self.snapper.target is not None and self._turn == 0:
                self._intervals = self._chord_intervals(self.snapper.target)
            for voice, interval in zip(self.voices, self._intervals):
                voice.semitones = self.correction + interval
            self._set_levels()
        # Python time this block took, in ms. Over ~16 ms (one block of audio)
        # and the mic overflows; code.py prints it with each overflow.
        self.busy_ms = (time.monotonic_ns() - began) / 1e6
        return got



def bench(hz=150.0, runs=5):
    """Time the pitch search on this chip, for the serial console:
    >>> import autotune; autotune.bench()"""
    buf = array.array("h", [int(6000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE)) for i in range(BUFFER_SIZE)])
    best = None
    for _ in range(runs):
        t = time.monotonic_ns()
        found = detect_pitch_autocorrelation(buf)
        ms = (time.monotonic_ns() - t) / 1e6
        best = ms if best is None or ms < best else best
    print("ulab: {}  detection: {:.2f} ms  found {:.1f} Hz for {:.1f}".format(np is not None, best, found, hz))
