# webos-ultimate2c-bridge

Makes the **8BitDo Ultimate 2C** work over its **2.4 GHz USB receiver** on a rooted LG webOS TV, with up to four controllers at once and optional rumble.

Without it, the TV detects the receiver but the controller never sends any input. The bridge is a small Python daemon that runs as root in userspace. It needs no kernel modules, and it doesn't change any system files.

## Tested on

| TV | webOS | Kernel | Result |
|---|---|---|---|
| LG OLED55B49LA | 10.3.1 | 5.4.268-294.24.papikonda.2 (aarch64) | Working, including several controllers at once, Moonlight, and rumble on two controllers |
| LG 55UR7500PSC | 10.3.2 (firmware 33.31.75) | 5.4.268 | Working with one controller: D-pad navigation on the Home screen |

Both TVs are rooted with the Homebrew Channel. Not checked yet: behaviour after sleep and wake. If you test another model, a PR adding a row is welcome.

## Install

**You need:**
- A rooted webOS TV with the [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel), which runs startup hooks from `/var/lib/webosbrew/init.d/`.
- Root SSH access to the TV from your computer, set up as an SSH host alias (default `lgtv`).
- `python3` on the TV. Recent webOS firmware includes it.

Run this from a clone of this repo:

```sh
sh install.sh            # or: sh install.sh <ssh-host>
```

This copies the daemon to `/media/developer/ultimate2c/`, creates the `enabled` marker, installs the boot hook `48-ultimate2c-bridge`, and starts the bridge. You don't need to reboot, and the bridge starts automatically on every boot after this. Running `install.sh` again updates the bridge in place.

### Check that it works

1. Plug in the receiver and turn the controller on in 2.4 GHz mode. If it was asleep, press any button.
2. Press the D-pad on the Home screen. The focus should move.
3. Run `ssh lgtv cat /tmp/ultimate2c-bridge.log`. You should see lines like:
   ```
   claimed 1-1 (bus 1 dev 3) as player 1; xpad detached
   input reports flowing (first report 0014...)
   ```

To apps such as games and Moonlight, each controller looks like a normal Xbox 360 pad named "Generic X-Box pad".

## Multiple controllers

Each receiver pairs with exactly one controller, so use one receiver per controller.

- Up to **four** receivers work at once. Each gets its own pad and a player number, 1 to 4.
- A new receiver takes the lowest free player number. For example, if you unplug player 1 and plug in another receiver, the new one becomes player 1.
- Unplugging one receiver doesn't affect the others.
- A fifth receiver waits until a slot frees up. The log says `waiting: all 4 player slots in use`.

You can plug receivers in and out at any time while the TV is on. A receiver that disconnects briefly, for example when the controller sleeps or goes out of range, gets its old pad and player number back if it returns within 15 seconds. Meanwhile the pad stays connected with all inputs released, so apps don't see the controller vanish. The bridge recognises a receiver by its USB serial number. If a receiver has no serial number, the bridge uses the USB port instead.

## Optional rumble

Rumble is off by default. To turn it on, create the marker file and restart the bridge:

```sh
ssh lgtv 'touch /media/developer/ultimate2c/rumble-enabled' && sh install.sh
```

The pads then advertise standard two-motor force feedback. Reopen Moonlight afterwards so it picks up the new controller capabilities. To turn rumble off again, delete the marker and restart the bridge.

The bridge sends motor commands from a separate thread, so input keeps flowing while the motors run. It stops the motors if input reports stop arriving, or when the receiver disconnects. The receiver only sends a report when something on the controller changes, so rumble stops after 0.3 s if nobody is touching the controller or a stick is held still.

## Troubleshooting

| Symptom | What to try |
|---|---|
| The log keeps showing `no reports for 10s; handshake repeated` | The controller is off, asleep, or not paired with that receiver. Turn it on and press a button. If that doesn't help, unplug and replug the receiver with the controller already on. |
| `no reports for 60s; handshake repeated` | This is normal: the controller hasn't been touched for a minute, and the bridge sends the handshake again just in case. |
| Nothing in the log at all | Check that the bridge is running with `ssh lgtv pgrep -f ultimate2c-bridge.py`. Also check that `/media/developer/ultimate2c/enabled` exists. |
| No rumble in Moonlight | Check that `/media/developer/ultimate2c/rumble-enabled` exists and the log says `force feedback enabled`, then reopen Moonlight. |
| The controller works over Bluetooth but not 2.4 GHz | This is the problem the bridge fixes, so make sure it's installed. Bluetooth controllers go through LG's own input stack and work without the bridge. |

The log is `/tmp/ultimate2c-bridge.log`. Once it reaches about 200 KB, the bridge renames it to `.old` and starts a new one.

## Uninstall or disable

```sh
sh uninstall.sh          # or: sh uninstall.sh <ssh-host>
```

This stops the bridge and removes the daemon, the marker and the boot hook. The TV's own `xpad` driver takes the receiver back automatically.

To turn the bridge off without removing it, delete `/media/developer/ultimate2c/enabled` and reboot.

## How it works

### The problem

The receiver shows up as USB device `2dc8:310a` with an Xbox 360-style (XInput) interface, class `ff/5d/01`. The TV's built-in `xpad` driver binds to it. However, the receiver **sends no input until the host sends it the Xbox 360 "set LED" packet** (`01 03 02`). `xpad` only sends that packet through its LED support, and LG builds its kernel without `CONFIG_JOYSTICK_XPAD_LEDS`. So the controller stays silent forever.

These are the results of sending packets to the receiver's XInput interface:

| Sent to endpoint 0x05 | Reports on endpoint 0x84 |
|---|---|
| nothing | none |
| rumble `00 08 00 LL RR 00 00 00` | none |
| set-LED `01 03 02` | a stream of 20-byte XInput reports |
| rumble, with USB reads and writes done one at a time | reports can pause until the motors stop |
| rumble, with input reads running at the same time | reports kept flowing during a test pulse |

The rest of the TV is fine. Fake D-pad events written into the `xpad` input device do move the Home screen focus. LG's input library (`libim.so`) accepts any input device that reports `BTN_GAMEPAD`.

### The fix

`ultimate2c-bridge.py` checks `/sys/bus/usb/devices` for receivers every second. It keeps one session for each player slot, 1 to 4. For each new receiver, it picks a slot and starts a thread that:

1. Creates a virtual pad through uinput, or reuses the slot's pad if it belongs to the same receiver. The pad has the name ("Generic X-Box pad"), USB IDs, buttons, axes and ranges that `xpad` would have created, so webOS and apps treat it as a normal pad.
2. Detaches `xpad` from the receiver's interface 0 through usbfs and claims the interface.
3. Sends the XInput capability requests, then the set-LED packet for its player number (`01 03 02` to `01 03 05`).
4. Reads the XInput reports, converts them with the same button and axis mapping `xpad` uses, and writes only the values that changed to the virtual pad.
5. Repeats the handshake every 10 s until the first report arrives. After that, it repeats it only after 60 s without reports.
6. Stops when the receiver disappears, goes to its "8BitDo IDLE" state, or the daemon is stopped. It then releases all inputs, gives the interface back to `xpad`, and keeps the pad for 15 seconds in case the receiver returns.

When rumble is on, each pad also has a worker thread. It accepts force-feedback effects from apps through uinput and forwards them to the receiver as motor commands. CPU use is negligible.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests use only the Python standard library. They fake usbfs, uinput, sysfs and the clock, so they run on any Linux machine without a receiver.

You can also run the bridge on a Linux PC with real receivers: `sudo python3 ultimate2c-bridge.py`. A desktop `xpad` driver sends its own LED packet when it binds to the receiver, though. To test the bridge's handshake by itself, stop `xpad` from loading first, then plug the receivers in:

```sh
echo "blacklist xpad" | sudo tee /run/modprobe.d/no-xpad.conf && sudo modprobe -r xpad
```

To undo it, run `sudo rm /run/modprobe.d/no-xpad.conf`. A reboot also clears it.

| File | Purpose |
|---|---|
| `ultimate2c-bridge.py` | The daemon |
| `48-ultimate2c-bridge` | Homebrew Channel startup hook |
| `install.sh` / `uninstall.sh` | Install and remove from your computer over SSH |
| `tests/test_bridge.py` | Unit tests: report parsing, usbfs and uinput handling, the receiver loop, pad reuse, rumble |
| `tests/test_multi_receiver.py` | Unit tests: several receivers, player slots, hot-plugging, reconnects |

## Notes

- Other 8BitDo receivers may have the same problem. To try one, change `VID`/`PID` at the top of the daemon.
- The bridge doesn't modify any kernel, udev or firmware files on the TV.
