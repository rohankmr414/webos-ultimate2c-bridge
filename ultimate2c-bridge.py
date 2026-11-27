#!/usr/bin/python3
"""8BitDo Ultimate 2C 2.4 GHz receiver bridge for LG webOS (rooted).

Why this exists: the receiver (USB 2dc8:310a, XInput class ff/5d/01) stays silent
until the host sends the Xbox 360 "set LED" packet on its interrupt OUT endpoint.
The TV kernel's built-in xpad driver has no LED support, so it never sends that
packet and no input reports ever arrive. This daemon takes the XInput interface
over through usbfs, performs the LED handshake, reads the 20-byte XInput reports
and republishes them through uinput with the same capabilities and names the xpad
driver would have produced, so webOS (libim) and apps treat it as a normal pad.

Rumble is opt-in. When enabled, a worker handles uinput feedback and sends motor
commands concurrently with the receiver's input reads.

Each receiver pairs with one controller. Up to MAX_PLAYERS receivers are served at once,
each in its own thread with its own uinput pad and player LED. A player slot keeps its pad
through the reconnect grace period, so a returning receiver gets its old slot back.

Run as root. Stops cleanly on SIGTERM/SIGINT and hands the interface back to xpad.
"""
import os, sys, time, struct, ctypes, fcntl, errno, signal, glob
import select
import threading

VID, PID = 0x2dc8, 0x310a
EP_IN, EP_OUT = 0x84, 0x05
LED_PACKET = b"\x01\x03\x02"          # Xbox 360 "set LED" (player 1 blink/solid)
MAX_PLAYERS = 4                        # the Xbox 360 LED protocol has four player slots
HANDSHAKE_RETRY_S = 10.0               # until the first report: repeat the handshake this often
SILENCE_RETRY_S = 60.0                 # after reports have been seen: repeat only after this much silence
LOG = "/tmp/ultimate2c-bridge.log"
UINPUT_NAME = "Generic X-Box pad"      # same name xpad gives this receiver
RUMBLE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rumble-enabled")
RUMBLE_INPUT_TIMEOUT_S = 0.3
RECONNECT_GRACE_S = 15.0

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
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_FF, EV_UINPUT = 0x15, 0x0101
FF_RUMBLE, FF_EFFECTS_MAX = 0x50, 8
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
UI_SET_FFBIT = _IOC(1, "U", 107, 4)
# The largest ff_effect union member contains a native pointer. The upload
# record holds two effects after its request id and return value (linux/uinput.h).
FF_EFFECT_SIZE = 44 if P == 4 else 48
FF_UPLOAD_SIZE = 8 + 2 * FF_EFFECT_SIZE
UI_BEGIN_FF_UPLOAD = _IOC(3, "U", 200, FF_UPLOAD_SIZE)
UI_END_FF_UPLOAD = _IOC(1, "U", 201, FF_UPLOAD_SIZE)
UI_BEGIN_FF_ERASE = _IOC(3, "U", 202, 12)
UI_END_FF_ERASE = _IOC(1, "U", 203, 12)

running = True
_log_lock = threading.Lock()
def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    with _log_lock:
        sys.stdout.write(line); sys.stdout.flush()
        try:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000: os.replace(LOG, LOG + ".old")
            with open(LOG, "a") as f: f.write(line)
        except OSError: pass

def led_packet(player):
    """Xbox 360 set-LED packet: pattern 2..5 = "blink, then light player 1..4"."""
    if not 1 <= player <= MAX_PLAYERS: raise ValueError("player must be 1..%d, got %r" % (MAX_PLAYERS, player))
    return bytes([0x01, 0x03, 0x01 + player])

def _read(path):
    with open(path) as f: return f.read()

def find_receivers():
    """All receivers in XInput mode as (syspath, busnum, devnum), sorted by syspath."""
    found = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            if int(_read(d + "/idVendor"), 16) != VID or int(_read(d + "/idProduct"), 16) != PID: continue
            intf = d + "/" + os.path.basename(d) + ":1.0"
            if _read(intf + "/bInterfaceClass").strip() != "ff" or _read(intf + "/bInterfaceSubClass").strip() != "5d": continue
            found.append((d, int(_read(d + "/busnum")), int(_read(d + "/devnum"))))
        except (OSError, ValueError): continue
    return found


def receiver_serial(syspath):
    try:
        with open(syspath + "/serial") as descriptor:
            return descriptor.read().strip() or None
    except OSError:
        return None

class Uinput:
    def __init__(self, vendor, product, version, rumble=False):
        mode = os.O_RDWR if rumble else os.O_WRONLY
        self.fd = os.open("/dev/uinput", mode | os.O_NONBLOCK)
        try:
            for ev in (EV_KEY, EV_ABS): fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
            for b in BUTTONS: fcntl.ioctl(self.fd, UI_SET_KEYBIT, b)
            for code, mn, mx, fuzz, flat in AXES:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("HHiiiiii", code, 0, 0, mn, mx, fuzz, flat, 0))
            if rumble:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_FF)
                fcntl.ioctl(self.fd, UI_SET_FFBIT, FF_RUMBLE)
            setup = struct.pack("HHHH", 0x03, vendor, product, version)
            setup += UINPUT_NAME.encode().ljust(80, b"\0") + struct.pack("I", FF_EFFECTS_MAX if rumble else 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self.state = {}
    def forward(self, state):
        changes = [(t, c, v) for (t, c), v in state.items() if self.state.get((t, c)) != v]
        if not changes:
            return 0
        now = time.time(); sec, usec = int(now), int((now % 1) * 1e6)
        buf = b"".join(struct.pack(EVENT_FMT, sec, usec, t, c, v) for t, c, v in changes)
        buf += struct.pack(EVENT_FMT, sec, usec, EV_SYN, 0, 0)
        try:
            if os.write(self.fd, buf) != len(buf):
                raise OSError("incomplete input event write")
        except OSError as e:
            log("uinput write error: %s" % e)
            return None
        self.state.update(state)
        return len(changes)
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
    def __init__(self, bus, dev, player=1):
        self.led = led_packet(player)
        self.fd = os.open("/dev/bus/usb/%03d/%03d" % (bus, dev), os.O_RDWR)
        self.claimed = False
        self.output_lock = threading.Lock()
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
        with self.output_lock:
            for value, length in ((0x0100, 20), (0x0000, 4)):
                try: self.control(0xC1, 0x01, value, 0x0000, length)
                except OSError as e: log("capability request %#06x failed: %s" % (value, e))
            try: self.xfer(EP_OUT, self.led, timeout=500); return True
            except OSError as e: log("LED packet failed: %s" % e); return False

    def rumble(self, strong, weak):
        with self.output_lock:
            self.xfer(EP_OUT, bytes((0, 8, 0, strong, weak, 0, 0, 0)), timeout=200)
    def close(self):
        if self.claimed:
            try: fcntl.ioctl(self.fd, USBDEVFS_RELEASE, struct.pack("I", 0))
            except OSError: pass
            self.driver_ioctl(USBDEVFS_CONNECT)       # give the interface back to xpad
        os.close(self.fd)

class Rumble:
    def __init__(self, pad, receiver=None):
        self.fd = pad.fd
        self.receiver = receiver
        self.receiver_lock = threading.Lock()
        self.effects = {}
        self.playing = None
        self.motor_state = (0, 0)
        self.last_report = None
        self.stop_requested = threading.Event()
        self.thread = threading.Thread(target=self.run, name="controller-rumble")
        self.logged_first_pulse = False

    def upload(self, request_id):
        data = bytearray(FF_UPLOAD_SIZE)
        struct.pack_into("=I", data, 0, request_id)
        fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, data)
        effect_type, effect_id = struct.unpack_from("=Hh", data, 8)
        result = -errno.EINVAL
        if effect_type == FF_RUMBLE and 0 <= effect_id < FF_EFFECTS_MAX:
            length, delay = struct.unpack_from("=HH", data, 18)
            strong, weak = struct.unpack_from("=HH", data, 24)
            self.effects[effect_id] = (strong >> 8, weak >> 8, length, delay)
            result = 0
        struct.pack_into("=i", data, 4, result)
        fcntl.ioctl(self.fd, UI_END_FF_UPLOAD, data)

    def erase(self, request_id):
        data = bytearray(12)
        struct.pack_into("=I", data, 0, request_id)
        fcntl.ioctl(self.fd, UI_BEGIN_FF_ERASE, data)
        effect_id, = struct.unpack_from("=I", data, 8)
        self.effects.pop(effect_id, None)
        struct.pack_into("=i", data, 4, 0)
        fcntl.ioctl(self.fd, UI_END_FF_ERASE, data)
        if self.playing is not None and self.playing[0] == effect_id:
            self.playing = None

    def handle_event(self, event_type, code, value):
        if event_type == EV_UINPUT:
            if code == UI_FF_UPLOAD:
                self.upload(value)
            elif code == UI_FF_ERASE:
                self.erase(value)
        elif event_type == EV_FF and self.receiver is not None and code in self.effects:
            if value > 0:
                strong, weak, length, delay = self.effects[code]
                starts = time.monotonic() + delay / 1000
                ends = starts + length * value / 1000 if length else None
                self.playing = (code, (strong, weak), starts, ends)
            elif self.playing is not None and self.playing[0] == code:
                self.playing = None

    def update_motors(self):
        if self.receiver is None:
            return
        motors = (0, 0)
        if self.playing is not None:
            _, requested, starts, ends = self.playing
            now = time.monotonic()
            if ends is not None and now >= ends:
                self.playing = None
            elif now >= starts:
                last_report = self.last_report
                if last_report is None or now - last_report >= RUMBLE_INPUT_TIMEOUT_S:
                    if self.motor_state != (0, 0):
                        log("rumble stopped after input reports went quiet")
                    self.playing = None
                else:
                    motors = requested
        if motors != self.motor_state:
            # A failed start may still have reached the receiver. A failed
            # stop should leave the worker's final stop attempt armed.
            if motors != (0, 0):
                self.motor_state = motors
            self.receiver.rumble(*motors)
            self.motor_state = motors
            if motors != (0, 0) and not self.logged_first_pulse:
                log("first force-feedback request forwarded to receiver")
                self.logged_first_pulse = True

    def attach(self, receiver):
        with self.receiver_lock:
            self.receiver = receiver
            self.last_report = None
            self.logged_first_pulse = False

    def detach(self):
        with self.receiver_lock:
            self.stop_motors()
            self.playing = None
            self.last_report = None
            self.receiver = None

    def stop_motors(self):
        if self.receiver is not None and self.motor_state != (0, 0):
            try:
                self.receiver.rumble(0, 0)
            except OSError as e:
                if e.errno not in (errno.ENODEV, errno.ESHUTDOWN):
                    log("rumble stop failed: %s" % e)
        self.motor_state = (0, 0)

    def run(self):
        try:
            while not self.stop_requested.is_set():
                readable, _, _ = select.select([self.fd], [], [], 0.025)
                data = b""
                if readable:
                    try:
                        data = os.read(self.fd, EVENT_SIZE * 32)
                    except BlockingIOError:
                        pass
                with self.receiver_lock:
                    for _, _, event_type, code, value in struct.iter_unpack(EVENT_FMT, data):
                        self.handle_event(event_type, code, value)
                    try:
                        self.update_motors()
                    except OSError as e:
                        log("rumble transfer failed: %s" % e)
                        self.playing = None
                        self.stop_motors()
        except OSError as e:
            log("rumble worker stopped: %s" % e)
        finally:
            self.detach()

    def close(self):
        self.stop_requested.set()
        self.thread.join()


class PadSession:
    """One player slot: its uinput pad, optional rumble worker, and the receiver it last served."""
    def __init__(self, rumble_enabled, player=1):
        self.rumble_enabled = rumble_enabled
        self.player = player
        self.pad = None
        self.feedback = None
        self.remove_at = None
        self.serial = None
        self.port = None

    def matches(self, syspath):
        """True if this slot holds a pad for the receiver at syspath: same USB serial, or same port if either has none."""
        if self.pad is None:
            return False
        serial = receiver_serial(syspath)
        return self.serial == serial if self.serial and serial else self.port == os.path.basename(syspath)

    def prepare(self, syspath, version):
        self.expire()
        serial = receiver_serial(syspath)
        port = os.path.basename(syspath)
        if self.pad is not None:
            if self.matches(syspath):
                self.serial = serial or self.serial
                self.port = port
                return
            self.close()
        rumble = self.rumble_enabled
        try:
            self.pad = Uinput(VID, PID, version, rumble=rumble)
        except OSError:
            if not rumble:
                raise
            log("force feedback unavailable; creating an input-only device")
            rumble = False
            self.pad = Uinput(VID, PID, version)
        self.serial = serial
        self.port = port
        log("uinput device created: %r (player %d)" % (UINPUT_NAME, self.player))
        if rumble:
            worker = Rumble(self.pad)
            worker.thread.start()
            self.feedback = worker
            log("force feedback enabled")
        # The first pad must be visible to webOS before native xpad detaches.
        time.sleep(0.5)

    def attach(self, receiver):
        if self.feedback is not None:
            self.feedback.attach(receiver)
        if self.remove_at is not None:
            log("receiver returned; reusing the existing uinput device")
        self.remove_at = None

    def forward(self, state):
        if self.feedback is not None:
            self.feedback.last_report = time.monotonic()
        return self.pad.forward(state)

    def detach(self):
        if self.feedback is not None:
            self.feedback.detach()
        if self.pad is None or self.remove_at is not None:
            return
        if self.pad.forward(dict.fromkeys(self.pad.state, 0)) is None:
            self.close()
            return
        self.remove_at = time.monotonic() + RECONNECT_GRACE_S
        log("receiver unavailable; holding the neutral uinput device for %gs" % RECONNECT_GRACE_S)

    def expire(self):
        if self.remove_at is not None and time.monotonic() >= self.remove_at:
            self.close()

    def close(self):
        if self.feedback is not None:
            self.feedback.close()
            self.feedback = None
        if self.pad is not None:
            self.pad.close()
            self.pad = None
            log("uinput device removed")
        self.remove_at = None
        self.serial = None
        self.port = None


def serve(syspath, bus, dev, session):
    version = int(_read(syspath + "/bcdDevice"), 16)
    rx = Receiver(bus, dev, session.player)
    try:
        session.prepare(syspath, version)
        rx.claim(); log("claimed %s (bus %d dev %d) as player %d; xpad detached" % (os.path.basename(syspath), bus, dev, session.player))
        rx.handshake(); last_led = time.time(); last_report = 0.0; reports = 0; events = 0; tick = time.time()
        session.attach(rx)
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
            events += session.forward(state) or 0
            if time.time() - tick >= 300: log("stats: %d reports, %d events in last 5 min" % (reports, events)); reports = events = 0; tick = time.time()
    finally:
        try:
            session.detach()
        finally:
            rx.close()
            log("released receiver interface")

def pick_session(syspath, idle):
    """Slot for a new receiver: the one holding its pad, else the lowest empty one, else the held pad nearest expiry."""
    for session in idle:
        if session.matches(syspath): return session
    for session in idle:
        if session.pad is None: return session
    held = [session for session in idle if session.remove_at is not None]
    return min(held, key=lambda session: session.remove_at) if held else None

def main():
    global running
    def stop(signum, frame):
        global running; running = False
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    log("bridge starting (pid %d), waiting for %04x:%04x (up to %d)" % (os.getpid(), VID, PID, MAX_PLAYERS))
    rumble = os.path.isfile(RUMBLE_FLAG)
    sessions = [PadSession(rumble, player) for player in range(1, MAX_PLAYERS + 1)]

    def run(key, session):
        try: serve(*key, session)
        except OSError as e: log("serve failed for %s: %s" % (os.path.basename(key[0]), e))

    workers = {}      # (syspath, bus, dev) -> (thread, session)
    finished = set()  # instances whose serve() returned; not served again until they re-enumerate
    waiting = set()   # instances already logged as waiting for a free player slot
    try:
        while running:
            for key, (thread, _) in list(workers.items()):
                if not thread.is_alive(): del workers[key]; finished.add(key)
            present = set(find_receivers())
            finished &= present; waiting &= present
            busy = [session for _, session in workers.values()]
            idle = [session for session in sessions if session not in busy]
            for session in idle: session.expire()
            # A slot whose receiver has vanished but whose thread is still winding down.
            leaving = [session for key, (_, session) in workers.items() if key not in present]
            for key in sorted(present - finished - set(workers)):
                if any(session.matches(key[0]) for session in leaving):
                    continue                  # same receiver re-enumerated; wait for its slot to free up
                session = pick_session(key[0], idle)
                if session is None:
                    if key not in waiting:
                        log("%s waiting: all %d player slots in use" % (os.path.basename(key[0]), MAX_PLAYERS)); waiting.add(key)
                    continue
                waiting.discard(key); idle.remove(session)
                thread = threading.Thread(target=run, args=(key, session), name="serve-p%d" % session.player, daemon=True)
                workers[key] = (thread, session); thread.start()
            time.sleep(1.0)
    finally:
        running = False                       # also on an unexpected error, so the workers stop
        for thread, _ in workers.values(): thread.join()
        for session in sessions: session.close()
    log("bridge stopped")

if __name__ == "__main__": main()
