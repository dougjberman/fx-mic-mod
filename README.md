# fx-mic-mod

Custom CircuitPython firmware for the Teenage Engineering EP-2350 "Ting" / FX Mic: stackable effects
(autotune, distortion, reverb, delay, chorus, phaser, pitch shift, filters) plus on-device
sample recording, with a friendly web editor for the presets.

It is a fork of Adafruit's [EP-2350 CircuitPython demo](https://github.com/adafruit/Adafruit_Learning_System_Guides/tree/main/Teenage_Engineering_EP2350_Demo)
by Tim Cocks (MIT), from the guide
[Hacking the Teenage Engineering EP-2350 Ting](https://learn.adafruit.com/hacking-the-teenage-engineering-ep-2350-ting).
Presets use Adafruit's `config.json` format, plus an `autotune` effect, so Adafruit's packs load unchanged.

> **Status:** tested on desktop Python with stand-in hardware modules only. Not yet run on a real ting.
> See [firmware/NOTES.md](firmware/NOTES.md) for what still needs checking on the device.

## Controls

| Control | Does |
|---|---|
| TOP (orange) | Next spot: clean → preset 1..4 → **record mode** → clean. Red LEDs show the preset. |
| MIDDLE (white) | Next sample slot 1..4. White LEDs show the slot. |
| BOTTOM (grey) | Play the selected sample (oneshot / hold / startstop, from the config). |
| Squeeze handle | Turns the mic on. Its travel is also `"$handle"` in presets (0 = rest, 1 = fully in). |
| Volume knob | Output level. |
| TOP + MIDDLE 0.8 s | Power off. |

**Record mode:** all four red LEDs light and the slot's white LED blinks. MIDDLE picks the slot,
squeeze and hold the handle to record, release to save. The red LEDs count the 4 seconds down.
A take replaces the slot's WAV. Grey plays it back.

## Two power-on modes (boot.py)

CircuitPython lets either the computer or the mic write files, never both at once.

- **Normal power-on:** the mic can record. Your computer sees the drive as read-only.
- **Hold TOP while powering on (edit mode):** your computer can save `config.json`, WAVs and
  firmware updates. Recording is off, and the red LEDs flash in record mode.

`boot.py` only runs on power-on or a hard reset. If you ever get stuck, CircuitPython's safe mode
skips it.

## Install

1. Back up the ting and flash CircuitPython 10.x for `teenage_engineering_ep2350` (see the flash guide).
2. Copy everything in `firmware/` to the CIRCUITPY drive.
3. Copy `adafruit_nau88l21.mpy` from the matching Adafruit library bundle into `CIRCUITPY/lib`.
4. Power-cycle. To change presets later, power on holding TOP and save a new `config.json`.

## Preset editor

`docs/index.html` is a web editor for `config.json`: preset slots like the red LEDs, autotune
with plain-language sliders, an effect chain you add to and reorder, sample slots, and an
Advanced mode with every raw parameter. It opens and saves `config.json` (Chrome and Edge can
save straight to the CIRCUITPY drive; other browsers download the file). It is one self-contained
page, served by GitHub Pages from the `docs/` folder.

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
