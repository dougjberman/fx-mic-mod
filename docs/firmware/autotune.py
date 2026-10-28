













import array
import math
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
RATES = (16000, 24000, 32000, 48000)
BUFFER_SIZE = 512
BLOCK_SIZE = 256

MIN_HZ = 80.0
MAX_HZ = 500.0
VOICED_THRESHOLD = 0.15
HARMONY_SETTLE = 3
DETECT_EVERY = 2
MIN_RMS = 250






GATE_OPEN = 120
GATE_CLOSE = 80
GATE_HOLD_MS = 200



SHIFT_ENGINE = "native"
GRAIN_SIZE = 512
GRAIN_DENSITY = 2





FEED_PHASE = 0









PROBE_RATE = 62.5 / 64










RENDER_GUARD_NS = 2000000
RENDER_NEAR_NS = 4000000
RENDER_HYST_NS = 1500000












FEED_SYNC = True
BLOCK_NS = 16000000


ANALYSE_SPARE_NS = 1000000




NATURAL_RATE = 0.08
HUMANIZE_CENTS = 30
NOTE_HYSTERESIS = 0.3

KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
SCALES = (
    ("chromatic", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)),
    ("major", (0, 2, 4, 5, 7, 9, 11)),
    ("minor", (0, 2, 3, 5, 7, 8, 10)),
)

INV_LN2 = 1.0 / math.log(2.0)





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





KEY_CHORDS = (
    ("key_third", (0, 2)),
    ("key_triad", (0, 2, 4)),
    ("key_seventh", (0, 2, 4, 6)),
)

OFF_KEY_SHAPE = (0, 4, 7, 10)


DOUBLES = (("above", 12), ("below", -12))


def chord_by_name(name):
    0
    for chord_name, tones in CHORDS:
        if chord_name == name:
            return tones, False
    for chord_name, steps in KEY_CHORDS:
        if chord_name == name:
            return steps, True
    raise ValueError("unknown harmonizer chord %r" % (name,))


def max_inversion(tones):
    0

    return 0 if any(t % 12 == 0 for t in tones[1:]) else len(tones) - 1


def voicing(tones, inversion=0, octave=0):
    0







    tones = list(tones)
    for _ in range(inversion):
        tones.append(tones.pop(0) + 12)
    if inversion:
        tones = [t - 12 for t in tones]
    tones.remove(0)
    return [t + 12 * octave for t in tones]


def log2(x):

    return math.log(x) * INV_LN2



























_COARSE = 4
_FINE = 2


def _set_detector():
    global _COARSE, _FINE, _RATE_F, _MIN_LAG_F, _MAX_LAG_F, _N_D
    global _MAX_LAG_D, _MIN_LAG_D, _WIN_D, _WIN_F
    _COARSE = SAMPLE_RATE // 4000
    _FINE = SAMPLE_RATE // 8000
    _RATE_F = SAMPLE_RATE / _FINE
    _MIN_LAG_F = int(_RATE_F / MAX_HZ)
    _MAX_LAG_F = int(_RATE_F / MIN_HZ)
    _N_D = BUFFER_SIZE // _COARSE
    _MAX_LAG_D = _MAX_LAG_F * _FINE // _COARSE + 1
    _MIN_LAG_D = max(2, _MIN_LAG_F * _FINE // _COARSE)
    _WIN_D = _N_D - _MAX_LAG_D
    _WIN_F = BUFFER_SIZE // _FINE - _MAX_LAG_F - 3


_set_detector()


def _rms(block):
    0
    if np is not None:
        return float(np.std(np.frombuffer(block, dtype=np.int16)))
    total = 0
    for i in range(0, len(block), 4):
        total += block[i] * block[i]
    return math.sqrt(total * 4 / len(block))


def set_rate(rate):
    0
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
    0
    if np is not None:
        return (x[0::2] + x[1::2]) * 0.5
    return [(x[i] + x[i + 1]) * 0.5 for i in range(0, len(x) - 1, 2)]


def _third(x):
    0
    if np is not None:
        return (x[0::3] + x[1::3] + x[2::3]) * (1.0 / 3.0)
    return [(x[i] + x[i + 1] + x[i + 2]) / 3.0 for i in range(0, len(x) - 2, 3)]


def _decimate(x, factor):
    0
    while factor > 1:
        if factor % 2 == 0:
            x, factor = _halve(x), factor // 2
        else:
            x, factor = _third(x), factor // 3
    return x


def detect_pitch_autocorrelation(buffer):
    0



    if np is not None:
        xf = _decimate(np.array(buffer, dtype=_FLOAT), _FINE)
        xd = _halve(xf)
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






def calculate_chromatic_ratio(current_hz):
    0


    if current_hz < 50.0 or current_hz > 1000.0:
        return 1.0


    midi_note_fractional = 12 * log2(current_hz / 440.0) + 69


    target_midi_note = round(midi_note_fractional)


    target_hz = 440.0 * (2.0 ** ((target_midi_note - 69) / 12.0))


    shift_ratio = target_hz / current_hz
    return shift_ratio


class NoteSnapper:
    0





    def __init__(self):
        self.allowed = SCALES[0][1]
        self.root = 0
        self.target = None
        self._leap = None

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
        0
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


            if self._leap == nearest:
                self.target = nearest
                self._leap = None
            else:
                self._leap = nearest
            return self.target - midi
        self._leap = None
        return self.target - midi
















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
        self.p = 0.25
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


        dg = (self.target_grain - g) * 0.25 / n
        dp = (1.0 - ratio) / g
        p = self.p
        w = self.w
        for i in range(n):
            hist[w] = buf[i]
            pb = p + 0.5
            if pb >= 1.0:
                pb -= 1.0

            r = w - p * g - 2.0 + size
            i0 = int(r)
            s0 = hist[i0 & mask]
            va = s0 + (hist[(i0 + 1) & mask] - s0) * (r - i0)

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
    0










    def __init__(self, path, loop=False, reverse=False, block=None):
        self.loop = loop
        self.reverse = reverse
        self.done = False
        self.block = block or BLOCK_SIZE
        self.buf = array.array("h", bytes(2 * self.block))
        self._view = memoryview(self.buf)
        self.file = open(path, "rb")
        try:
            self.start, self.size = self._find_data()
        except Exception:
            self.file.close()
            raise
        self.left = self.size
        self.file.seek(self.start)


        self._roll_at = None
        self._roll_blocks = 0
        self._rolled = 0

    def roll(self, blocks):
        0

        if not blocks:
            self._roll_at = None
            return
        if self._roll_at is None:
            self._roll_at, self._rolled = self.left, 0
        self._roll_blocks = blocks

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

    def read(self, gain=256):
        0

        if self.done:
            return 0
        if self._roll_at is not None:
            if self._rolled >= self._roll_blocks or self.left <= 0:
                self._rolled = 0
                self.left = self._roll_at
                if not self.reverse:
                    self.file.seek(self.start + 2 * (self.size - self.left))
            self._rolled += 1
        if self.left <= 0:
            if not self.loop:
                self.done = True
                return 0
            self.file.seek(self.start)
            self.left = self.size
        want = min(self.block, self.left)
        if self.reverse:
            self.file.seek(self.start + 2 * (self.left - want))
        got = (self.file.readinto(self._view[0:want]) or 0) // 2
        self.left = self.left - got if got else 0
        if self.reverse and got:
            _reverse(self.buf, got)
        if gain != 256 and got:
            _scale(self.buf, got, gain)
        return got

    def close(self):
        self.done = True
        self.file.close()







def _views_write_through():
    if np is None:
        return False
    try:
        buf = array.array("h", [0])
        np.frombuffer(buf, dtype=np.int16)[0:1] = np.array([7], dtype=np.int16)
        return buf[0] == 7
    except Exception:
        return False


_NP_VIEWS = _views_write_through()


def _mix_into(block, extra, n):
    0
    if _NP_VIEWS:
        out = np.frombuffer(block, dtype=np.int16)
        mixed = np.array(out[0:n], dtype=_FLOAT) + np.frombuffer(extra, dtype=np.int16)[0:n]
        out[0:n] = np.array(np.clip(mixed, -32768, 32767), dtype=np.int16)
        return
    for i in range(n):
        value = block[i] + extra[i]
        block[i] = 32767 if value > 32767 else (-32768 if value < -32768 else value)


def _scale(buf, n, gain):
    0
    if _NP_VIEWS:
        out = np.frombuffer(buf, dtype=np.int16)
        scaled = np.array(out[0:n], dtype=_FLOAT) * (gain / 256)
        out[0:n] = np.array(np.clip(scaled, -32768, 32767), dtype=np.int16)
        return
    for i in range(n):
        buf[i] = (buf[i] * gain) >> 8


def _reverse(buf, n):
    0
    if _NP_VIEWS:
        out = np.frombuffer(buf, dtype=np.int16)
        out[0:n] = np.array(out[0:n][::-1], dtype=np.int16)
        return
    i, j = 0, n - 1
    while i < j:
        buf[i], buf[j] = buf[j], buf[i]
        i += 1
        j -= 1


class BackwardsStream:
    0


















    HALF = 4096
    PIECE = 512

    TAIL_SECONDS = 0.5

    def __init__(self, path, sample_rate=SAMPLE_RATE, half=HALF, raw_sample=None):
        if raw_sample is None:
            import audiocore
            raw_sample = audiocore.RawSample
        self.path = path
        self.half = half
        self.seconds = half / sample_rate
        self.buffer = array.array("h", bytes(4 * half))
        self._piece = array.array("h", bytes(2 * self.PIECE))
        self._piece_view = memoryview(self._piece)
        if _NP_VIEWS:
            self._np_buffer = np.frombuffer(self.buffer, dtype=np.int16)
            self._np_piece = np.frombuffer(self._piece, dtype=np.int16)
        self.sample = raw_sample(self.buffer, channel_count=1,
                                 sample_rate=sample_rate, single_buffer=False)
        self.reader = None
        self.playing = False
        self._started = 0.0
        self._next = 0
        self._blocks = 0
        self._left = 0

        self.worst_ms = 0.0

    def start(self, now):
        0

        self._close_reader()
        self.reader = WaveReader(self.path)
        self._left = self.reader.size
        self._blocks = (self.reader.size + self.half - 1) // self.half
        self.worst_ms = 0.0
        self._fill(0)
        self._fill(1)
        self._next = 2
        self._started = now
        self.playing = True

    def _fill(self, index):
        0
        began = time.monotonic()
        at = (index % 2) * self.half
        end = at + self.half
        reader = self.reader
        while at < end:
            want = min(self.PIECE, end - at, self._left if reader is not None else 0)
            got = 0
            if want > 0:
                self._left -= want
                reader.file.seek(reader.start + 2 * self._left)
                got = (reader.file.readinto(self._piece_view[0:want]) or 0) // 2
            if not got:

                self._write_silence(at, end)
                break
            self._write_reversed(at, got)
            at += got
        self.worst_ms = max(self.worst_ms, (time.monotonic() - began) * 1000)

    def _write_reversed(self, at, n):
        if _NP_VIEWS:
            self._np_buffer[at:at + n] = self._np_piece[0:n][::-1]
            return
        buf, piece = self.buffer, self._piece
        for i in range(n):
            buf[at + i] = piece[n - 1 - i]

    def _write_silence(self, at, end):
        if _NP_VIEWS:
            self._np_buffer[at:end] = 0
            return
        buf = self.buffer
        for i in range(at, end):
            buf[i] = 0

    def update(self, now):
        0

        if not self.playing:
            return False
        elapsed = now - self._started


        while elapsed >= (self._next - 0.5) * self.seconds:
            self._fill(self._next)
            self._next += 1
        if elapsed >= self._blocks * self.seconds + self.TAIL_SECONDS:
            self.stop()
            return False
        return True

    def stop(self):
        self.playing = False
        self._close_reader()

    def _close_reader(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def deinit(self):
        self.stop()
        self.sample.deinit()


def _value(param):

    return getattr(param, "value", param)


def scale_by_name(name):
    for scale_name, pitch_classes in SCALES:
        if scale_name == name:
            return pitch_classes
    raise ValueError("unknown scale %r" % (name,))


def key_by_name(name):
    return KEYS.index(name.upper().replace("DB", "C#").replace("EB", "D#")
                      .replace("GB", "F#").replace("AB", "G#").replace("BB", "A#"))

























VOCODER_WAVEFORMS = ("saw", "square", "pulse", "saw_square")



VOCODER_FILTERS = True
VOCODER_MAX_BANDS = 11



VOCODER_VOICE = False
VOCODER_VOICE_MAX_BANDS = 8
VOCODER_VOICE_DENSITY = 1
VOCODER_LOW_HZ = 180.0
VOCODER_HIGH_HZ = 5000.0
VOCODER_BREATH_HZ = 4000.0
VOCODER_HARMONICS = 48
VOCODER_TABLE = 256
VOCODER_NOISE = 2048
VOCODER_GAIN = 0.7
VOCODER_RELEASE = 0.4
VOCODER_RETUNE = 0.01



VOCODER_GATE = -50.0
VOCODER_GATE_OFF = -80.0
VOCODER_GATE_CLOSE = 6.0
VOCODER_GATE_HOLD = 4


VOCODER_EVERY_BLOCK = False

_SPECTRUM = None
if np is not None:
    try:
        from ulab import utils as _ulab_utils
        _SPECTRUM = getattr(_ulab_utils, "spectrogram", None)
    except ImportError:
        pass




_IFFT = None
_IFFT_GAIN = 1.0


def _ifft_real(re, im):
    0

    if _IFFT == "pair":
        return np.fft.ifft(re, im)[0]
    return np.real(np.fft.ifft(re)) - np.imag(np.fft.ifft(im))


if np is not None and hasattr(np, "fft") and hasattr(np.fft, "ifft"):
    try:
        _IFFT = "pair" if isinstance(np.fft.ifft(np.zeros(4), np.zeros(4)), tuple) else "complex"
    except Exception:
        try:
            _IFFT = "pair" if isinstance(np.fft.ifft(np.zeros(4)), tuple) else "complex"
        except Exception:
            _IFFT = None
    if _IFFT is not None:
        try:

            if float(_ifft_real(np.array([0.0, 1.0, 0.0, 0.0]), np.zeros(4))[0]) < 0.5:
                _IFFT_GAIN = float(VOCODER_TABLE)
        except Exception:
            _IFFT = None


def _power(x):
    0
    if _SPECTRUM is not None:
        mag = _SPECTRUM(x)
        return mag * mag
    spec = np.fft.fft(x)
    if isinstance(spec, tuple):
        re, im = spec
    else:
        re, im = np.real(spec), np.imag(spec)
    return re * re + im * im







VOCODER_CHORDS = (
    ("major", (4, 5, 6)),
    ("minor", (10, 12, 15)),
    ("7", (4, 5, 6, 7)),
    ("maj7", (8, 10, 12, 15)),
    ("m7", (10, 12, 15, 18)),
    ("power", (2, 3, 4)),


    ("dim", (5, 6, 7)),
    ("m7b5", (5, 6, 7, 9)),
)




VOCODER_KEY_CHORDS = (
    ("key_triad", (0, 2, 4)),
    ("key_seventh", (0, 2, 4, 6)),
)
VOCODER_SHAPES = (
    ((0, 4, 7), "major"),
    ((0, 3, 7), "minor"),
    ((0, 3, 6), "dim"),
    ((0, 4, 7, 11), "maj7"),
    ((0, 3, 7, 10), "m7"),
    ((0, 4, 7, 10), "7"),
    ((0, 3, 6, 10), "m7b5"),
)
VOCODER_CHORD_SAMPLES = 128

_WAVES = {}
_SCALES = {}


def chord_wave(shape, chord):
    0


    key = (shape, chord)
    if key in _WAVES:
        return _WAVES[key]
    ratios = None
    for name, multiples in VOCODER_CHORDS:
        if name == chord:
            ratios = multiples
    if ratios is None:
        raise ValueError("vocoder chord must be one of {}".format(
            tuple(name for name, _ in VOCODER_CHORDS + VOCODER_KEY_CHORDS)))
    base, density = carrier_wave(shape)
    size = len(base)
    n = VOCODER_CHORD_SAMPLES * ratios[-1]
    wave = [0.0] * n
    for m in ratios:
        step = m * size / n
        for i in range(n):
            p = i * step
            j = int(p)
            a = base[j % size]
            wave[i] += a + (base[(j + 1) % size] - a) * (p - j)
    scale = 30000.0 / max(abs(v) for v in wave)
    table = array.array("h", [int(v * scale) for v in wave])
    _SCALES[key] = scale


    density *= scale * scale * sum(ratios) / ratios[0]
    _WAVES[key] = table, ratios[0], density
    return _WAVES[key]


def key_tones(snapper, note, steps):
    0


    allowed = snapper.allowed
    if len(allowed) == 12:
        allowed = SCALES[1][1]
    pcs = sorted((pc + snapper.root) % 12 for pc in allowed)
    pc = note % 12
    if pc not in pcs:
        return None
    i, n = pcs.index(pc), len(pcs)
    return [pcs[(i + s) % n] - pc + 12 * ((i + s) // n) for s in steps]


def vocoder_key_chord(tones, steps):
    0
    if tones is not None:
        tones = tuple(tones)
        for shape, name in VOCODER_SHAPES:
            if shape == tones:
                return name
    return "7" if len(steps) > 3 else "major"


def carrier_wave(shape):
    0



    if shape not in VOCODER_WAVEFORMS:
        raise ValueError("vocoder waveform must be one of {}".format(VOCODER_WAVEFORMS))
    if shape in _WAVES:
        return _WAVES[shape]
    n = VOCODER_TABLE
    terms = carrier_terms(shape)
    wave = [0.0] * n
    for k, amp, cos in terms:
        if amp:
            step = 2 * math.pi * k / n
            for i in range(n):
                wave[i] += amp * (math.cos(step * i) if cos else math.sin(step * i))
    scale = 30000.0 / max(abs(v) for v in wave)
    table = array.array("h", [int(v * scale) for v in wave])
    density = sum((k * amp * scale) ** 2 for k, amp, _ in terms) / len(terms)
    _WAVES[shape] = table, density
    _SCALES[shape] = scale
    return table, density


def carrier_terms(shape):
    0
    if shape not in VOCODER_WAVEFORMS:
        raise ValueError("vocoder waveform must be one of {}".format(VOCODER_WAVEFORMS))
    terms = []
    for k in range(1, VOCODER_HARMONICS + 1):
        if shape == "saw":
            terms.append((k, 1.0 / k, False))
        elif shape == "square":
            terms.append((k, 1.0 / k if k % 2 else 0.0, False))
        elif shape == "saw_square":

            terms.append((k, 2.0 / k if k % 2 else 1.0 / k, False))
        else:
            terms.append((k, math.sin(math.pi * k * 0.25) / k, True))
    return terms


def carrier_spectrum(shape, chord=None):
    0



    table, density = carrier_wave(shape)
    scale = _SCALES[shape]
    if chord is not None:
        chord_wave(shape, chord)
        scale *= _SCALES[(shape, chord)]
    re, im = [], []
    for k, amp, cos in carrier_terms(shape):
        re.append(amp * scale if cos else 0.0)
        im.append(0.0 if cos else -amp * scale)
    return np.array(re), np.array(im)


class Vocoder:
    0



















    def __init__(self, rate, waveform="saw", bands=8, octave=0, breath=0.0, chord=None,
                 gate=VOCODER_GATE, squeeze_chord=None):
        import synthio

        if np is None or not (_SPECTRUM is not None or hasattr(np, "fft")):
            raise ValueError("the vocoder needs ulab with fft")
        if not 4 <= bands <= VOCODER_MAX_BANDS:
            raise ValueError("vocoder bands must be 4 to {}".format(VOCODER_MAX_BANDS))
        if not -2 <= octave <= 2:
            raise ValueError("vocoder octave must be -2 to 2")

        self.filters = VOCODER_FILTERS or _IFFT is None
        self.rate = rate
        self.breath = min(1.0, max(0.0, breath))
        self.waveform = waveform
        self.notes = []
        self._pool = None
        self._pressed = False
        self.key_steps = None
        self.chord = ""


        self.squeezable = squeeze_chord is not None
        if self.squeezable:
            self._modes = (chord, None if squeeze_chord == "none" else squeeze_chord)
        else:
            self._modes = (chord,)
        tones = 1
        for mode in self._modes:
            steps = dict(VOCODER_KEY_CHORDS).get(mode)
            if steps is None:
                self._wave(mode)
                tones = max(tones, len(dict(VOCODER_CHORDS).get(mode, (1,))))
            else:
                tones = max(tones, len(steps))
                for shape, name in VOCODER_SHAPES:
                    if len(shape) == len(steps):
                        chord_wave(waveform, name)
        self.squeezed = False
        self._use(chord)
        self.octave = 2.0 ** octave
        self.hz = 0.0


        n = 1
        while n * 2 <= BUFFER_SIZE:
            n *= 2
        self._start = BUFFER_SIZE - n
        self._hann = np.array([math.sin(math.pi * (i + 0.5) / n) ** 2 for i in range(n)])

        psd = 2.0 / (n * n * 0.375)

        self._gate = None
        if gate > VOCODER_GATE_OFF:
            self._gate = 32768.0 ** 2 * 10.0 ** (gate / 10.0) / psd
            self._gate_close = self._gate * 10.0 ** (-VOCODER_GATE_CLOSE / 10.0)
        self._held = 0



        high = min(VOCODER_HIGH_HZ, 0.4 * rate)
        ratio = (high / VOCODER_LOW_HZ) ** (1.0 / (bands - 1))
        half = math.sqrt(ratio)
        q = 1.0 / (half - 1.0 / half)
        self.centres = []
        self._bins = []
        self._k = []
        self._floor = []
        self._biquads = []
        mode = synthio.FilterMode.BAND_PASS
        for i in range(bands):
            centre = VOCODER_LOW_HZ * ratio ** i
            low, top = centre / half, centre * half
            a = max(1, int(low * n / rate + 0.5))
            b = max(a + 1, int(top * n / rate + 0.5))
            self.centres.append(centre)
            self._bins.append((a, b))



            self._k.append(VOCODER_GAIN * math.sqrt(psd / (0.5 * (1 / low - 1 / top))))
            self._floor.append(top)
            if self.filters:
                self.notes.append(synthio.Note(
                    frequency=110.0, waveform=self._table, amplitude=0.0,
                    filter=synthio.Biquad(mode, frequency=centre, Q=q)))
            else:

                w0 = 2 * math.pi * centre / rate
                alpha = math.sin(w0) / (2 * q)
                self._biquads.append((alpha / (1 + alpha), -2 * math.cos(w0) / (1 + alpha),
                                      (1 - alpha) / (1 + alpha)))
        self.levels = [0.0] * bands

        if not self.filters:


            self._harmonics = np.array([float(k) for k in range(1, VOCODER_HARMONICS + 1)])
            self._re = np.zeros(VOCODER_TABLE)
            self._im = np.zeros(VOCODER_TABLE)
            self._pool = []
            for _ in range(tones):
                tables = [array.array("h", [0] * VOCODER_TABLE) for _ in range(2)]
                self._pool.append([synthio.Note(frequency=110.0, waveform=tables[0], amplitude=0.0),
                                   tables, [np.frombuffer(t, dtype=np.int16) for t in tables], 0])
            self._resp = None
            self._resp_hz = 0.0
            self._spectra = {}
            self._retone()

        self.noise = None
        if self.breath > 0:
            cut = min(VOCODER_BREATH_HZ, 0.35 * rate)
            seed = 12345
            noise = array.array("h", [0] * VOCODER_NOISE)
            for i in range(VOCODER_NOISE):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                noise[i] = (seed >> 15) % 60001 - 30000

            level = 17320.0 * math.sqrt(1.0 - cut / (rate / 2.0))
            self._noise_bins = (int(cut * n / rate + 0.5), n // 2)
            self._noise_k = self.breath * VOCODER_GAIN * math.sqrt(psd) / level


            noise, scale = _high_passed(noise, cut / rate, 0.7071)
            self._noise_k /= scale
            self.noise = synthio.Note(
                frequency=rate / VOCODER_NOISE, waveform=noise, amplitude=0.0)
        self.synth = synthio.Synthesizer(sample_rate=rate, channel_count=1)
        self.output = self.synth

    def start(self):
        self.synth.press(self.notes + ([self.noise] if self.noise else []))
        self._pressed = True

    def _wave(self, chord):
        0
        if chord is None:
            table, density = carrier_wave(self.waveform)
            return table, 1.0, density
        table, root, density = chord_wave(self.waveform, chord)
        return table, 1.0 / root, density

    def _use(self, mode):
        0

        self.key_steps = dict(VOCODER_KEY_CHORDS).get(mode)
        if self.key_steps is None:
            self.set_chord(mode)
        elif self.chord not in [n for t, n in VOCODER_SHAPES if len(t) == len(self.key_steps)]:
            self.set_chord(vocoder_key_chord(None, self.key_steps))

    def squeeze(self, squeezed):
        0
        if self.squeezable and squeezed != self.squeezed:
            self.squeezed = squeezed
            self._use(self._modes[1 if squeezed else 0])

    def set_chord(self, chord):
        0

        if chord == self.chord:
            return
        self._table, self._sub, density = self._wave(chord)
        self.chord = chord
        self._dk = 1.0 / math.sqrt(density)
        if self._pool is not None:
            self._retone()
            return
        for note in self.notes:
            note.waveform = self._table
            if self.hz:
                note.frequency = self.hz * self._sub

    def _retone(self):
        0

        self._ratios = dict(VOCODER_CHORDS).get(self.chord, (1,))
        if self.chord not in self._spectra:
            self._spectra[self.chord] = carrier_spectrum(self.waveform, self.chord)
        self._coefs = self._spectra[self.chord]
        self._resp = None
        notes = [entry[0] for entry in self._pool[:len(self._ratios)]]
        if self._pressed:
            gone = [note for note in self.notes if note not in notes]
            new = [note for note in notes if note not in self.notes]
            for note in gone:
                note.amplitude = 0.0
            if gone:
                self.synth.release(gone)
            if new:
                self.synth.press(new)
        self.notes = notes
        self._tune()

    def _tune(self):
        0
        if not self.hz:
            return
        if self._pool is None:
            for note in self.notes:
                note.frequency = self.hz * self._sub
            return
        root = self._ratios[0]
        for note, m in zip(self.notes, self._ratios):
            note.frequency = self.hz * m / root

    def _respond(self):
        0


        hz, rate, count = self.hz or 110.0, self.rate, VOCODER_HARMONICS


        root = self._ratios[0]
        size = count * len(self._ratios)
        w, cr, ci = np.zeros(size), np.zeros(size), np.zeros(size)
        for t, m in enumerate(self._ratios):
            f = hz * m / root
            w[t * count:(t + 1) * count] = self._harmonics * (2 * math.pi * f / rate)

            keep = min(count, max(0, int(0.5 * rate / f - 1e-6)))
            cr[t * count:t * count + keep] = self._coefs[0][:keep]
            ci[t * count:t * count + keep] = self._coefs[1][:keep]
        cw, sw = np.cos(w), np.sin(w)
        c2, s2 = np.cos(w * 2.0), np.sin(w * 2.0)
        nr = c2 * -1.0 + 1.0
        resp = []
        for b0, a1, a2 in self._biquads:
            dr = cw * a1 + c2 * a2 + 1.0
            di = sw * -a1 - s2 * a2
            mag = (dr * dr + di * di) * (1.0 / b0)
            hr = (nr * dr + s2 * di) / mag
            hi = (s2 * dr - nr * di) / mag
            resp.append((cr * hr - ci * hi, cr * hi + ci * hr))
        self._resp = resp
        self._resp_hz = hz

    def _play(self, root):
        0
        notes = self.notes
        if not root:
            for note in notes:
                note.amplitude = 0.0
            return
        if self._resp is None or abs(self.hz - self._resp_hz) > VOCODER_RETUNE * self._resp_hz:
            self._respond()
        re = im = None
        for level, (hr, hi) in zip(self.levels, self._resp):
            if level:
                if re is None:
                    re, im = hr * level, hi * level
                else:
                    re += hr * level
                    im += hi * level
        if re is None:
            for note in notes:
                note.amplitude = 0.0
            return
        count = VOCODER_HARMONICS
        gain = _IFFT_GAIN
        for t, entry in enumerate(self._pool[:len(notes)]):
            note, tables, views, playing = entry
            self._re[1:count + 1] = re[t * count:(t + 1) * count]
            self._im[1:count + 1] = im[t * count:(t + 1) * count]
            wave = _ifft_real(self._re, self._im)
            peak = max(float(np.max(wave)), -float(np.min(wave))) * gain
            if peak < 1.0:
                note.amplitude = 0.0
                continue
            spare = 1 - playing
            views[spare][:] = np.array(wave * (30000.0 * gain / peak), dtype=np.int16)
            note.waveform = tables[spare]
            note.amplitude = min(1.0, peak / 30000.0)
            entry[3] = spare

    def measure(self, window):
        0
        x = np.frombuffer(window, dtype=np.int16)[self._start:] * self._hann
        p = _power(x)
        out = [float(np.sum(p[a:b])) for a, b in self._bins]
        if self.noise is not None:
            a, b = self._noise_bins
            out.append(float(np.sum(p[a:b])))
        return out

    def update(self, window, hz):
        0

        if hz > 0:
            hz = min(2000.0, max(30.0, hz * self.octave))
            if abs(hz - self.hz) > 0.002 * hz:
                self.hz = hz
                self._tune()
        powers = self.measure(window)
        f0 = self.hz or 110.0
        root = self._dk / math.sqrt(f0)
        if self._gate is not None:
            total = sum(powers)
            if total >= self._gate:
                self._held = VOCODER_GATE_HOLD
            elif total < self._gate_close and self._held:
                self._held -= 1
            if not self._held:
                root = 0.0
        levels = self.levels
        for i in range(len(levels)):
            if self._floor[i] < f0:

                levels[i] = 0.0
                continue
            level = min(1.0, self._k[i] * math.sqrt(powers[i]) * root)
            if level < levels[i]:
                level += (levels[i] - level) * VOCODER_RELEASE
            levels[i] = level
        if self._pool is None:
            for note, level in zip(self.notes, levels):
                note.amplitude = level
        else:
            self._play(root)
        if self.noise is not None:
            self.noise.amplitude = min(1.0, self._noise_k * math.sqrt(powers[-1])) if root else 0.0

    def deinit(self):
        self.synth.release_all()
        self.synth.deinit()


def _high_passed(wave, cutoff, q):
    0


    w0 = 2 * math.pi * cutoff
    alpha = math.sin(w0) / (2 * q)
    cos = math.cos(w0)
    a0 = 1 + alpha
    b0 = (1 + cos) / 2 / a0
    b1 = -(1 + cos) / a0
    a1 = -2 * cos / a0
    a2 = (1 - alpha) / a0
    x1 = x2 = y1 = y2 = 0.0
    out = [0.0] * len(wave)
    for _ in range(2):
        for i in range(len(wave)):
            x = wave[i]
            y = b0 * (x + x2) + b1 * x1 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            out[i] = y
    scale = 30000.0 / max(abs(v) for v in out)
    return array.array("h", [int(v * scale) for v in out]), scale


def _feed_sample(buffer, fmt):
    0
    import audiocore
    return audiocore.RawSample(buffer, sample_rate=fmt["sample_rate"],
                               single_buffer=FEED_PHASE is None)

class Autotune:
    0


























    def __init__(self, mic, fmt, key="C", scale="chromatic", speed=1.0,
                 humanize=0.0, mix=1.0, grain_size=None,
                 density=None, engine=SHIFT_ENGINE, harmony=None,
                 inversion=0, harmony_octave=0, harmony_level=0.7, correct=True,
                 harmony_double=None, vocoder=None, vocoder_bands=8, vocoder_octave=0,
                 vocoder_breath=0.0, vocoder_mix=1.0, vocoder_chord=None,
                 vocoder_gate=VOCODER_GATE, vocoder_squeeze_chord=None):
        import audiocore

        if vocoder is not None and harmony is not None:
            raise ValueError("a preset can have a harmonizer or a vocoder, not both")

        voiceless = vocoder is not None and not hasattr(vocoder_mix, "value") and vocoder_mix >= 1
        if vocoder is not None and not voiceless and not VOCODER_VOICE:
            print("vocoder: the mic can't run your voice beside it; playing it at 100%")
            voiceless, vocoder_mix = True, 1.0
        if density is None:


            density = VOCODER_VOICE_DENSITY if vocoder is not None else GRAIN_DENSITY

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
        self.cents = None
        self.correction = 0.0
        self._target = 0.0
        self._window = array.array("h", [0] * BUFFER_SIZE)
        self._block = array.array("h", [0] * BLOCK_SIZE)
        self._half = 0
        self._burst_due = None
        self._guard = 0
        self._probe = None
        self._step = 0
        self._renders = 0
        self._probe_phase = 0.0
        self._newest = 0
        self._period = 0
        self._waiting = False
        self._turn = 0
        self.busy_ms = 0.0
        self.wait_ms = 0.0
        self.idle_ms = 0.0
        self.outside_ms = 0.0
        self._ended = 0
        self._analyse_ns = 0



        self.gate = 1

        self.sample = None
        self.sample_gain = 256
        self._sample_ready = 0
        self._zeros = array.array("h", [0] * BLOCK_SIZE)
        self.gate_open = False
        self._gate_hold = 0
        self._gate_blocks = max(1, GATE_HOLD_MS * SAMPLE_RATE // (1000 * BLOCK_SIZE))



        self._out = array.array("h", [0] * (BLOCK_SIZE * 2))
        view = memoryview(self._out)
        self._front, self._back = view[:BLOCK_SIZE], view[BLOCK_SIZE:]
        self.feed = _feed_sample(self._out, fmt)

        self.shifter = None
        self.shifter_name = None
        self.ola = None
        if voiceless:
            self.shifter_name = "no shifter"
        elif engine == "native":
            import audiodelays
            self.shifter, self.shifter_name = _make_native_shifter(
                audiodelays, fmt, mix, grain_size, density)
        if self.shifter is None and not voiceless:
            self.ola = OLAPitchShifter(grain=grain_size)
            self.shifter_name = "python OLA"
            self.output = self.feed
        else:
            self.output = self.shifter
        self._started = False




        self.voices = []
        self._feeds = []
        self.mixer = None
        self.harmony_level = harmony_level
        self._level = None
        self._intervals = []
        self._double = 0
        self._key_chord = False


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
            if harmony_double is not None:
                self._double = dict(DOUBLES).get(str(harmony_double).lower(), 0)
                if not self._double:
                    raise ValueError("harmonizer double is above or below, not %r" % (harmony_double,))
                if len(tones) > 3:
                    raise ValueError("harmonizer double needs a chord of three notes or fewer")
            if not self._key_chord:
                self._intervals = self._doubled(voicing(tones, inversion, harmony_octave))
            if self.shifter is None:
                print("harmonizer off: it needs audiodelays.GranularPitchShift")
            else:
                import audiodelays
                count = len(tones) - 1
                for _ in range(count + (1 if self._double else 0)):
                    self._feeds.append(_feed_sample(self._out, fmt))
                    self.voices.append(_make_native_shifter(
                        audiodelays, fmt, 1.0, grain_size, density)[0])
                if self._key_chord:
                    self._intervals = self._doubled(
                        voicing(OFF_KEY_SHAPE[:count + 1], inversion, harmony_octave))
        if self.shifter is not None:


            import audiomixer
            self.mixer = audiomixer.Mixer(voice_count=len(self.voices) + 1, **fmt)
            self.output = self.mixer
            try:
                import synthio
                self._probe = synthio.LFO(rate=PROBE_RATE, scale=0.0, offset=1.0)
                self.mixer.voice[0].level = self._probe
            except Exception as error:
                print("render counter off: {}".format(error))
                self._probe = None



        self.vocoder = None
        self._vmixer = None
        self.vocoder_mix = vocoder_mix
        self._vocoder_level = None
        if vocoder is not None:
            if not voiceless and vocoder_bands > VOCODER_VOICE_MAX_BANDS:
                print("vocoder: {} bands with the voice in is more than the mic can run; "
                      "using {}".format(vocoder_bands, VOCODER_VOICE_MAX_BANDS))
                vocoder_bands = VOCODER_VOICE_MAX_BANDS
            self.vocoder = Vocoder(fmt["sample_rate"], vocoder, vocoder_bands,
                                   vocoder_octave, vocoder_breath, vocoder_chord, vocoder_gate,
                                   vocoder_squeeze_chord)
            if voiceless:
                self.output = self.vocoder.output
            else:
                import audiomixer
                self._voice_out = self.output
                self._vmixer = audiomixer.Mixer(voice_count=2, **fmt)
                self.output = self._vmixer

    @property
    def mix(self):
        return self.shifter.mix if self.shifter is not None else 1.0

    @mix.setter
    def mix(self, value):
        if self.shifter is not None:
            self.shifter.mix = value

    def set_key_scale(self, key="C", scale="chromatic"):
        self.snapper.set_scale(scale_by_name(scale), key_by_name(key))

    def play_sample(self, path, loop=False, reverse=False):
        0


        self.stop_sample()
        self.sample = WaveReader(path, loop, reverse)
        self._sample_ready = self.sample.read(self.sample_gain)

    def stop_sample(self):
        if self.sample is not None:
            self.sample.close()
            self.sample = None

    def start(self):
        0

        if self.vocoder is not None and not self._started:
            self.vocoder.start()
            if self._vmixer is not None:
                self._vmixer.voice[0].play(self._voice_out, loop=True)
                self._vmixer.voice[1].play(self.vocoder.output, loop=True)
                self._set_vocoder_levels()
        if self.shifter is not None and not self._started:
            if self.mixer is not None:
                self.mixer.voice[0].play(self.shifter, loop=True)
                for index, voice in enumerate(self.voices):
                    self.mixer.voice[index + 1].play(voice, loop=True)
                self._set_levels()
            counting = False
            try:
                counting = FEED_PHASE is not None and self._await_render()
            except Exception as error:
                print("render counter off: {}".format(error))
            for voice, feed in zip(self.voices, self._feeds):
                voice.play(feed, loop=True)
            self.shifter.play(self.feed, loop=True)
            if counting:
                try:
                    counting = self._count_from_start()
                except Exception as error:
                    print("render counter off: {}".format(error))
                    counting = self._step = 0
            if not counting and FEED_SYNC and FEED_PHASE is not None:
                try:
                    self._sync_feed()
                except Exception as error:
                    print("feed sync failed: {}".format(error))
        self._started = True

    def _await_render(self):
        0


        probe = self._probe
        if probe is None:
            return False
        try:
            phase = probe.phase
        except AttributeError:
            return False
        now = time.monotonic_ns
        began = now()
        while probe.phase == phase:
            if now() - began > 6 * BLOCK_NS:
                print("render counter off: the LFO never moved")
                return False
        return True

    def _count_from_start(self):
        0


        probe = self._probe
        now = time.monotonic_ns
        phase = probe.phase
        began = now()
        total = 0.0
        first = last = None
        starts = []
        prev = began
        while prev - began < 5 * BLOCK_NS:
            t = now()
            p = probe.phase
            if p != phase:
                total += (p - phase) % 1.0
                phase = p
                last = (t, total)
                starts.append(prev)
                if first is None:
                    first = last
            prev = t
        renders = 0 if first is None else (last[0] - first[0] + BLOCK_NS // 2) // BLOCK_NS
        if not renders:
            print("render counter off: too few renders seen")
            return False



        per = PROBE_RATE * BLOCK_NS / 1e9
        measured = (last[1] - first[1]) / renders
        self._step = per if abs(measured - per) < abs(measured - per / 2) else per / 2
        self._renders = int(total / self._step + 0.5)
        self._probe_phase = phase
        self._burst_due = None
        if len(starts) > 1:
            gap = min(b - a for a, b in zip(starts, starts[1:]))
            self._period = max(1, (gap + BLOCK_NS // 2) // BLOCK_NS) * BLOCK_NS
            self._burst_due = starts[-1] + self._period
        self._feed_block(self._zeros)
        print("feed sync: counting renders, {} in {:.0f} ms, {} since the feed started; every {} ms".format(
            renders, (last[0] - first[0]) / 1e6, self._renders, self._period // 1000000))
        return True

    def _render_ahead(self):
        0

        due, period = self._burst_due, self._period
        if not self._step or due is None or not period:
            return 0
        t = time.monotonic_ns()
        while due + RENDER_GUARD_NS <= t:
            due += period
        near = RENDER_NEAR_NS + (RENDER_HYST_NS if self._waiting else -RENDER_HYST_NS)
        return due - t if 0 < due - t < near else 0

    def _wait_for_render(self):
        0


        due, period = self._burst_due, self._period
        if due is None:
            return 0
        now = time.monotonic_ns
        t = began = now()
        while due + RENDER_GUARD_NS <= t:
            due += period
        near = RENDER_NEAR_NS + (RENDER_HYST_NS if self._waiting else -RENDER_HYST_NS)
        if due - t >= near:
            if due - t < BLOCK_NS:
                self._waiting = False
        else:
            self._waiting = True
            probe = self._probe
            seen = probe.phase
            if t < due or seen == self._probe_phase:
                prev = t
                while t < due + RENDER_GUARD_NS:
                    if probe.phase != seen:
                        due = prev
                        break
                    prev = t
                    t = now()
            due += period
        self._burst_due = due
        return now() - began

    def _sync_feed(self):
        0


        now = time.monotonic_ns

        began = prev = now()
        gap = burst = 0
        bursts = 0
        while prev - began < 2 * BLOCK_NS + BLOCK_NS // 4:
            t = now()
            if t - prev > 1000000:
                bursts += 1
            if t - prev > gap:
                gap, burst = t - prev, prev
            prev = t





        block = self._block
        for _ in range(6):
            t = now()
            self.mic.record(block, BLOCK_SIZE)
            edge = now()
            if edge - t > BLOCK_NS // 4:
                break
        for i in (1, 2):
            self.mic.record(block, BLOCK_SIZE)
            t = now() - i * BLOCK_NS
            if t < edge:
                edge = t
        edge += 2 * BLOCK_NS

        t = now()
        self._noise_gate(block)
        wrote = now() - t + 100000






        ahead = (burst - edge - wrote + 1000000) % BLOCK_NS - 1000000
        self._guard = max(ahead, 0) + 1000000 if ahead < BLOCK_NS // 2 else 0
        due = burst - self._guard - edge - wrote
        last = -((-due) // BLOCK_NS) - 1
        self._half = (1 ^ (last % 2)) ^ FEED_PHASE
        self._burst_due = burst
        self._wait_out_burst()
        self._feed_block(self._zeros)
        print("feed sync: burst {:.1f} ms after a block, {:.1f} ms long; write at {:.1f} ms; {} bursts; {}".format(
            ((burst - edge) % BLOCK_NS) / 1e6, gap / 1e6, wrote / 1e6, bursts,
            "waits for it" if self._guard else "writes ahead"))

    def _wait_out_burst(self):
        0


        if self._step:
            return self._wait_for_render()
        due, guard = self._burst_due, self._guard
        if due is None or not guard:
            return 0
        now = time.monotonic_ns
        t = began = now()
        while due <= t:
            due += 2 * BLOCK_NS
        if due - guard <= t:
            prev = t
            while prev < due + guard:
                t = now()
                if t - prev > 1000000:
                    due = prev
                    break
                prev = t
            due += 2 * BLOCK_NS
        self._burst_due = due
        return now() - began

    def deinit(self):
        self.stop_sample()
        for part in [self._vmixer, self.mixer, self.shifter] + self.voices + self._feeds + [self.feed]:
            if part is not None:
                part.deinit()
        if self.vocoder is not None:
            self.vocoder.deinit()

    def _set_vocoder_levels(self):
        level = min(1.0, max(0.0, _value(self.vocoder_mix)))
        if level == self._vocoder_level:
            return
        self._vocoder_level = level
        self._vmixer.voice[0].level = 1.0 - level
        self._vmixer.voice[1].level = level

    def _set_levels(self):
        level = min(1.0, max(0.0, _value(self.harmony_level)))
        if level == self._level:
            return
        self._level = level




        gain = 1.0 / (1.0 + len(self.voices) * level)
        if self._probe is not None:
            self._probe.offset = gain
        else:
            self.mixer.voice[0].level = gain
        for index in range(len(self.voices)):
            self.mixer.voice[index + 1].level = gain * level

    def _chord_intervals(self, note):
        0










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
        return self._doubled([offset + i for i in self._key_intervals(note + offset)])

    def _doubled(self, intervals):
        0


        return intervals + [self._double] if self._double else intervals

    def _key_intervals(self, note):
        0
        steps = self._tones
        tones = key_tones(self.snapper, note, steps)
        if tones is None:
            tones = OFF_KEY_SHAPE[:len(steps)]
        return voicing(tones, self._inversion, self._octave)

    def _feed_block(self, block):
        if FEED_PHASE is None:
            self._front[:] = self._back
            self._back[:] = block
            return
        if self._step:


            phase = self._probe.phase
            count = int((phase - self._probe_phase) % 1.0 / self._step + 0.5)
            if count:
                self._probe_phase = phase
                self._renders += count
            first = (self._renders % 2) ^ FEED_PHASE
            halves = (self._front, self._back)
            if self._newest != first:
                halves[first][:] = halves[self._newest]
            halves[first ^ 1][:] = block
            self._newest = first ^ 1
            return
        start = (self._half ^ FEED_PHASE) * BLOCK_SIZE
        self._out[start:start + BLOCK_SIZE] = block
        self._half ^= 1

    def _noise_gate(self, block):
        0

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
        0
        block = self._block
        entered = time.monotonic_ns()
        got = self.mic.record(block, BLOCK_SIZE)
        began = time.monotonic_ns()
        self.idle_ms = (began - entered) / 1e6
        self.outside_ms = (entered - self._ended) / 1e6 if self._ended else 0.0
        for i in range(got, BLOCK_SIZE):
            block[i] = 0
        if not self.gate:
            block[0:BLOCK_SIZE] = self._zeros



        sample = self.sample
        if sample is not None:
            extra, n = sample.buf, self._sample_ready
            if not self.gate:
                block[0:n] = extra[0:n]
            elif n:
                _mix_into(block, extra, n)
            if not n and sample.done:
                self.stop_sample()






        self._noise_gate(block)
        waited = 0
        if self.shifter is not None:





            early = self._render_ahead() > self._analyse_ns + ANALYSE_SPARE_NS
            if early:
                self._analyse(block, speed_override)
            waited = self._wait_out_burst()
            self._feed_block(block)
            if not early:
                self._analyse(block, speed_override)
        else:
            self._analyse(block, speed_override)
        if self.sample is not None:
            self._sample_ready = self.sample.read(self.sample_gain)


        self._ended = time.monotonic_ns()
        self.wait_ms = waited / 1e6
        self.busy_ms = (self._ended - began - waited) / 1e6
        return got

    def _analyse(self, block, speed_override=None):
        0
        began = time.monotonic_ns()
        window = self._window
        window[0:BLOCK_SIZE] = window[BLOCK_SIZE:BUFFER_SIZE]
        window[BLOCK_SIZE:BUFFER_SIZE] = block






        every = DETECT_EVERY + (1 if self.sample is not None and self.gate else 0)
        self._turn = (self._turn + 1) % every
        if not (self.enabled and (self.correct or self._key_chord or self.vocoder is not None)):
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

                slack = _value(self.humanize) * HUMANIZE_CENTS / 100.0
                if semis > slack:
                    semis -= slack
                elif semis < -slack:
                    semis += slack
                else:
                    semis = 0.0
                self._target = semis
            else:

                self._target *= 0.9 ** every
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
        elif self.shifter is not None:
            self.shifter.semitones = self.correction
        if self.voices:
            if self._key_chord and self.snapper.target is not None and self._turn == 0:
                self._intervals = self._chord_intervals(self.snapper.target)
            for voice, interval in zip(self.voices, self._intervals):
                voice.semitones = self.correction + interval
            self._set_levels()
        if self.vocoder is not None:
            steps = self.vocoder.key_steps
            if steps and self._turn == 0 and self.snapper.target is not None:


                self.vocoder.set_chord(vocoder_key_chord(
                    key_tones(self.snapper, self.snapper.target, steps), steps))


            if VOCODER_EVERY_BLOCK or DETECT_EVERY == 1 or self._turn != 0:
                hz = self.hz * 2.0 ** (self.correction / 12.0) if self.hz > 0 else 0.0
                self.vocoder.update(window, hz)
            if self._vmixer is not None:
                self._set_vocoder_levels()
        cost = time.monotonic_ns() - began



        self._analyse_ns += (cost - self._analyse_ns) // 4



def bench(hz=150.0, runs=5):
    0

    buf = array.array("h", [int(6000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE)) for i in range(BUFFER_SIZE)])
    best = None
    for _ in range(runs):
        t = time.monotonic_ns()
        found = detect_pitch_autocorrelation(buf)
        ms = (time.monotonic_ns() - t) / 1e6
        best = ms if best is None or ms < best else best
    print("ulab: {}  detection: {:.2f} ms  found {:.1f} Hz for {:.1f}".format(np is not None, best, found, hz))


def bench_vocoder(bands=8, runs=5, chord=None):
    0



    voc = Vocoder(SAMPLE_RATE, bands=bands, breath=0.3, chord=chord, gate=VOCODER_GATE_OFF)
    buf = array.array("h", [int(6000 * math.sin(2 * math.pi * 150.0 * i / SAMPLE_RATE)
                                + 3000 * math.sin(2 * math.pi * 900.0 * i / SAMPLE_RATE))
                            for i in range(BUFFER_SIZE)])
    times = []
    for hz in (150.0, 160.0):
        best = None
        for i in range(runs):
            if hz == 160.0:
                voc.update(buf, 150.0 if i % 2 else 170.0)
            t = time.monotonic_ns()
            voc.update(buf, hz)
            ms = (time.monotonic_ns() - t) / 1e6
            best = ms if best is None or ms < best else best
        times.append(best)
    voc.deinit()
    print("vocoder, {} bands{}: {:.2f} ms a block, {:.2f} ms on a new note ({})".format(
        bands, ", " + chord if chord else "", times[0], times[1],
        "filters" if voc.filters else "no filters"))
