# Firmware notes

## Files
- `code.py`: Adafruit's demo main loop, extended with autotune presets and record mode.
- `config_loader.py`: Adafruit's JSON loader plus `create_preset()` / `is_autotune()`.
- `autotune.py`: pitch detect + snap + native shifter stage (from this project's autotune firmware).
- `recorder.py`: RAM recorder and WAV writer.
- `boot.py`: picks record mode or edit mode at power-on.
- `shake.py`: the accelerometer, and the shake effect on sample playback.
- `selftest.py`: on-device checks for `tools/ting_test.py`; never runs at power-on.

## How an autotune preset runs
Python calls `mic.record()` every 16 ms, detects pitch, and writes the block into a double-buffered
`RawSample` that feeds `audiodelays.GranularPitchShift`. The preset's other effects play from the
shifter. Presets without autotune hand the mic straight to the native chain, like Adafruit's demo,
with no extra CPU. `speed` / `humanize` accept a number or `"$handle"`. With `squeeze_key` /
`squeeze_scale`, the main loop switches the key when the handle passes 0.67 (two-thirds) and back at 0.5.

## How the harmonizer runs
A `harmonizer` effect adds one `GranularPitchShift` per harmony voice (up to three) to the autotune
stage. Each reads the same Python-written blocks through its own `RawSample` over the same buffer,
and an `audiomixer.Mixer` sums them with the corrected voice; that mixer is the stage's output.
Each block sets every harmony shifter to the autotune correction plus its chord interval. Without
autotune in the preset the stage runs with no correction (and skips pitch detection unless a key
chord needs the note).

## How the vocoder runs
A `vocoder` effect also joins the autotune stage. On the blocks between pitch searches Python
takes the newest power-of-two samples of the pitch window (512 at 16 or 24 kHz), runs a ulab FFT
and sums the bins of each band into a level. A channel vocoder is the carrier through each band's
band-pass at that band's level; the carrier repeats at the note, so the sum is one waveform too.
Since 1.5 Python works that waveform out: each of the carrier's 48 harmonics times the bands'
`synthio` band-pass response at that harmonic (worked out again only when the note moves 1%),
weighted by the levels, then a 256-point ulab inverse FFT into one of two tables, and one plain
`synthio.Note` per carrier note plays it at the tracked note. The breath noise loops, so its
high-pass is run once at load and baked into its table. So the synth runs no `Biquad` at all:
on a Ting a Note and Biquad per band (1.1 to 1.4) beside the voice's pitch shifter overflowed at
8 bands. `VOCODER_FILTERS = True` in autotune.py plays it that way still, to compare.
With `mix` 1 nothing plays the voice, so the stage builds no shifter and the synth heads the
chain; otherwise a two-voice mixer sums voice and synth. A `chord` carrier is a note per chord
note, in just intonation (minor is 10:12:15 of a lower note), each with its own waveform from the
same band levels. A key chord (`key_triad`, `key_seventh`) picks the chord the scale gives the
snapped note on each pitch search, pressing or letting go of notes as the chord's size changes.

## How the shake runs
Shaking the mic while a sample plays mixes in one `audiodelays.Echo` (`"shake"` in config.json:
`zip` by default, `warble`, `glitch` or `off`). It sits last on the sample's way to its mixer voice,
after the sample's own effects and before the preset it plays into, and never in the mic's chain.
A `synthio.LFO` sweeps its delay; Python only sets its mix from the shake (0 when still, where the
Echo copies the sample straight through). Zip and warble use `freq_shift`, so a changing delay
bends the repeats' pitch instead of clicking; glitch is a plain Echo whose delay steps down.
The Shake FX Lab (a page made in the project thread) runs the same Echo step for step at 24 kHz.
Through an autotune preset the sample is streamed by Python, so there the shake is a stutter roll:
`WaveReader.roll()` repeats the last stretch of the file, shorter on each sweep. That only moves
the file position, so it adds no audio work to the autotune pass.

## Needs checking on the device
- **Handle start point:** `"$handle"` reads 0 until the squeeze passes the paddle click plus
  the dead zone (0.25, or `"handle_dead_zone"` in config.json). The console prints "handle opens the
  mic at 0.xx" on each squeeze; check it settles near the real click, then try dead zones from about
  0.1 to 0.25 and keep whichever lets you sing with a light squeeze without nudging the effect.
- **Harmonizer CPU:** autotune plus three harmony voices is four granular shifters. Watch for
  `overflow` with a four-note chord, and try `density` 1 on autotune if it can't keep up.
- **Harmonizer feeds:** several `RawSample`s over one buffer must stay in step. Listen for
  flamming or a comb sound on the harmony; if so, the shifters may need one shared feed.
- **Vocoder CPU:** `import autotune; autotune.bench_vocoder()` on the serial console times the
  Python side of one block on a held note and on a new note (`bench_vocoder(chord="minor")` for
  three notes). Both should stay under the pitch search (`autotune.bench()`). The synth side is
  one plain note per carrier note plus breath. Watch for `overflow` on Night call, squeezed and not.
- **Vocoder APIs:** written for CircuitPython 10's `synthio`, `ulab.utils.spectrogram` (or
  `ulab.numpy.fft.fft`) and `ulab.numpy.fft.ifft` (either the (real, imaginary) pair CircuitPython
  builds have, or complex numbers). Without `ifft` the vocoder falls back to a Biquad per band.
  A preset that fails to build falls back to clean and prints why.
- **Vocoder level:** `VOCODER_GAIN` (0.7) puts the vocoder about level with the voice in the
  browser preview. Check it against the Clean spot on the mic, and that a loud voice doesn't hit
  the synth's soft clip. Band levels step every 32 ms; listen for zipper noise on held vowels.
- **Vocoder gate:** -50 dB (`VOCODER_GATE`) is a starting guess, tried only in the preview with a
  synthetic hum. Check that the mic's own noise floor sits well under it, and that quiet singing
  still opens it.
- **Waveform swaps:** each band update writes a new 256-sample table and points the note at it
  (`Note.waveform`) every 32 ms. Listen for zipper noise or clicks on held vowels against 1.4.
- **CPU:** autotune plus 2-3 effects. Watch the serial console for `overflow`.
- **Buffer size:** autotune presets build effects with 512-byte buffers (`AUTOTUNE_FORMAT`),
  Adafruit's use 1024. Try both if audio stutters.
- **Render counter** (PROBE_RATE in autotune.py): the shifters always go through a mixer whose
  voice level is a synthio LFO with scale 0. Its phase only moves when the mixer renders, so
  Python counts renders and knows which feed half the next one reads first (a RawSample flips
  halves on every pull and never resets to half 0). Each write puts the new block where the next
  render reads it second and the block before it where it reads first. The console prints
  `feed sync: counting renders, …; every N ms` on each load; `render counter off: …` means it
  fell back to the timed sync below. `f` still flips the halves. The counter also times the
  renders: a write less than 4 ms before one waits for it (with 1.5 ms of hysteresis),
  since on the harmonizer the render lands about 1 ms after each mic block, right on the write.
  The window was half a block, but those waits ran to 10 ms, and with a sample plus harmony or
  echoes the pass overran 16 ms: the mic dropped a block (the sample played slow, and crackled).
  When a wait is longer than the pitch work, the pitch work also runs in it, before the write.
  `overflow (… ms of 16, plus … waiting for the chain; … spare, … outside)` shows the pass before:
  "spare" near 16 means a block was lost just before it.
- **FEED_PHASE / FEED_SYNC** in autotune.py (fallback): the chain reads both feed halves in one burst every
  two blocks (half 0 first). At each load the sync times the burst and the mic's block edges; a
  write due less than half a block before a burst waits for it, so every write has half a block
  of slack. The console prints `feed sync: … ; waits for it` or `writes ahead`. If a load still
  crackles, type `f` and Enter to flip it, and keep that load's line.
- **Recording after playback:** record mode stops the mic feeding the mixer, then calls
  `mic.record()`. Check that the first block of a take is not stale audio from before the squeeze
  (the 50 ms trim and 0.1 s settle should cover it).
- **Take length:** `RECORD_SECONDS = 4` uses 128 KB of RAM. The serial console prints free memory
  on entering record mode; raise it if there is room.
- **Flash write time:** saving a 4 s take writes 128 KB. While it saves, one LED at a time runs down
  the mic (red 1 to white 4), at least one full sweep.
- **Take trim:** silence around the sound is cut, keeping 10 ms before the first sample over
  about -35 dBFS (`ONSET_LEVEL` in recorder.py) and 150 ms after the last. If quiet starts get
  clipped, lower it; if hum stops the trim, raise it. `"trim_takes": false` turns it off. A take
  ended by letting go always loses its last 100 ms (`RELEASE_MS`), the release click.
- **Take normalize** (`"normalize_takes": true`, off by default): each take is scaled so its peak
  sits at -3 dBFS (`NORMALIZE_PEAK`), boosting by at most +12 dB (`NORMALIZE_MAX_GAIN`). It runs
  before saving, so check the save time on a 4 s take; it uses ulab when the build has it.
- **Samples through the preset** (`"sample_fx": "preset"`): non-autotune presets get a second
  `audiomixer.Mixer` at the chain input. Autotune presets stream the WAV from flash 512 bytes per
  block and mix it in Python before pitch detection. Check for crackle or `overflow` while a
  sample plays on an autotune preset, and that a 1024-byte effect chain accepts the mixer.
- **Sample button timing:** `TAP_HOLD_SECONDS` (0.25) and `DOUBLE_TAP_SECONDS` (0.3) in code.py
  are first guesses. The console prints `stopped 1 (held N ms)` on a hold, so try quick taps (they
  must keep playing), short deliberate holds (they must stop), and relaxed double taps (they must
  play backwards); move the numbers to whatever feels natural and the editor's `TAP_HOLD_MS` /
  `DOUBLE_TAP_MS` to match. Timing uses keypad's own event timestamps, so a slow loop pass doesn't
  skew it.
- **Backwards samples:** off autotune, the first double tap loads the WAV backwards into RAM and
  prints `turned round in N ms`; that delay is heard once per slot. A 4 s take is 192 KB at 24 kHz,
  so check it fits next to a busy preset (`can't play backwards: memory` means it didn't, and it
  plays forwards). Through autotune it streams backwards from flash, one seek per 16 ms block:
  watch for `overflow` or crackle while a sample plays backwards on a harmonizer or vocoder preset,
  where the pass is tightest.
- **Accelerometer:** the board says only "accelerometer at 0x18". shake.py speaks the LIS3DH
  register set (WHO_AM_I 0x33, also LIS2DH12) and Silan's SC7A20 (0x11), which copies it. The
  console prints `shake: LIS3DH found` or `shake: no accelerometer (...)` with the id it read; an
  unknown id leaves the shake off and everything else as it was.
- **Shake feel:** `SHAKE_FROM_G` (0.5 g off gravity) and `SHAKE_FULL_G` (1.6 g) are guesses. Check that
  pressing BOTTOM, squeezing and walking around don't trigger it, and that a firm shake reaches
  full. The amount rises in 30 ms and falls over 250 ms (`ATTACK_SECONDS` / `RELEASE_SECONDS`).
- **Shake CPU:** a freq_shift Echo writes longest / shortest buffer cells per output sample (about
  11 at zip's shortest), only while shaken. Watch for `overflow` shaking a sample into Dream swirl,
  the heaviest native preset. Each shake clears the Echo's buffer by flipping `freq_shift` and back
  (12 KB memset); listen for a click at the start of a shake.
- **Shake roll on autotune:** listen for crackle as the file seeks back each stretch, and check that a
  roll on a harmonizer or vocoder preset doesn't overflow.
- **Squeeze key threshold:** check the handle really reaches 0.67 travel on a firm squeeze
  (`handle_control.a` on the serial console). Lower `SQUEEZE_KEY_ON` / `SQUEEZE_KEY_OFF` if not.
- **Effects-only pages** (`"effects_only": true`): MIDDLE rebuilds the preset at the same slot on
  the next page, so a page turn sounds like a TOP press (a muted switch, one `overflow` cleared).
  The console prints `page N of M, preset K of 4: name, … bytes free` on each change; check free
  memory with 16 presets against a 4-preset pack (each preset's JSON stays in RAM, its effects
  don't), and that TOP + MIDDLE still powers off.
- **LFO-driven speed/humanize:** only plain numbers and `"$handle"` are verified. A synthio LFO
  only updates while a native effect ticks it.
