# Firmware notes

## Files
- `code.py`: Adafruit's demo main loop, extended with autotune presets and record mode.
- `config_loader.py`: Adafruit's JSON loader plus `create_preset()` / `is_autotune()`.
- `autotune.py`: pitch detect + snap + native shifter stage (from this project's autotune firmware).
- `recorder.py`: RAM recorder and WAV writer.
- `boot.py`: picks record mode or edit mode at power-on.

## How an autotune preset runs
Python calls `mic.record()` every 16 ms, detects pitch, and writes the block into a double-buffered
`RawSample` that feeds `audiodelays.GranularPitchShift`. The preset's other effects play from the
shifter. Presets without autotune hand the mic straight to the native chain, like Adafruit's demo,
with no extra CPU. `speed` / `humanize` accept a number or `"$handle"`.

## Needs checking on the device
- **CPU:** autotune plus 2-3 effects. Watch the serial console for `overflow`.
- **Buffer size:** autotune presets build effects with 512-byte buffers (`AUTOTUNE_FORMAT`),
  Adafruit's use 1024. Try both if audio stutters.
- **FEED_PHASE** in autotune.py: flip it between 0 and 1 if there is steady crackle.
- **Recording after playback:** record mode stops the mic feeding the mixer, then calls
  `mic.record()`. Check that the first block of a take is not stale audio from before the squeeze
  (the 50 ms trim and 0.1 s settle should cover it).
- **Take length:** `RECORD_SECONDS = 4` uses 128 KB of RAM. The serial console prints free memory
  on entering record mode; raise it if there is room.
- **Flash write time:** saving a 4 s take writes 128 KB. The LEDs go dark while it saves.
- **LFO-driven speed/humanize:** only plain numbers and `"$handle"` are verified. A synthio LFO
  only updates while a native effect ticks it.
