#!/bin/sh
# Install the Ultimate 2C receiver bridge on the rooted TV. Run from this directory on the workstation.
# Usage: sh install.sh [ssh-host]   (default host alias: lgtv)
set -e
HOST="${1:-lgtv}"
cd "$(dirname "$0")"
scp -q ultimate2c-bridge.py 48-ultimate2c-bridge "$HOST:/tmp/"
ssh "$HOST" '
set -e
mkdir -p /media/developer/ultimate2c
cp /tmp/ultimate2c-bridge.py /media/developer/ultimate2c/ultimate2c-bridge.py
chmod 755 /media/developer/ultimate2c/ultimate2c-bridge.py
touch /media/developer/ultimate2c/enabled
cp /tmp/48-ultimate2c-bridge /var/lib/webosbrew/init.d/48-ultimate2c-bridge
chmod 755 /var/lib/webosbrew/init.d/48-ultimate2c-bridge
rm -f /tmp/ultimate2c-bridge.py /tmp/48-ultimate2c-bridge
for p in $(pgrep -f "ultimate2c-bridge.py"); do [ "$p" != "$$" ] && kill "$p" 2>/dev/null || true; done
sleep 1
sh /var/lib/webosbrew/init.d/48-ultimate2c-bridge
sleep 3
echo "bridge log:"; cat /tmp/ultimate2c-bridge.log
'
