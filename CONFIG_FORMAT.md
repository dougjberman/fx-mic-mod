# Shared preset format (proposal)

One format for the firmware, the GUI and the browser preview: **Adafruit's `config.json`,
plus one new effect type, `autotune`.** Adafruit's packs load unchanged.

Reference copies of Adafruit's files (MIT, Tim Cocks / Adafruit) are in
`fork/adafruit-reference/`: `code.py`, `config_loader.py` (the source of truth for
parameter names and types) and the demo `config.json`.

## File

`/config.json` on CIRCUITPY, one pack per file.

```json
{
  "name": "MYPACK",
  "presets": [ PRESET, PRESET, PRESET, PRESET ],
  "samples": [ SAMPLE, SAMPLE, SAMPLE, SAMPLE ]
}
```

- Up to 4 presets (TOP button, red LEDs; position 0 in the cycle is "clean").
- Up to 4 samples (MIDDLE selects, white LEDs; BOTTOM plays).

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

## Sample

```json
{"file": "1.wav", "playmode": "oneshot|hold|startstop", "blocks": {...}, "effects": [EFFECT, ...]}
```

## Effect

Every effect: `"effect": <type>`, optional `"mix"` (0..1), optional `"enabled": false`.
Any numeric param marked (B) accepts a number, a block object, or `"$name"`.
`"$handle"` is supplied by the firmware: handle travel 0.0 (out) .. 1.0 (squeezed).

| type | params |
|---|---|
| `autotune` **(new)** | `key` "C".."B" (sharps, e.g. "F#"), `scale` "chromatic" / "major" / "minor", `speed` (B) 0 = natural .. 1 = hard snap, `humanize` 0..1, `mix` (B), `grain_size` int (default 512), `density` int (default 2), `squeeze_key` / `squeeze_scale` (same values as key/scale: the key used while the handle is fully squeezed) |
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
  that key/scale when its travel reaches 0.8, and back to `key`/`scale` when it falls to 0.6 (the gap
  stops it flickering). A missing one of the pair keeps the resting value, e.g.
  `"key": "C", "scale": "major", "squeeze_scale": "minor"` goes C major → C minor.
  It combines with `"speed": "$handle"`.
- `speed` s (clamped 0..1) sets the fraction of the remaining correction applied per 16 ms block:
  `rate = 0.08 + 0.92 * s * s` (s = 0 is a ~200 ms glide, s = 1 snaps instantly).
- `humanize` h is a dead zone of `h * 30` cents: errors inside it are left alone, larger errors
  are corrected by (error - dead zone). So h = 0 corrects fully, h = 1 leaves up to 30 cents
  (vibrato, scoops) untouched. Source of truth: `firmware/autotune.py` `Autotune.step()`.

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
