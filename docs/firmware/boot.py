# SPDX-FileCopyrightText: Copyright (c) 2026 fx-mic-mod contributors

# SPDX-License-Identifier: MIT
0
















import board
import digitalio
import storage
import usb_hid

usb_hid.disable()

top = digitalio.DigitalInOut(board.BUTTON_TOP)
top.switch_to_input(pull=digitalio.Pull.UP)
edit_mode = not top.value
top.deinit()

if not edit_mode:

    storage.remount("/", readonly=False)
