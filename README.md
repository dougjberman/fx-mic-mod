# fx-mic-mod

Custom CircuitPython firmware for the Teenage Engineering EP-2350 "Ting" / FX Mic: stackable effects
(autotune, a chord harmonizer, a pitch-tracking vocoder, distortion, reverb, delay, chorus, phaser, pitch shift, filters) plus on-device
sample recording, with a friendly web editor for the presets. Or swap the samples for more effects:
the optional **effects-only mode** holds up to 16 presets on the mic, in four pages of four.

It is a fork of Adafruit's [EP-2350 CircuitPython demo](https://github.com/adafruit/Adafruit_Learning_System_Guides/tree/main/Teenage_Engineering_EP2350_Demo)
by Tim Cocks (MIT), from the guide
[Hacking the Teenage Engineering EP-2350 Ting](https://learn.adafruit.com/hacking-the-teenage-engineering-ep-2350-ting).
Presets use Adafruit's `config.json` format, plus `autotune`, `harmonizer` and `vocoder` effects, so Adafruit's packs load unchanged.

> **Status:** firmware 1.2.0, running day to day on a real Ting. 1.1.0 added auto-off on batteries,
> and 1.2.0 added double tap to go back on TOP and MIDDLE (the install page's "What's new" lists
> every release). The desktop tests use stand-in
> hardware modules, and [`tools/ting_test.py`](#testing-on-a-real-mic) checks a real mic over USB.
> See [firmware/NOTES.md](firmware/NOTES.md) for what has been checked on the device, with tuning notes.

## Controls

The EP-2350 is sold as the **FX Mic** and as the **Ting**. They run the same firmware but the buttons
are different colors, so this README names them by position:

| Button | FX Mic | Ting |
|---|---|---|
| TOP | orange | orange |
| MIDDLE | white | green |
| BOTTOM | gray | white |
| Handle | orange | green |

The website has an FX Mic / Ting switch at the top of every page that changes the colors and
instructions to match your mic.

| Control | Does |
|---|---|
| TOP | Next effects preset: clean → preset 1..4 → **record mode** → clean. The red LEDs count up as on the stock firmware: one lit for preset 1, two for preset 2, all four for preset 4, none on clean. In record mode all four flash. It steps when you let go, and a quick double tap goes back one instead. |
| MIDDLE | Next sample slot 1..4, counted on the white LEDs the same way (one lit for slot 1, all four for slot 4). Like TOP, it steps when you let go, and a quick double tap goes back one slot. |
| BOTTOM | Play the selected sample: a tap plays it through, a hold plays it until you let go, and a double tap plays it backwards. In config, you can decide if you want samples to play through the currently selected effect preset, (`"sample_fx": "preset"`) or hardcode the sample's own effects. |
| Squeeze handle | Turns the mic on. Its travel is also `"$handle"` in presets (0 = just squeezed enough to open the mic, 1 = fully in). Effects parameters can be mapped to the squeeze, such as an autotune preset that switches key on a full squeeze (`squeeze_key` / `squeeze_scale`). |
| Shake | While a sample plays, shaking the mic zips it up (the default), warbles it or glitches it: `"shake": "zip"`, `"warble"`, `"glitch"` or `"off"`. Played through an autotune preset, the sample stutters instead. |
| Volume knob | Output level. |
| TOP + MIDDLE 0.8 s | Power off. Because TOP and MIDDLE step on release, this doesn't change the preset or sample. |
| (nothing for 10 min) | On batteries the mic turns itself off, with every light blinking for the last 10 seconds (press anything or squeeze to keep it on). `"auto_off_minutes"` sets the time, 0 for never; it stays on over USB to a computer. |

**Record mode:** all four red LEDs flash together and the white LEDs show the slot to record into. MIDDLE picks the slot,
squeeze and hold the handle to record, release to save. The red LEDs stop flashing and count down the 4 second max sample length as you record. While the take saves, a light runs down the mic from the top red LED to the bottom white one. Silence before and after the sound is trimmed (the editor can turn that off), so the sample starts right on it. The editor can also normalize takes, so each one peaks at -3 dB (off by default).
A recording immediately replaces the slot's WAV. BOTTOM plays it back to decide if you want another take.

## Effects-only mode: 16 presets

Can't pick just four? Turn on **Give me more effects instead of samples** in the editor's Samples
section (`"effects_only": true` in `config.json`). The mic drops the samples
and record mode and holds up to **16 effect presets, in four pages of four**. The editor shows them
as four rows, one per page, and every preset has a copy button so you can paste it into another
slot and make a variation.

| Control | Does |
|---|---|
| TOP | Next preset on the current page, 1..4 and round again. The red LEDs count the preset as usual. A quick double tap goes back one. |
| MIDDLE | Next page, 1..4 and back to 1, landing on the page's first preset. The white LEDs count the page: two lit is page 2. A quick double tap goes back a page. |
| BOTTOM | Clean (no effects), with every LED off. It's the only way to clean; from there, TOP or MIDDLE goes to page 1, preset 1. |

Pages fill in order, so page 2 starts at the fifth preset. Presses made while a preset is still
loading all count, and the mic loads only the preset they end up on. Your WAV files stay on the mic and your
sample settings stay in `config.json`, so turning the mode off brings the samples back. Presets are
small: the mic keeps all 16 as JSON and only builds the effects of the one playing.

## Two power-on modes (boot.py)

CircuitPython lets either the computer or the mic write files, never both at once.

- **Normal power-on (squeeze the handle):** the mic can record. Your computer sees the drive as read-only.
- **Hold TOP (orange) while squeezing the handle to power on (edit mode):** your computer can save `config.json`, WAVs and
  firmware updates. Recording is off: in record mode the red LEDs flash in alternating pairs instead of all together.

`boot.py` only runs on power-on or a hard reset. If you ever get stuck, CircuitPython's safe mode
skips it.

## Install

1. Back up the mic and flash CircuitPython 10.x for `teenage_engineering_ep2350` (see the flash guide).
2. Copy everything in `firmware/` to the CIRCUITPY drive.
3. Copy `adafruit_nau88l21.mpy` and the `adafruit_register` folder from the matching Adafruit library bundle into `CIRCUITPY/lib`.
4. Power-cycle. To change presets later, power on holding TOP and save a new `config.json`.

## Website (docs/)

One static site, deployed by Cloudflare from `docs/` (see `wrangler.jsonc`):

- `index.html`: how it works, the button guide and record mode.
- `site-stats.js`: cookie-free visit and button counts through GoatCounter. Nothing loads until its
  `CODE` is set, and the part of a URL after `#` (shared presets) is never sent.
- `site-device.js` / `site-device.css`: the FX Mic / Ting switch in the nav. It sets `<html data-device>`,
  swaps button color words (`<span data-dw="mid">`), the site palette and the mic drawings.
  `?device=ting` or `?device=fxmic` in a link picks one.
- `install.html`: back up the mic (Ting or FX Mic), flash CircuitPython, then install fx-mic-mod in one click
  (it copies the files in `docs/firmware/` to CIRCUITPY), plus a serial console and restore steps.
  Its update check reads `FIRMWARE_VERSION` from the mic's `code.py`, lists what's new from the page's
  "What's new" table (one row per version; a test checks the current one is there) and copies only the
  firmware files, keeping `config.json`. Adapted from this project's Ting Flash Bench.
- `editor.html`: the Effects & Samples Editor. Simple mode uses sliders and switches, Advanced mode shows every
  raw parameter. The Power section sets **Turn off when inactive** (`auto_off_minutes`).
  **Try it on your computer** plays the selected preset live through your computer's
  mic (or a sample singer) using `ting-dsp.js`, a Web Audio approximation of the firmware. Saves
  `config.json` straight to CIRCUITPY in Chrome and Edge, or downloads it elsewhere.

`docs/firmware/` holds `firmware/` without comments or docstrings, line for line, so the mic's
small drive keeps room for recordings and tracebacks still match the source. A test fails if they
drift, so run `python3 tools/slim_firmware.py` after changing the firmware.

## Presets

See [CONFIG_FORMAT.md](CONFIG_FORMAT.md). `firmware/config.json` is a starter pack (two autotune
presets and two from Adafruit's demo). `examples/adafruit-demo.json` is Adafruit's original pack.

## Tests

```
python3 -m unittest discover tests
```

Needs `numpy`. The tests load the presets, check the effects' maths, and run `firmware/code.py`
against fake hardware (`tests/fakes.py`): autotune, the harmonizer and vocoder, recording takes,
the LED countdown, edit mode, sample taps, holds and backwards double taps, TOP / MIDDLE double
taps and presses made during a preset load. Others check that `docs/firmware/` matches
`firmware/` and that the install page lists the current version.

## Testing on a real mic

`tools/ting_test.py` runs the automatic part of the hardware test plan over USB serial. Power the
mic on normally (not edit mode), plug it in, then:

```
pip install pyserial numpy
python3 tools/ting_test.py check            # no hands needed, about 5 minutes
python3 tools/ting_test.py check --stress 50
python3 tools/ting_test.py guided           # LEDs, buttons, handle and knob, with prompts
python3 tools/ting_test.py install          # copy firmware/ to the mic
python3 tools/ting_test.py backup ~/Desktop # copy the mic's drive to a folder
```

`check` stops code.py, puts `selftest.py` and three test WAVs on the drive, and drives
`firmware/selftest.py` from the raw REPL. Audio checks are an acoustic loopback: a test WAV goes
through a preset in place of the mic, plays out of the speaker, and the mic records it. The
computer then measures the recordings: whether autotune lands within 15 cents, whether each
harmony voice is at its interval, whether pitch shifts move by the right amount, how much of
each 16 ms block autotune's Python needs, how much CPU the native effects leave, memory leaks
across rebuilds, and how loud each preset is. Run it somewhere quiet with headphones unplugged.
Results, recordings and the raw log go to `test-results/<date-time>/`.

## License

MIT. Adafruit's original files keep their copyright headers. See `LICENSE` and `LICENSE-ADAFRUIT`.
