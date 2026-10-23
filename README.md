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

| Control | Does |
|---|---|
| TOP (orange) | Next spot: clean → preset 1..4 → **record mode** → clean. Red LEDs show the preset. |
| MIDDLE (white) | Next sample slot 1..4. White LEDs show the slot. |
| BOTTOM (grey) | Play the selected sample (oneshot / hold / startstop, from the config), through the effect preset currently selected on the mic (`"sample_fx": "preset"`) or the sample's own effects. |
| Squeeze handle | Turns the mic on. Its travel is also `"$handle"` in presets (0 = just squeezed enough to open the mic, 1 = fully in), and an autotune preset can switch key on a full squeeze (`squeeze_key` / `squeeze_scale`). |
| Volume knob | Output level. |
| TOP + MIDDLE 0.8 s | Power off. |

**Record mode:** all four red LEDs light and the slot's white LED blinks. MIDDLE picks the slot,
squeeze and hold the handle to record, release to save. The red LEDs count the 4 seconds down.
A take replaces the slot's WAV. Grey plays it back.

## Two power-on modes (boot.py)

CircuitPython lets either the computer or the mic write files, never both at once.

- **Normal power-on (squeeze the handle):** the mic can record. Your computer sees the drive as read-only.
- **Hold TOP (orange) while squeezing the handle to power on (edit mode):** your computer can save `config.json`, WAVs and
  firmware updates. Recording is off, and the red LEDs flash in record mode.

`boot.py` only runs on power-on or a hard reset. If you ever get stuck, CircuitPython's safe mode
skips it.

## Install

1. Back up the ting and flash CircuitPython 10.x for `teenage_engineering_ep2350` (see the flash guide).
2. Copy everything in `firmware/` to the CIRCUITPY drive.
3. Copy `adafruit_nau88l21.mpy` from the matching Adafruit library bundle into `CIRCUITPY/lib`.
4. Power-cycle. To change presets later, power on holding TOP and save a new `config.json`.

## Website (docs/)

One static site, deployed by Cloudflare from `docs/` (see `wrangler.jsonc`):

- `index.html`: how it works, the button guide and record mode.
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

## License

MIT. Adafruit's original files keep their copyright headers. See `LICENSE` and `LICENSE-ADAFRUIT`.
