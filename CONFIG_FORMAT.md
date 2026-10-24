# Shared preset format (proposal)

One format for the firmware, the GUI and the browser preview: **Adafruit's `config.json`,
plus two new effect types, `autotune` and `harmonizer`.** Adafruit's packs load unchanged.

Reference copies of Adafruit's files (MIT, Tim Cocks / Adafruit) are in
`fork/adafruit-reference/`: `code.py`, `config_loader.py` (the source of truth for
parameter names and types) and the demo `config.json`.

## File

`/config.json` on CIRCUITPY, one pack per file.

```json
{
  "name": "MYPACK",
  "sample_fx": "preset",
  "presets": [ PRESET, PRESET, PRESET, PRESET ],
  "samples": [ SAMPLE, SAMPLE, SAMPLE, SAMPLE ]
}
```

- Up to 4 presets (TOP button, red LEDs; position 0 in the cycle is "clean").
- Up to 4 samples (MIDDLE selects, white LEDs; BOTTOM plays).
- `sample_fx` **(new, optional)**: where samples get their effects.
  - `"own"` (default, Adafruit's behaviour): each sample runs through its own `effects`.
  - `"preset"`: samples play dry into the effect preset currently selected on the mic (TOP button), mixed with the mic ahead of the
    whole chain, autotune included. The handle still gates only the mic. Sample `effects` are
    ignored. On clean and in record mode samples play dry. On autotune presets the firmware
    streams the WAV into autotune itself, so the file must be 16-bit mono 16 kHz (the
    recorder writes that).

## Preset

```json
{
  "name": "T-Pain",
  "comment": "optional free text",
  "blocks": { "sweep": {"block": "lfo", "waveform": "sine", "rate": 1.5, "scale": 1000, "offset": 2000} },
  "list": [ EFFECT, EFFECT, ... ]
}
```

`list` runs top to bottom. `name`/`comment` are new-ish: Adafruit's tool writes them, their
firmware ignores them; we show `name` in the GUI.

`level` **(new, optional)**: the preset's output level, `0.0` to `1.0`, default `1.0`. The
firmware sets the output mixer voice to it whenever the mic is open on this preset (with
`sample_fx: "preset"` it scales the sample too). It's how a loud chain, like distortion, is
brought down to match the others. The editor's **Balance preset volume outputs** button sets it by running the
sample singer through each preset and turning the louder ones down to match the quietest.
It can only turn a preset down, because a mixer voice can't go above 1.0.

## Sample

```json
{"file": "1.wav", "playmode": "oneshot|hold|startstop", "blocks": {...}, "effects": [EFFECT, ...]}
```

Samples must be 16-bit mono WAVs at the mic's `sample_rate` (16 kHz unless set). The install guide's step 05 converts other WAVs to 16 kHz.

## Effect

Every effect: `"effect": <type>`, optional `"mix"` (0..1), optional `"enabled": false`.

`mix` is CircuitPython's own, and it isn't a crossfade for every effect. `echo`, `multi_tap_delay` and
`freeverb` keep the dry voice at full level up to mix 0.5 and bring the effect in at full by 0.5, so
0.25 is already an even blend. `chorus` always keeps the dry voice at full and adds the voices on top.
`freeverb`'s rooms are sized in samples for 44.1 kHz, and the mic runs at 16 kHz, so every room is
nearly three times longer than the same `roomsize` would be on a computer: small rooms want `roomsize`
near 0. The editor's preview follows all of this.
Any numeric param marked (B) accepts a number, a block object, or `"$name"`.
`"$handle"` is supplied by the firmware: 0.0 from rest until the squeeze has opened the mic and
gone a little further (the paddle click point plus a dead zone, so you can sing with a light squeeze), then up to
1.0 fully squeezed. The firmware learns the click point as you play. The dead zone is 0.25 of
the travel; a top-level `"handle_dead_zone": 0..0.5` in config.json changes it.

A top-level `"sample_rate"` sets the rate the whole mic runs at: 16000 (the default), 24000, 32000
or 48000. Higher rates keep more of the top end and cost more CPU and memory. Samples must be WAVs at
that rate, and sample counts in presets (`grain_size`, `window`, `overlap`) are written for 16 kHz:
the mic scales them, so a grain keeps its length in time.

| type | params |
|---|---|
| `autotune` **(new)** | `key` "C".."B" (sharps, e.g. "F#"), `scale` "chromatic" / "major" / "minor", `speed` (B) 0 = natural .. 1 = hard snap, `humanize` 0..1, `mix` (B), `grain_size` int (default 512), `density` int (default 2), `squeeze_key` / `squeeze_scale` (same values as key/scale: the key used while the handle is fully squeezed) |
| `harmonizer` **(new)** | `chord` (see below, default "major"), `inversion` int (default 0), `octave` int (default 0, moves the harmony by octaves), `level` (B) harmony volume against the voice, 0..1 (default 0.7) |
| `pitch_shift` | `semitones` (B), `window` int, `overlap` int, `mix` (B) |
| `granular_pitch_shift` | `semitones` (B), `grain_size` int, `density` int, `spread` float, `mix` (B) |
| `distortion` | `drive` (B), `pre_gain` (B, dB), `post_gain` (B, dB), `mode` = an `audiofilters.DistortionMode` name, case-insensitive (e.g. "clip", "lofi", "overdrive", "wavefold"), `soft_clip` bool, `mix` (B) |
| `filter` | `filter`: one or a list of `{"mode": <synthio.FilterMode name, e.g. "low_pass", "high_pass", "band_pass", "notch">, "frequency": (B) Hz, "Q": (B), "A": (B) shelf/peak gain}`, `mix` (B) |
| `phaser` | `frequency` (B) Hz, `feedback` (B), `stages` int, `mix` (B) |
| `chorus` | `max_delay_ms` int, `delay_ms` (B), `voices` (B), `mix` (B) |
| `echo` | `max_delay_ms` int, `delay_ms` (B), `decay` (B), `freq_shift` bool, `mix` (B) |
| `multi_tap_delay` | `max_delay_ms` int, `delay_ms` (B), `decay` (B), `taps` list of `pos` or `[pos, level]`, `mix` (B) |
| `freeverb` | `roomsize` (B), `damp` (B), `mix` (B) |

Aliases accepted by the loader: `reverb` = freeverb, `delay` = echo.
Exact enum values for `distortion.mode` and `filter.mode` come from `config_loader.py`.

### autotune rules
- Must be the **first** enabled effect in a preset `list` (it needs the dry voice to detect pitch).
  The firmware moves it to the front if it is not; the GUI enforces it.
- At most one per preset. Not allowed in sample `effects`.
- Defaults: `key` "C", `scale` "chromatic", `speed` 1.0, `humanize` 0, `mix` 1.
- Squeeze to change key: if `squeeze_key` or `squeeze_scale` is set, the handle switches autotune to
  that key/scale when its travel reaches 0.67 (two-thirds), and back to `key`/`scale` when it falls to 0.5 (the gap
  stops it flickering). A missing one of the pair keeps the resting value, e.g.
  `"key": "C", "scale": "major", "squeeze_scale": "minor"` goes C major → C minor.
  It can't be combined with `"$handle"` in `speed`, `humanize` or `mix` (directly or inside a
  block): the handle gets one job besides opening the mic, and the loader rejects both.
- `speed` s (clamped 0..1) sets the fraction of the remaining correction applied per 16 ms block:
  `rate = 0.08 + 0.92 * s * s` (s = 0 is a ~200 ms glide, s = 1 snaps instantly).
- `humanize` h is a dead zone of `h * 30` cents: errors inside it are left alone, larger errors
  are corrected by (error - dead zone). So h = 0 corrects fully, h = 1 leaves up to 30 cents
  (vibrato, scoops) untouched. Source of truth: `firmware/autotune.py` `Autotune.step()`.

### harmonizer rules
- Sings a chord on every note: the sung note is the root, and each other chord note is one more
  pitch shifter fed the same voice. It runs inside the autotune stage, so the harmony follows the
  corrected note. Without an `autotune` in the preset the voice is left untuned and the harmony
  follows it as sung.
- At most one per preset. Not allowed in sample `effects`. Where it sits in `list` doesn't matter.
- `chord` names (semitones above the sung note): `octave` 0 12, `maj3` 0 4, `min3` 0 3, `fifth` 0 7, `power` 0 7 12,
  `major` 0 4 7, `minor` 0 3 7, `dim` 0 3 6, `aug` 0 4 8, `sus2` 0 2 7, `sus4` 0 5 7,
  `6` 0 4 7 9, `m6` 0 3 7 9, `6/9` 0 4 9 14, `maj7` 0 4 7 11, `7` 0 4 7 10, `m7` 0 3 7 10,
  `m_maj7` 0 3 7 11, `m7b5` 0 3 6 10, `dim7` 0 3 6 9, `7sus4` 0 5 7 10, `aug7` 0 4 8 10,
  `aug_maj7` 0 4 8 11, `add9` 0 4 7 14, `m_add9` 0 3 7 14, `add11` 0 4 7 17, `maj9` 0 4 11 14,
  `9` 0 4 10 14, `m9` 0 3 10 14, `7b9` 0 4 10 13, `7#9` 0 4 10 15, `maj7#11` 0 4 11 18,
  `11` 0 10 14 17, `m11` 0 3 10 17, `13` 0 4 10 21, `maj13` 0 4 11 21, `m13` 0 3 10 21,
  `quartal` 0 5 10 15. Four notes at most (three harmony voices), so five-note chords leave out
  the 5th.
- Key chords follow the autotune key instead: `key_third` (a 3rd above in the scale),
  `key_triad`, `key_seventh`, built in scale steps on the note autotune snapped to (E in C major
  gives E minor). They need autotune in a major or minor key to know which 3rd is meant; with
  "Every note" (or no autotune) the firmware falls back to the key's major scale, and the editor
  doesn't offer them. A note outside the scale gets
  0 4 7 10 shapes.
- `inversion` n lifts the n lowest chord notes an octave, then drops the chord an octave, so the
  voice stays in the chord and n notes move below it: C major sung on C is E G above (0),
  E G below (1), G below and E above (2). Up to the number of harmony voices; chords with an
  octave in them (`octave`, `power`) only allow 0.
- `level` l: the voice plays at `1 / sqrt(1 + n * l * l)` and each of the n harmony voices at
  l times that, so the sum stays about as loud as the voice alone. `"$handle"` brings the
  harmony in with the squeeze, which can't be combined with `squeeze_key` / `squeeze_scale`.
  Source of truth: `firmware/autotune.py` (`CHORDS`, `voicing`, `Autotune`).

## Firmware behaviour the preview mirrors
- TOP+MIDDLE power-off hold is **0.8 s** (`SHUTDOWN_HOLD_SECONDS` in Adafruit's code.py; its
  docstring says 500 ms but the constant wins).
- Omitted effect params use the CircuitPython constructor defaults.

## Blocks
- `{"block": "lfo", "waveform": "sine|triangle|square|saw|ramp_down", "rate", "scale", "offset", "phase_offset", "once", "interpolate"}`
- `{"block": "math", "operation": <synthio.MathOperation name, e.g. "constrained_lerp">, "a", "b", "c"}`
- Name a block under `"blocks"` and refer to it as `"$name"` to share it between params.

## Not in the file
Sample rate (16 kHz), buffer sizes, mic gain and volume are firmware constants, never in the JSON.
