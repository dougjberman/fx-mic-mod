"""Test signals and audio measurements for tools/ting_test.py.

Everything here works on 16 kHz mono int16 audio held in numpy arrays, and
runs on the computer: the mic only plays and records.
"""

import os
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "firmware"))

import autotune  # noqa: E402  (the firmware's own chord tables)

RATE = 24000  # the mic's default rate (code.py DEFAULT_RATE)
LEAD_IN = 0.8          # seconds of silence before the first note
NOTE_SECONDS = 0.6
GAP_SECONDS = 0.1

# Off-pitch notes for autotune: (MIDI note, cents sharp). F#4 is off the C
# major scale, so a C major preset has to move it to F or G. All of them sit
# inside autotune's 80-500 Hz detection range.
OFF_NOTES = ((60, 35), (64, -40), (66, 20), (67, -30), (69, 40), (62, -25))
# In-tune notes for the harmonizer and the loopback check.
SINE_NOTES = ((60, 0), (64, 0), (67, 0), (65, 0))


def midi_hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12.0)


def hz_midi(hz):
    return 69 + 12 * np.log2(hz / 440.0)


def tone(notes, partials=(1.0,), note_seconds=NOTE_SECONDS, peak=0.5):
    """Notes (MIDI, cents) as int16, each with a short fade in and out."""
    out = [np.zeros(int(LEAD_IN * RATE))]
    n = int(note_seconds * RATE)
    t = np.arange(n) / RATE
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.02)
    for midi, cents in notes:
        f = midi_hz(midi + cents / 100.0)
        x = sum(a * np.sin(2 * np.pi * f * (k + 1) * t) for k, a in enumerate(partials))
        out.append(x * fade / sum(partials))
        out.append(np.zeros(int(GAP_SECONDS * RATE)))
    x = np.concatenate(out)
    return (x * peak * 32767).astype(np.int16)


def signals():
    """The WAVs the suite puts on the mic, by name."""
    return {
        "silence.wav": np.zeros(RATE // 2, dtype=np.int16),
        "notes_off.wav": tone(OFF_NOTES, partials=(1.0, 0.5, 0.3, 0.2, 0.12)),
        "notes_sine.wav": tone(SINE_NOTES, note_seconds=0.8),
    }


def seconds_of(samples):
    return len(samples) / RATE


def wav_bytes(samples):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def write_wav(path, samples):
    with open(path, "wb") as f:
        f.write(wav_bytes(samples))


def read_wav(path):
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


# --- Measurements ---


def active(x, floor_db=35):
    """Frames (1024 long) whose level is within ``floor_db`` of the loudest."""
    x = x.astype(np.float64)
    n = len(x) // 1024
    if n == 0:
        return np.zeros(0, dtype=bool), x
    rms = np.sqrt(np.mean(x[:n * 1024].reshape(n, 1024) ** 2, axis=1)) + 1e-9
    return 20 * np.log10(rms / rms.max()) > -floor_db, x


def level_db(x):
    """Level of the active part, in dB below full scale."""
    mask, xf = active(x)
    if not mask.any():
        return -120.0
    frames = xf[:len(mask) * 1024].reshape(len(mask), 1024)[mask]
    return float(20 * np.log10(np.sqrt(np.mean(frames ** 2)) / 32768 + 1e-12))


def yin(x, fmin=80.0, fmax=1000.0, frame=1024, hop=256, threshold=0.15):
    """Pitch per hop in Hz (0 where there is no clear pitch)."""
    x = x.astype(np.float64)
    lo, hi = int(RATE / fmax), int(RATE / fmin)
    out = []
    gate = np.sqrt(np.mean(x ** 2)) * 0.1 if len(x) else 0
    for start in range(0, len(x) - frame - hi, hop):
        w = x[start:start + frame + hi]
        if np.sqrt(np.mean(w[:frame] ** 2)) < gate:
            out.append(0.0)
            continue
        d = np.array([np.sum((w[:frame] - w[lag:lag + frame]) ** 2) for lag in range(hi + 1)])
        cmnd = np.ones_like(d)
        cum = np.cumsum(d[1:])
        cmnd[1:] = d[1:] * np.arange(1, hi + 1) / np.maximum(cum, 1e-9)
        lag = next((t for t in range(lo, hi) if cmnd[t] < threshold and cmnd[t] <= cmnd[t + 1]), None)
        if lag is None:
            out.append(0.0)
            continue
        # parabolic interpolation
        a, b, c = cmnd[lag - 1], cmnd[lag], cmnd[lag + 1]
        shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
        out.append(RATE / (lag + shift))
    return np.array(out)


def steady(midis, tolerance=0.3):
    """Keep pitches that agree with both neighbours, so glides and note
    changes don't count."""
    keep = []
    for i in range(1, len(midis) - 1):
        a, b, c = midis[i - 1], midis[i], midis[i + 1]
        if a and b and c and abs(a - b) < tolerance and abs(c - b) < tolerance:
            keep.append(b)
    return np.array(keep)


def pitch_report(x):
    f0 = yin(x)
    midis = np.where(f0 > 0, hz_midi(np.maximum(f0, 1e-9)), 0.0)
    return steady(midis)


def scale_pcs(key="C", scale="chromatic"):
    root = autotune.key_by_name(key)
    return sorted((pc + root) % 12 for pc in autotune.scale_by_name(scale))


def tuning(x, key="C", scale="chromatic"):
    """How far steady notes sit from the nearest note of the scale, in cents."""
    pcs = scale_pcs(key, scale)
    m = pitch_report(x)
    if len(m) == 0:
        return {"frames": 0}
    nearest = np.array([min((round(v) + d for d in range(-2, 3)
                             if (round(v) + d) % 12 in pcs), key=lambda n: abs(n - v)) for v in m])
    cents = (m - nearest) * 100
    out_of_scale = np.mean([(round(v) % 12) not in pcs for v in m])
    return {
        "frames": int(len(m)),
        "median_cents": float(np.median(np.abs(cents))),
        "within_20": float(np.mean(np.abs(cents) <= 20)),
        "out_of_scale": float(out_of_scale),
        "notes": sorted({int(round(v)) for v in m}),
    }


def key_intervals(chord, note, key="C", scale="chromatic", inversion=0, octave=0):
    """Harmony intervals a key chord gives on ``note``, like Autotune does."""
    tones, _ = autotune.chord_by_name(chord)
    allowed = autotune.scale_by_name(scale)
    if len(allowed) == 12:
        allowed = autotune.SCALES[1][1]
    root = autotune.key_by_name(key)
    pcs = sorted((pc + root) % 12 for pc in allowed)
    pc = note % 12
    if pc in pcs:
        i, n = pcs.index(pc), len(pcs)
        shape = [pcs[(i + s) % n] - pc + 12 * ((i + s) // n) for s in tones]
    else:
        shape = autotune.OFF_KEY_SHAPE[:len(tones)]
    return list(autotune.voicing(shape, inversion, octave))


def expected_harmony(spec, notes, key="C", scale="chromatic"):
    """Expected intervals for each lead note, for a harmonizer spec."""
    chord = spec.get("chord", "major")
    inversion = spec.get("inversion", 0)
    octave = spec.get("octave", 0)
    tones, is_key = autotune.chord_by_name(chord)
    out = {}
    for midi, _ in notes:
        if is_key:
            out[midi] = key_intervals(chord, midi, key, scale, inversion, octave)
        else:
            out[midi] = list(autotune.voicing(tones, inversion, octave))
    return out


def spectral_peaks(x, frame=4096, hop=1024, fmin=110.0, fmax=2400.0, floor_db=-24):
    """Per frame, the MIDI pitches of the clear spectral peaks."""
    x = x.astype(np.float64)
    window = np.hanning(frame)
    freqs = np.fft.rfftfreq(frame, 1.0 / RATE)
    band = (freqs >= fmin) & (freqs <= fmax)
    gate = np.sqrt(np.mean(x ** 2)) * 0.2 if len(x) else 0
    frames = []
    for start in range(0, len(x) - frame, hop):
        w = x[start:start + frame]
        if np.sqrt(np.mean(w ** 2)) < gate:
            frames.append([])
            continue
        mag = np.abs(np.fft.rfft(w * window))
        db = 20 * np.log10(mag + 1e-9)
        top = db[band].max()
        peaks = []
        for k in np.nonzero(band)[0]:
            if db[k] > top + floor_db and db[k] >= db[k - 1] and db[k] > db[k + 1]:
                # parabolic interpolation of the peak
                a, b, c = db[k - 1], db[k], db[k + 1]
                shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
                peaks.append(float(hz_midi((k + shift) * RATE / frame)))
        frames.append(peaks)
    return frames


def harmony(x, expected, harmonics=(12, 19, 24, 28)):
    """Check each lead note is joined by its expected harmony notes.

    ``expected`` maps a lead MIDI note to its intervals. A frame counts for
    a lead when that note is among its peaks; each expected interval must
    show up in at least 40 % of that lead's frames."""
    leads = {}
    for peaks in spectral_peaks(x):
        # The lead is the input note whose chord best explains the peaks:
        # several leads can be present at once as each other's harmony.
        def score(n):
            if not any(abs(p - n) < 0.35 for p in peaks):
                return -1
            return sum(any(abs(p - (n + i)) < 0.35 for p in peaks) for i in expected[n])
        lead = max(expected, key=score) if peaks else None
        if lead is None or score(lead) < 0:
            continue
        seen = {int(round(p - lead)) for p in peaks if abs((p - lead) - round(p - lead)) < 0.35}
        entry = leads.setdefault(lead, {"frames": 0, "counts": {}})
        entry["frames"] += 1
        for interval in seen - {0}:
            entry["counts"][interval] = entry["counts"].get(interval, 0) + 1
    result = {}
    ok = bool(leads)
    for lead, want in expected.items():
        entry = leads.get(lead)
        if not entry:
            result[lead] = {"heard": False, "want": want}
            ok = False
            continue
        frames = entry["frames"]
        found = {i: entry["counts"].get(i, 0) / frames for i in want}
        missing = [i for i, share in found.items() if share < 0.4]
        extra = sorted(i for i, c in entry["counts"].items()
                       if c / frames >= 0.4 and i not in want and i not in harmonics)
        if missing:
            ok = False
        result[lead] = {"heard": True, "frames": frames, "want": want,
                        "missing": missing, "extra": extra}
    return ok, result


def shifted(x, notes, semitones, tolerance=0.5):
    """Share of steady pitches that sit ``semitones`` from an input note."""
    m = pitch_report(x)
    if len(m) == 0:
        return 0.0, 0
    targets = [midi + cents / 100.0 + semitones for midi, cents in notes]
    hits = [min(abs(v - t) for t in targets) < tolerance for v in m]
    return float(np.mean(hits)), int(len(m))


def heard_notes(x, notes, tolerance=0.3):
    """Share of steady pitches that match one of the input notes."""
    return shifted(x, notes, 0, tolerance)


def describe(x):
    return {"seconds": round(len(x) / RATE, 2), "level_db": round(level_db(x), 1),
            "peak": int(np.abs(x.astype(np.int32)).max()) if len(x) else 0}


if __name__ == "__main__":
    # Write the test signals to a folder, to listen to them.
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    for name, samples in signals().items():
        write_wav(os.path.join(folder, name), samples)
        print(name, round(seconds_of(samples), 2), "s")
