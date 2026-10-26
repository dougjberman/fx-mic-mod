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

## Needs checking on the device
- **Handle start point:** `"$handle"` reads 0 until the squeeze passes the paddle click plus
  the dead zone (0.25, or `"handle_dead_zone"` in config.json). The console prints "handle opens the
  mic at 0.xx" on each squeeze; check it settles near the real click, then try dead zones from about
  0.1 to 0.25 and keep whichever lets you sing with a light squeeze without nudging the effect.
- **Harmonizer CPU:** autotune plus three harmony voices is four granular shifters. Watch for
  `overflow` with a four-note chord, and try `density` 1 on autotune if it can't keep up.
- **Harmonizer feeds:** several `RawSample`s over one buffer must stay in step. Listen for
  flamming or a comb sound on the harmony; if so, the shifters may need one shared feed.
- **CPU:** autotune plus 2-3 effects. Watch the serial console for `overflow`.
- **Buffer size:** autotune presets build effects with 512-byte buffers (`AUTOTUNE_FORMAT`),
  Adafruit's use 1024. Try both if audio stutters.
- **FEED_PHASE** in autotune.py (default 1): some loads of an autotune preset crackle all the way
  through and others don't. Type `f` and Enter in the serial console to flip it live and hear
  whether the other value cures that load. The two-block window (`null`) crackled on every load.
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
- **Samples through the preset** (`"sample_fx": "preset"`): non-autotune presets get a second
  `audiomixer.Mixer` at the chain input. Autotune presets stream the WAV from flash 512 bytes per
  block and mix it in Python before pitch detection. Check for crackle or `overflow` while a
  sample plays on an autotune preset, and that a 1024-byte effect chain accepts the mixer.
- **Squeeze key threshold:** check the handle really reaches 0.67 travel on a firm squeeze
  (`handle_control.a` on the serial console). Lower `SQUEEZE_KEY_ON` / `SQUEEZE_KEY_OFF` if not.
- **LFO-driven speed/humanize:** only plain numbers and `"$handle"` are verified. A synthio LFO
  only updates while a native effect ticks it.
