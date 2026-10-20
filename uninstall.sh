#!/bin/sh
# Stop and remove the Ultimate 2C receiver bridge from the TV. The kernel xpad driver takes the receiver back.
# Usage: sh uninstall.sh [ssh-host]
HOST="${1:-lgtv}"
ssh "$HOST" '
rm -f /media/developer/ultimate2c/enabled /var/lib/webosbrew/init.d/48-ultimate2c-bridge
for p in $(pgrep -f "ultimate2c-bridge.py"); do [ "$p" != "$$" ] && kill "$p" 2>/dev/null; done
sleep 2
rm -rf /media/developer/ultimate2c
echo "removed; interface driver now: $(readlink /sys/bus/usb/devices/1-1/1-1:1.0/driver 2>/dev/null | sed "s#.*/##")"
'
