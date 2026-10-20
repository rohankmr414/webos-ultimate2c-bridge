#!/usr/bin/python3
"""8BitDo Ultimate 2C 2.4 GHz receiver bridge for LG webOS (rooted).

Why this exists: the receiver (USB 2dc8:310a, XInput class ff/5d/01) stays silent
until the host sends the Xbox 360 "set LED" packet on its interrupt OUT endpoint.
The TV kernel's built-in xpad driver has no LED support, so it never sends that
packet and no input reports ever arrive. This daemon takes the XInput interface
over through usbfs, performs the LED handshake, reads the 20-byte XInput reports
and republishes them through uinput with the same capabilities and names the xpad
driver would have produced, so webOS (libim) and apps treat it as a normal pad.

A rumble packet (00 08 ...) silences the receiver again, so no rumble is sent and
the uinput device does not advertise force feedback.

Run as root. Stops cleanly on SIGTERM/SIGINT and hands the interface back to xpad.
"""
import os, sys, time, struct, ctypes, fcntl, errno, signal, glob

VID, PID = 0x2dc8, 0x310a
EP_IN, EP_OUT = 0x84, 0x05
LED_PACKET = b"\x01\x03\x02"          # Xbox 360 "set LED" (player 1 blink/solid)
HANDSHAKE_RETRY_S = 10.0               # until the first report: repeat the handshake this often
SILENCE_RETRY_S = 60.0                 # after reports have been seen: repeat only after this much silence
LOG = "/tmp/ultimate2c-bridge.log"
UINPUT_NAME = "Generic X-Box pad"      # same name xpad gives this receiver

P = struct.calcsize("P")
def _IOC(d, t, nr, size): return (d << 30) | (size << 16) | (ord(t) << 8) | nr
def _align(n, a): return (n + a - 1) // a * a
SZ_CTRL = _align(12, P) + P; SZ_BULK = _align(12, P) + P; SZ_IOCTL = 8 + P
USBDEVFS_CONTROL = _IOC(3, "U", 0, SZ_CTRL); USBDEVFS_BULK = _IOC(3, "U", 2, SZ_BULK)
USBDEVFS_CLAIM = _IOC(2, "U", 15, 4); USBDEVFS_RELEASE = _IOC(2, "U", 16, 4)
USBDEVFS_IOCTL = _IOC(3, "U", 18, SZ_IOCTL)
USBDEVFS_DISCONNECT = _IOC(0, "U", 22, 0); USBDEVFS_CONNECT = _IOC(0, "U", 23, 0)
UI_DEV_CREATE = _IOC(0, "U", 1, 0); UI_DEV_DESTROY = _IOC(0, "U", 2, 0)
UI_DEV_SETUP = _IOC(1, "U", 3, 92); UI_ABS_SETUP = _IOC(1, "U", 4, 28)
UI_SET_EVBIT = _IOC(1, "U", 100, 4); UI_SET_KEYBIT = _IOC(1, "U", 101, 4); UI_SET_ABSBIT = _IOC(1, "U", 103, 4)
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y = 0, 1, 2, 3, 4, 5, 0x10, 0x11
BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR = 0x130, 0x131, 0x133, 0x134, 0x136, 0x137
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13a, 0x13b, 0x13c, 0x13d, 0x13e
BUTTONS = [BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR]
# (abs code, min, max, fuzz, flat) exactly as xpad sets them for an Xbox 360 type pad
AXES = [(ABS_X, -32768, 32767, 16, 128), (ABS_Y, -32768, 32767, 16, 128), (ABS_RX, -32768, 32767, 16, 128),
        (ABS_RY, -32768, 32767, 16, 128), (ABS_Z, 0, 255, 0, 0), (ABS_RZ, 0, 255, 0, 0),
        (ABS_HAT0X, -1, 1, 0, 0), (ABS_HAT0Y, -1, 1, 0, 0)]
EVENT_FMT = "llHHi"

running = True
def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    sys.stdout.write(line); sys.stdout.flush()
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000: os.replace(LOG, LOG + ".old")
        with open(LOG, "a") as f: f.write(line)
    except OSError: pass

def find_receiver():
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if int(open(d + "/idVendor").read(), 16) != VID or int(open(d + "/idProduct").read(), 16) != PID: continue
            intf = d + "/" + os.path.basename(d) + ":1.0"
            if open(intf + "/bInterfaceClass").read().strip() != "ff" or open(intf + "/bInterfaceSubClass").read().strip() != "5d": continue
            return d, int(open(d + "/busnum").read()), int(open(d + "/devnum").read())
        except (OSError, ValueError): continue
    return None

class Uinput:
    def __init__(self, vendor, product, version):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_KEY, EV_ABS): fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for b in BUTTONS: fcntl.ioctl(self.fd, UI_SET_KEYBIT, b)
        for code, mn, mx, fuzz, flat in AXES:
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("HHiiiiii", code, 0, 0, mn, mx, fuzz, flat, 0))
        setup = struct.pack("HHHH", 0x03, vendor, product, version) + UINPUT_NAME.encode().ljust(80, b"\0") + struct.pack("I", 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup); fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.state = {}
    def emit(self, changes):
        if not changes: return
        now = time.time(); sec, usec = int(now), int((now % 1) * 1e6)
        buf = b"".join(struct.pack(EVENT_FMT, sec, usec, t, c, v) for t, c, v in changes)
        buf += struct.pack(EVENT_FMT, sec, usec, EV_SYN, 0, 0)
        try: os.write(self.fd, buf)
        except OSError as e: log("uinput write error: %s" % e)
    def close(self):
        try: fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError: pass
        os.close(self.fd)

def parse_report(r):
    """XInput 20-byte report -> dict of (type, code) -> value, mirroring xpad's Xbox 360 mapping."""
    if len(r) < 14 or r[0] != 0x00 or r[1] != 0x14: return None
    b2, b3 = r[2], r[3]
    lx, ly, rx, ry = struct.unpack_from("<hhhh", r, 6)
    s = {(EV_ABS, ABS_HAT0X): (1 if b2 & 0x08 else 0) - (1 if b2 & 0x04 else 0),
         (EV_ABS, ABS_HAT0Y): (1 if b2 & 0x02 else 0) - (1 if b2 & 0x01 else 0),
         (EV_KEY, BTN_START): (b2 >> 4) & 1, (EV_KEY, BTN_SELECT): (b2 >> 5) & 1,
         (EV_KEY, BTN_THUMBL): (b2 >> 6) & 1, (EV_KEY, BTN_THUMBR): (b2 >> 7) & 1,
         (EV_KEY, BTN_TL): b3 & 1, (EV_KEY, BTN_TR): (b3 >> 1) & 1, (EV_KEY, BTN_MODE): (b3 >> 2) & 1,
         (EV_KEY, BTN_A): (b3 >> 4) & 1, (EV_KEY, BTN_B): (b3 >> 5) & 1, (EV_KEY, BTN_X): (b3 >> 6) & 1, (EV_KEY, BTN_Y): (b3 >> 7) & 1,
         (EV_ABS, ABS_Z): r[4], (EV_ABS, ABS_RZ): r[5],
         (EV_ABS, ABS_X): lx, (EV_ABS, ABS_Y): ~ly, (EV_ABS, ABS_RX): rx, (EV_ABS, ABS_RY): ~ry}
    return s

class Receiver:
    def __init__(self, bus, dev):
        self.fd = os.open("/dev/bus/usb/%03d/%03d" % (bus, dev), os.O_RDWR)
        self.claimed = False
    def driver_ioctl(self, code):
        s = struct.pack("ii", 0, code).ljust(SZ_IOCTL, b"\0")
        try: fcntl.ioctl(self.fd, USBDEVFS_IOCTL, bytearray(s)); return True
        except OSError as e:
            if e.errno != errno.ENODATA: log("driver ioctl %#x: %s" % (code, e))
            return False
    def claim(self):
        self.driver_ioctl(USBDEVFS_DISCONNECT)       # detach xpad from interface 0 (ENODATA if none bound)
        fcntl.ioctl(self.fd, USBDEVFS_CLAIM, struct.pack("I", 0)); self.claimed = True
    def xfer(self, ep, data=None, length=32, timeout=500):
        buf = ctypes.create_string_buffer(data, len(data)) if data else ctypes.create_string_buffer(length)
        s = struct.pack("III", ep, len(buf), timeout).ljust(_align(12, P), b"\0") + struct.pack("P", ctypes.addressof(buf))
        n = fcntl.ioctl(self.fd, USBDEVFS_BULK, bytearray(s)); return buf.raw[:n]
    def control(self, rtype, req, value, index, length, timeout=1000):
        buf = ctypes.create_string_buffer(length)
        s = struct.pack("BBHHHI", rtype, req, value, index, length, timeout).ljust(_align(12, P), b"\0") + struct.pack("P", ctypes.addressof(buf))
        n = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, bytearray(s)); return buf.raw[:n]
    def handshake(self):
        """What an XInput host does: vendor capability requests, then the set-LED packet. The receiver
        only starts streaming input reports after this."""
        for value, length in ((0x0100, 20), (0x0000, 4)):
            try: self.control(0xC1, 0x01, value, 0x0000, length)
            except OSError as e: log("capability request %#06x failed: %s" % (value, e))
        try: self.xfer(EP_OUT, LED_PACKET, timeout=500); return True
        except OSError as e: log("LED packet failed: %s" % e); return False
    def close(self):
        if self.claimed:
            try: fcntl.ioctl(self.fd, USBDEVFS_RELEASE, struct.pack("I", 0))
            except OSError: pass
            self.driver_ioctl(USBDEVFS_CONNECT)       # give the interface back to xpad
        os.close(self.fd)

def serve(syspath, bus, dev):
    version = int(open(syspath + "/bcdDevice").read(), 16)
    rx = Receiver(bus, dev); ui = None
    try:
        ui = Uinput(VID, PID, version); log("uinput device created: %r" % UINPUT_NAME)
        time.sleep(0.5)                                   # let the compositor pick up the new node first
        rx.claim(); log("claimed %s (bus %d dev %d); xpad detached" % (os.path.basename(syspath), bus, dev))
        rx.handshake(); last_led = time.time(); last_report = 0.0; reports = 0; events = 0; tick = time.time()
        while running:
            try: r = rx.xfer(EP_IN, length=32, timeout=500)
            except OSError as e:
                if e.errno in (errno.ETIMEDOUT, errno.EAGAIN):
                    wait = HANDSHAKE_RETRY_S if last_report == 0.0 else SILENCE_RETRY_S
                    if time.time() - max(last_report, last_led) >= wait:
                        rx.handshake(); last_led = time.time(); log("no reports for %ds; handshake repeated" % wait)
                    continue
                if e.errno in (errno.ENODEV, errno.ENOENT, errno.EIO, errno.EPIPE, errno.ESHUTDOWN): log("receiver gone (%s)" % e); break
                log("read error: %s" % e); time.sleep(0.2); continue
            state = parse_report(r)
            if state is None: continue
            if last_report == 0.0: log("input reports flowing (first report %s)" % r.hex())
            last_report = time.time(); reports += 1
            changes = [(t, c, v) for (t, c), v in state.items() if ui.state.get((t, c)) != v]
            ui.state.update(state); ui.emit(changes); events += len(changes)
            if time.time() - tick >= 300: log("stats: %d reports, %d events in last 5 min" % (reports, events)); reports = events = 0; tick = time.time()
    finally:
        if ui: ui.close()
        rx.close(); log("released interface; uinput destroyed")

def main():
    def stop(signum, frame):
        global running; running = False
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    log("bridge starting (pid %d), waiting for %04x:%04x" % (os.getpid(), VID, PID))
    while running:
        found = find_receiver()
        if not found: time.sleep(1.0); continue
        syspath, bus, dev = found
        try: serve(syspath, bus, dev)
        except OSError as e: log("serve failed: %s" % e)
        # wait until this device instance disappears (re-enumeration gives a new devnum)
        while running and find_receiver() == found: time.sleep(1.0)
    log("bridge stopped")

if __name__ == "__main__": main()
