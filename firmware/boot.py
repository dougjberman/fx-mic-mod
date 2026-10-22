# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors
#
# SPDX-License-Identifier: MIT
"""
Pick who may write the CIRCUITPY drive, once per power-on.

CircuitPython lets either the computer or the code write the drive, never both.

* Default, **record mode**: the code can write, so the mic can record samples.
  A computer sees the drive read-only.
* Hold TOP (orange) while powering on, **edit mode**: the computer can write,
  for saving config.json from the GUI, copying WAVs or updating the firmware.
  Recording is off.

boot.py runs only on a hard reset or power-on, not on a code.py reload.
"""

import board
import digitalio
import storage

top = digitalio.DigitalInOut(board.BUTTON_TOP)
top.switch_to_input(pull=digitalio.Pull.UP)
edit_mode = not top.value  # pressed reads low
top.deinit()

if not edit_mode:
    # readonly=False is from CircuitPython's side: code can write, USB cannot.
    storage.remount("/", readonly=False)
