# fx-mic-mod

Custom CircuitPython firmware for the Teenage Engineering EP-2350 "Ting" / FX Mic: stackable effects
(autotune, a chord harmonizer, distortion, reverb, delay, chorus, phaser, pitch shift, filters) plus on-device
sample recording, with a friendly web editor for the presets.

It is a fork of Adafruit's [EP-2350 CircuitPython demo](https://github.com/adafruit/Adafruit_Learning_System_Guides/tree/main/Teenage_Engineering_EP2350_Demo)
by Tim Cocks (MIT), from the guide
[Hacking the Teenage Engineering EP-2350 Ting](https://learn.adafruit.com/hacking-the-teenage-engineering-ep-2350-ting).
Presets use Adafruit's `config.json` format, plus `autotune` and `harmonizer` effects, so Adafruit's packs load unchanged.

> **Status:** tested on desktop Python with stand-in hardware modules only. Not yet run on a real ting.
> See [firmware/NOTES.md](firmware/NOTES.md) for what still needs checking on the device.

## Controls

The EP-2350 is sold as the **FX Mic** and as the **Ting**. They run the same firmware but the buttons
are different colours, so this README names them by position:

| Button | FX Mic | Ting |
|---|---|---|
| TOP | orange | orange |
| MIDDLE | white | green |
| BOTTOM | grey | white |
| Handle | orange | green |

The website has an FX Mic / Ting switch at the top of every page that changes the colours and
instructions to match your mic.

| Control | Does |
|---|---|
| TOP | Next effects preset: clean → preset 1..4 → **record mode** → clean. The red LEDs count up as on the stock firmware: one lit for preset 1, two for preset 2, all four for preset 4, none on clean. In record mode all four flash. |
| MIDDLE | Next sample slot 1..4, counted on the white LEDs the same way (one lit for slot 1, all four for slot 4). |
| BOTTOM | Play the selected sample (oneshot / hold / startstop, from the config). In config, you can decide if you want samples to play through the currently selected effect preset, (`"sample_fx": "preset"`) or hardcode the sample's own effects. |
| Squeeze handle | Turns the mic on. Its travel is also `"$handle"` in presets (0 = just squeezed enough to open the mic, 1 = fully in). Effects parameters can be mapped to the squeeze, such as an autotune preset that switches key on a full squeeze (`squeeze_key` / `squeeze_scale`). |
| Volume knob | Output level. |
| TOP + MIDDLE 0.8 s | Power off. |

**Record mode:** all four red LEDs flash together and the white LEDs show the slot to record into. MIDDLE picks the slot,
squeeze and hold the handle to record, release to save. The red LEDs stop flashing and count down the 4 second max sample length as you record.
A recording immediately replaces the slot's WAV. BOTTOM plays it back to decide if you want another take.

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
3. Copy `adafruit_nau88l21.mpy` from the matching Adafruit library bundle into `CIRCUITPY/lib`.
4. Power-cycle. To change presets later, power on holding TOP and save a new `config.json`.

## Website (docs/)

One static site, deployed by Cloudflare from `docs/` (see `wrangler.jsonc`):

- `index.html`: how it works, the button guide and record mode.
- `site-device.js` / `site-device.css`: the FX Mic / Ting switch in the nav. It sets `<html data-device>`,
  swaps button colour words (`<span data-dw="mid">`), the site palette and the mic drawings.
  `?device=ting` or `?device=fxmic` in a link picks one.
- `install.html`: back up the mic (Ting or FX Mic), flash CircuitPython, then install fx-mic-mod in one click
  (it copies the files in `docs/firmware/` to CIRCUITPY), plus a serial console and restore steps.
  Adapted from this project's Ting Flash Bench.
- `editor.html`: the preset editor. Simple mode uses sliders and switches, Advanced mode shows every
  raw parameter. **Try it on your computer** plays the selected preset live through your computer's
  mic (or a sample singer) using `ting-dsp.js`, a Web Audio approximation of the firmware. Saves
  `config.json` straight to CIRCUITPY in Chrome and Edge, or downloads it elsewhere.

`docs/firmware/` must match `firmware/`. A test fails if they drift, so copy the files over after
changing the firmware.

## Presets

See [CONFIG_FORMAT.md](CONFIG_FORMAT.md). `firmware/config.json` is a starter pack (two autotune
presets and two from Adafruit's demo). `examples/adafruit-demo.json` is Adafruit's original pack.

## Tests

```
python3 -m unittest discover tests
```

Needs `numpy`. The tests run `firmware/code.py` against fake hardware (`tests/fakes.py`): an
autotune preset, two recorded takes, the LED countdown and edit mode.

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
