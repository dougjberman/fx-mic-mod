# Shared preset format (proposal)

One format for the firmware, the GUI and the browser preview: **Adafruit's `config.json`,
plus three new effect types, `autotune`, `harmonizer` and `vocoder`.** Adafruit's packs load unchanged.

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
- TOP and MIDDLE step when let go, not when pressed, so holding TOP + MIDDLE to power off steps
  neither. The LEDs show the next spot as soon as the button comes up, and the step itself waits
  200 ms (`BUTTON_GAP_SECONDS`): a press in that time is a double tap, and goes back one preset,
  sample or page at once instead, without loading the next one first. Presses made while a preset
  is loading all count once it has loaded, and only the preset they end up on is loaded.
- `effects_only` **(new, optional)**: `true` trades the samples and record mode for
  more presets, up to 16 in pages of four. Page 1 is presets 1 to 4, page 2 is 5 to 8, and so on;
  the number of pages is however many the presets fill. TOP steps through the current page's
  presets (red LEDs), from the last back to the first, with no Clean stop. MIDDLE steps to the
  next page, from the last back to page 1, and the white LEDs count it (two lit is page 2). A page
  turn lands on the new page's first preset. A quick double tap on TOP or MIDDLE goes back a preset
  or a page instead.
  BOTTOM goes to Clean, every LED dark, and is the only way there; from Clean, TOP or MIDDLE goes
  to page 1, preset 1. The mic starts on Clean. `samples` and the sample settings are ignored but kept, so turning this off brings them
  back. Only the selected preset's effects are built, so 16 presets cost the mic only their JSON.
- `shake` **(new, optional)**: what shaking the mic does to a sample while it plays.
  `"zip"` (the default when the key is missing) speeds the echoes up so the sound zips,
  `"warble"` wobbles its pitch like worn tape, `"glitch"` stutters it shorter and shorter, and
  `"off"` (or `false`) does nothing. An object tunes a style:
  `{"style": "zip", "rate": 2.4, "feedback": 0.8, "mix": 0.3, "shortest_ms": 20, "longest_ms": 140}`
  (zip's own numbers)
  (sweeps a second, echo feedback 0 to 0.95, echo mix at a full shake 0 to 1, and the delay's range).
  Played through an autotune preset (`"sample_fx": "preset"`), any style is a stutter roll over the
  same range.
- `sample_fx` **(new, optional)**: where samples get their effects.
  - `"own"` (default, Adafruit's behavior): each sample runs through its own `effects`.
  - `"preset"`: samples play dry into the effect preset currently selected on the mic (TOP button), mixed with the mic ahead of the
    whole chain, autotune included. The handle still gates only the mic. Sample `effects` are
    ignored. On clean and in record mode samples play dry. On autotune presets the firmware
    streams the WAV into autotune itself, so the file must be 16-bit mono at the mic's rate, 24 kHz by default (the
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
brought down to match the others. Set it by ear on the mic (the editor's Try it meter helps compare
presets, but the browser preview only estimates the mic). It can only turn a preset down, because a
mixer voice can't go above 1.0. The defaults were set from recordings of a Ting against its Clean spot.

## Sample

```json
{"file": "1.wav", "playmode": "auto", "blocks": {...}, "effects": [EFFECT, ...]}
```

The BOTTOM button plays every sample the same way:
every press starts the sample at once, so a tap never waits.
- **Tap** (let go within 250 ms, `TAP_HOLD_SECONDS`): it plays through once.
- **Hold** (250 ms or longer): letting go stops it. It plays once, it doesn't loop.
- **Double tap** (a second press within 300 ms of a tap's press, `DOUBLE_TAP_SECONDS`): it cuts the
  first tap off and plays the sample backwards from the end; held, letting go stops it.

`playmode` is what chose this before. The editor writes `"auto"`. The firmware still reads `"oneshot"`,
`"hold"` and `"startstop"` (Adafruit's packs use them) but plays those slots the same way.
Backwards through autotune (`"sample_fx": "preset"` on an autotune preset) streams from flash;
everywhere else it streams backwards from the drive too, a piece at a time, in 16 KB of RAM whatever
the length.

Samples must be 16-bit mono WAVs at the mic's `sample_rate` (24 kHz unless set). The install guide's step 05 converts other WAVs to 24 kHz.

## Effect

Every effect: `"effect": <type>`, optional `"mix"` (0..1), optional `"enabled": false`.

`mix` is CircuitPython's own, and it isn't a crossfade for every effect. `echo`, `multi_tap_delay` and
`freeverb` keep the dry voice at full level up to mix 0.5 and bring the effect in at full by 0.5, so
0.25 is already an even blend. `chorus` always keeps the dry voice at full and adds the voices on top.
`freeverb`'s rooms are sized in samples for 44.1 kHz, and the mic runs at 24 kHz, so every room is
nearly twice as long as the same `roomsize` would be on a computer: small rooms want a low `roomsize`. The editor's preview follows all of this.
Any numeric param marked (B) accepts a number, a block object, or `"$name"`.
`"$handle"` is supplied by the firmware: 0.0 from rest until the squeeze has opened the mic and
gone a little further (the paddle click point plus a dead zone, so you can sing with a light squeeze), then up to
1.0 fully squeezed. The firmware learns the click point as you play. The dead zone is 0.4 of
the travel; a top-level `"handle_dead_zone": 0..0.5` in config.json changes it.

A top-level `"sample_rate"` sets the rate the whole mic runs at: 16000, 24000 (the default), 32000
or 48000. Higher rates keep more of the top end and cost more CPU and memory: at 32 kHz a Ting
crackles on a preset with autotune, distortion, echo and reverb, and at 24 kHz that preset needs to
lose its reverb. Samples must be WAVs at
that rate, and sample counts in presets (`grain_size`, `window`, `overlap`) are written for 16 kHz:
the mic scales them, so a grain keeps its length in time.

A top-level `"trim_takes"` (default true) cuts the silence before and after the sound in a take
recorded on the mic. Set it to false to keep everything between the squeeze and the release.

A top-level `"normalize_takes"` (default false) scales each take recorded on the mic so its loudest
moment sits at -3 dBFS, boosting by at most +12 dB so a near-silent take doesn't become loud hiss.

A top-level `"auto_off_minutes"` (0..240, default 10; 0 for never) turns the mic off once it has
sat that long unused on batteries: no button press, squeeze, knob turn, sample playing or take
recording. Every LED blinks for the last 10 seconds, and anything done then keeps it on. Plugged
into a computer it stays on. Needs firmware 1.1.0.

A top-level `"sample_level"` (0..1, default 1) turns every sample down. A take recorded on the mic
already sits at the voice's level; the install guide's converter brings imported files to about that
level too.

A top-level `"feed_phase"` (0 or 1, default 1) picks which half of the autotune stage's double buffer
is written while the other plays. If an autotune preset crackles steadily and clean ones don't, type
`f` and Enter in the install page's serial console to flip it live and compare.

| type | params |
|---|---|
| `autotune` **(new)** | `key` "C".."B" (sharps, e.g. "F#"), `scale` "chromatic" / "major" / "minor", `speed` (B) 0 = natural .. 1 = hard snap, `humanize` 0..1, `mix` (B), `grain_size` int (default 512), `density` int (default 2), `squeeze_key` / `squeeze_scale` (same values as key/scale: the key used while the handle is fully squeezed) |
| `harmonizer` **(new)** | `chord` (see below, default "major"), `inversion` int (default 0), `octave` int (default 0, moves the harmony by octaves), `level` (B) harmony volume against the voice, 0..1 (default 0.7), `double` "above" or "below" (optional: one more voice on your note an octave up or down; chords of three notes or fewer) |
| `vocoder` **(new)** | `waveform` "saw" / "square" / "pulse" / "saw_square" (default "saw"), `bands` int 4..11 (default 8; at most 8 when `mix` isn't a plain 1), `octave` int -2..2 (default 0), `breath` 0..1 (default 0), `chord` (optional: "key_triad", "key_seventh", "major", "minor", "7", "maj7", "m7", "power", "dim", "m7b5"), `gate` dB, -80..-20 (default -50; -80 = off), `squeeze_chord` (optional: a `chord` value, or "none" for one note), `mix` (B) vocoder against the voice, 0..1 (default 1 = vocoder only) |
| `pitch_shift` | `semitones` (B), `window` int and `overlap` int, both in **bytes** of 16-bit audio (so samples = bytes / 2, at 16 kHz before the rate scaling), `mix` (B) |
| `granular_pitch_shift` | `semitones` (B), `grain_size` int samples, `density` int 1..8, `spread` float 0..1 (fixed at load, not a block), `mix` (B) |
| `distortion` | `drive` (B), `pre_gain` (B, dB), `post_gain` (B, dB), `mode` = an `audiofilters.DistortionMode` name, case-insensitive ("clip", "lofi", "overdrive", "waveshape"; "overdrive" ignores `drive`, `pre_gain` sets how hard it is pushed), `soft_clip` bool, `mix` (B) |
| `filter` | `filter`: one or a list of `{"mode": <synthio.FilterMode name, e.g. "low_pass", "high_pass", "band_pass", "notch">, "frequency": (B) Hz, "Q": (B), "A": (B) shelf/peak gain}`, `mix` (B) |
| `phaser` | `frequency` (B) Hz, `feedback` (B; the mic holds it to 0.1..0.9), `stages` int, `mix` (B) |
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
- `double` adds one more voice singing the sung note an octave above (`"above"`) or below
  (`"below"`), whatever the chord's inversion or `octave`. It takes the third harmony voice, so it
  works on chords of three notes or fewer (thirds, fifths, `power`, triads, `key_third`,
  `key_triad`), and only one octave at a time.
- `level` l: the voice plays at `1 / (1 + n * l)` and each of the n harmony voices at
  l times that, so the sum stays about as loud as the voice alone (the voices are copies of one
  voice, so they add up nearly in phase). `"$handle"` brings the
  harmony in with the squeeze, which can't be combined with `squeeze_key` / `squeeze_scale`.
  Source of truth: `firmware/autotune.py` (`CHORDS`, `voicing`, `Autotune`).

### vocoder rules
- A channel vocoder whose carrier is a synth playing the note you sing (pitch tracking), so the mic
  needs no second input: your words, played by the synth. With an `autotune` in the preset the
  carrier plays the corrected note (it follows `key`, `scale` and `speed`); without one it follows
  the voice as sung. Between words (no pitch found) it holds the last note.
- At most one per preset, and not in the same preset as a `harmonizer`. Not allowed in sample
  `effects`. Where it sits in `list` doesn't matter: it runs inside the autotune stage.
- `bands` band-pass channels, spaced evenly in pitch from 180 Hz to 5 kHz (or 0.4 x the rate).
  More bands make the words clearer and cost a little more CPU. Each band is a synthio note: the
  carrier waveform (band-limited, 48 harmonics) through its own band-pass filter, its loudness set
  from the voice's level in that band. The mic measures the voice with an FFT every 32 ms, on the
  blocks between pitch searches, so the vocoder adds no work to the busiest block.
- `saw_square` is a saw and a square at the same note, summed, as some vocoder players use for a
  fuller carrier. It is one waveform, so it costs no more than either alone.
- `chord` makes the carrier play a chord on the sung note (the root), as when a vocoder's carrier
  is a synth chord. The notes are in just intonation, whole multiples of one sub-note (major
  4:5:6, minor 10:12:15, 7 4:5:6:7, maj7 8:10:12:15, m7 10:12:15:18, power 2:3:4), so the whole
  chord repeats at that sub-note and fits in one waveform: no more CPU than one note. Next to
  equal temperament the major 3rd is 14 cents lower and the 5th 2 cents higher. `dim` and
  `m7b5` are 5:6:7 and 5:6:7:9, the smooth "septimal" tuning.
- `chord` `"key_triad"` or `"key_seventh"` follows the autotune key, like the harmonizer's key
  chords: each snapped note gets the triad or 7th chord the scale builds on it (in C major, C gets
  C major, D gets D minor, B gets B diminished; with 7ths, G gets G7). A note off the scale gets a
  major chord (a dominant 7th). They need autotune in a major or minor key; otherwise C major.
- `gate` is a noise gate, in dB from full scale, on the voice's level across the bands (180 Hz
  up, so low hum barely counts). Below it the bands close, so a fan or air conditioner doesn't
  play the synth; they close 6 dB under it after about 130 ms, so word ends ring out. Raise it
  if the synth hums with no one singing; -80 or lower turns it off.
- `squeeze_chord` is the chord a full squeeze switches to, and easing off switches back (at the
  same points as autotune's `squeeze_key`: in at 0.67 of the handle's travel, back at 0.5). With
  autotune on `"chromatic"` and `chord` `"major"`, `squeeze_chord` `"minor"` picks major or minor
  on any note you sing. The handle does one job, so this can't go with `squeeze_key`,
  `squeeze_scale` or a `"$handle"` mix. Both chords' tables are built when the preset loads, so
  switching costs no CPU.
- `octave` moves the carrier by whole octaves from the note. `breath` adds high-passed noise
  (above 4 kHz) that follows the voice's highs, so s, t and f sounds come through.
- `mix` m: the voice plays at `1 - m` and the vocoder at `m`. A plain `1` (the default) leaves the
  voice out altogether, and then the mic doesn't run the voice's pitch shifter at all. `"$handle"`
  brings the vocoder in with the squeeze, which can't be combined with `squeeze_key` / `squeeze_scale`.
  With the voice in (any `mix` but a plain `1`), its shifter runs beside the synth and `bands` stops
  at 8: on a Ting, 11 bands plus the voice overflowed, while 8 plus the voice, or 11 alone, ran clean.
  Source of truth: `firmware/autotune.py` (`Vocoder`, `VOCODER_*`).

## Firmware behavior the preview mirrors
- TOP+MIDDLE power-off hold is **0.8 s** (`SHUTDOWN_HOLD_SECONDS` in Adafruit's code.py; its
  docstring says 500 ms but the constant wins).
- Omitted effect params use the CircuitPython constructor defaults.

## Firmware versions
The firmware's release number is `FIRMWARE_VERSION` in `code.py` (it prints at boot). The launch
release is **1.0.0**, and every release from it on has three parts. Builds numbered 1.0 to 1.7.1
came before the launch: the install page and the editor count them as older than 1.0.0 (their
`PRELAUNCH` list), so a mic on one gets offered the update. The earliest builds have no number.
To keep configs and firmware working together after release:
- New firmware always reads older configs. A setting the editor drops (like the harmonizer's
  `octave`) stays readable on the mic.
- A config setting added after a release raises `FIRMWARE_VERSION` and gets a row in the
  editor's `FIRMWARE_NEEDS` table with that number. Save to mic reads the mic's `code.py`
  and refuses to write a config that uses a setting the mic's firmware is too old for.
- The firmware skips an effect parameter it doesn't know, with a console line
  (`... has no parameter x; ignoring it`), instead of failing the whole preset.
  Unknown top-level keys were always ignored.

## Blocks
- `{"block": "lfo", "waveform": "sine|triangle|square|saw|ramp_down", "rate", "scale", "offset", "phase_offset", "once", "interpolate"}`
- `{"block": "math", "operation": <synthio.MathOperation name, e.g. "constrained_lerp">, "a", "b", "c"}`
- Name a block under `"blocks"` and refer to it as `"$name"` to share it between params.

## Not in the file
Buffer sizes, mic gain and volume are firmware constants, never in the JSON.
