# webos-ultimate2c-bridge

Makes the **8BitDo Ultimate 2C** work over its **2.4 GHz USB receiver** on a rooted LG webOS TV.

Tested on an LG 55UR7500PSC, webOS 10.3.2, firmware 33.31.75, kernel 5.4.268, rooted with the Homebrew Channel. Status: the bridge runs and the TV accepts the virtual pad; the final button-press check is still pending (see "Verifying").

## The problem

The receiver enumerates as `2dc8:310a` with an XInput interface (class `ff/5d/01`). The TV's built-in `xpad` driver binds to it, but the receiver **never sends input reports until the host sends the Xbox 360 "set LED" packet** (`01 03 02`) on its interrupt OUT endpoint. `xpad` only sends that packet through its LED class support, and LG's kernel is built without `CONFIG_JOYSTICK_XPAD_LEDS`, so the controller stays silent forever. Bluetooth works because it goes through a different path (LG's `lginput2`).

The TV side is fine: synthetic D-pad events written into the xpad node move the Home screen focus, and LG's input library (`libim.so`) accepts any node whose key bitmap includes `BTN_GAMEPAD`.

Observed on the receiver's XInput interface:

| Sent to EP 0x05 | Reports on EP 0x84 |
|---|---|
| nothing | none |
| rumble `00 08 00 LL RR 00 00 00` | none |
| set-LED `01 03 02` | stream of 20-byte XInput reports |
| rumble while streaming | stream stops |

## The fix

`ultimate2c-bridge.py` is a small root daemon that replaces `xpad` for this one device:

1. Waits for `2dc8:310a` to appear in `/sys/bus/usb/devices`.
2. Creates a uinput pad named "Generic X-Box pad" (bus USB, `2dc8:310a`) with the same buttons, axes and ranges `xpad` would create.
3. Detaches `xpad` from interface 0 via usbfs, claims it, sends the XInput capability requests and the LED packet.
4. Parses the XInput reports with the `xpad` mapping and writes only changed values to uinput.
5. Repeats the handshake every 10 s until the first report, then only after 60 s of silence. When the receiver disappears or goes to its "8BitDo IDLE" state it hands the interface back to `xpad` and waits again.

No force feedback is advertised, because a rumble packet silences the receiver. CPU use is negligible. Log: `/tmp/ultimate2c-bridge.log`.

## Requirements

- Rooted webOS TV with the Homebrew Channel (startup hooks in `/var/lib/webosbrew/init.d/`).
- Root SSH access configured as an SSH alias (default `lgtv`).
- `python3` on the TV (present on recent webOS firmware).

## Install

```sh
sh install.sh            # or: sh install.sh <ssh-host>
```

This copies the daemon to `/media/developer/ultimate2c/`, creates the `enabled` marker, installs the boot hook `48-ultimate2c-bridge`, and starts the bridge immediately.

## Verifying

1. Turn the controller on in 2.4 GHz mode with the receiver plugged in; press any button if it was asleep.
2. Press the D-pad on the Home screen. Focus should move.
3. `ssh lgtv cat /tmp/ultimate2c-bridge.log` should show `input reports flowing`.

If nothing arrives, unplug and replug the receiver with the controller already on. Moonlight and other apps see the same uinput node as a normal Xbox 360 pad.

## Uninstall

```sh
sh uninstall.sh          # or: sh uninstall.sh <ssh-host>
```

Removes the marker, hook and daemon; the kernel `xpad` driver rebinds automatically. To disable without removing, delete `/media/developer/ultimate2c/enabled` and reboot.

## Files

| File | Purpose |
|---|---|
| `ultimate2c-bridge.py` | The daemon |
| `48-ultimate2c-bridge` | Homebrew Channel startup hook |
| `install.sh` / `uninstall.sh` | Run from a workstation with SSH access to the TV |

## Notes

- Other 8BitDo receivers may behave the same way; change `VID`/`PID` at the top of the daemon to try one.
- Everything runs in userspace; no kernel, udev or firmware files on the TV are modified.
