# Firmware notes

## Files
- `code.py`: Adafruit's demo main loop, extended with autotune presets and record mode.
- `config_loader.py`: Adafruit's JSON loader plus `create_preset()` / `is_autotune()`.
- `autotune.py`: pitch detect + snap + native shifter stage (from this project's autotune firmware).
- `recorder.py`: RAM recorder and WAV writer.
- `boot.py`: picks record mode or edit mode at power-on.
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
A `vocoder` effect also joins the autotune stage. Its carrier is a `synthio.Synthesizer` with one
`Note` per band: a band-limited saw / square / pulse / saw+square table through that note's own band-pass
`synthio.Biquad`, all at the tracked note (the corrected note when the preset has autotune).
Python only measures the voice: on the blocks between pitch searches it takes the newest
power-of-two samples of the pitch window (512 at 16 or 24 kHz), runs a ulab FFT, sums the bins of
each band and sets each band note's amplitude. With `mix` 1 nothing plays the voice, so the stage
builds no shifter and the synth heads the chain; otherwise a two-voice mixer sums voice and synth.
A `chord` carrier costs nothing extra: the chord's notes are whole-number multiples of a lower
note (minor is 10:12:15), so one longer table holds the whole chord and each band note plays it
at the sung note divided by the root's multiple.

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
  Python side of one measurement (FFT and band sums); it should stay well under the pitch search
  (`autotune.bench()`). The synth side is native: 8 bands is 8 synthio notes with a biquad each.
  Watch for `overflow` with 11 bands plus breath, and with the vocoder before a reverb. If blocks
  overrun, try fewer bands; `VOCODER_EVERY_BLOCK = True` gives smoother bands for more CPU.
- **Vocoder APIs:** written for CircuitPython 10's `synthio.Note(filter=synthio.Biquad(...))` and
  `ulab.utils.spectrogram` (or `ulab.numpy.fft.fft`). A preset that fails to build falls back to
  clean and prints why.
- **Vocoder level:** `VOCODER_GAIN` (0.7) puts the vocoder about level with the voice in the
  browser preview. Check it against the Clean spot on the mic, and that a loud voice doesn't hit
  the synth's soft clip. Band levels step every 32 ms; listen for zipper noise on held vowels.
- **Chord tables:** a chord carrier's table is 128 samples per multiple of its top note, so up to
  2304 samples for `m7`. Check synthio takes waveforms that long, and that low sung notes don't
  alias (the sub-note can sit below 20 Hz, which synthio should play fine).
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
- **Squeeze key threshold:** check the handle really reaches 0.67 travel on a firm squeeze
  (`handle_control.a` on the serial console). Lower `SQUEEZE_KEY_ON` / `SQUEEZE_KEY_OFF` if not.
- **LFO-driven speed/humanize:** only plain numbers and `"$handle"` are verified. A synthio LFO
  only updates while a native effect ticks it.
