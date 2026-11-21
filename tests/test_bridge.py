"""Hardware-free tests for ultimate2c-bridge.py. Run: python3 -m unittest discover -s tests -v"""
import ctypes, errno, io, os, struct, sys, tempfile, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
# Compile from source every time: a cached .pyc can go stale when the file is rewritten with the same
# size within the same second (mtime resolution), silently testing old code.
SRC = os.path.join(HERE, "..", "ultimate2c-bridge.py")
bridge = types.ModuleType("bridge"); bridge.__file__ = SRC
with open(SRC) as _f: exec(compile(_f.read(), SRC, "exec"), bridge.__dict__)

EV_KEY, EV_ABS = bridge.EV_KEY, bridge.EV_ABS


def write(path, text):
    with open(path, "w") as f: f.write(text)


def report(buttons2=0, buttons3=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0):
    """Build a 20-byte Xbox 360 XInput input report."""
    return bytes([0x00, 0x14, buttons2, buttons3, lt, rt]) + struct.pack("<hhhh", lx, ly, rx, ry) + bytes(6)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for target, attr, val in ((bridge, "LOG", os.path.join(self.tmp.name, "bridge.log")),
                                  (sys, "stdout", io.StringIO()), (bridge, "running", True)):
            p = mock.patch.object(target, attr, val); p.start(); self.addCleanup(p.stop)

    def logged(self):
        try:
            with open(bridge.LOG) as f: return f.read()
        except FileNotFoundError: return ""


class IoctlNumbers(unittest.TestCase):
    """Compare against values from the Linux headers for the running pointer size."""
    def test_fixed_size_ioctls(self):
        self.assertEqual(bridge.USBDEVFS_CLAIM, 0x8004550F)
        self.assertEqual(bridge.USBDEVFS_RELEASE, 0x80045510)
        self.assertEqual(bridge.USBDEVFS_DISCONNECT, 0x5516)
        self.assertEqual(bridge.USBDEVFS_CONNECT, 0x5517)
        self.assertEqual(bridge.UI_DEV_CREATE, 0x5501)
        self.assertEqual(bridge.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(bridge.UI_DEV_SETUP, 0x405C5503)
        self.assertEqual(bridge.UI_ABS_SETUP, 0x401C5504)
        self.assertEqual(bridge.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(bridge.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(bridge.UI_SET_ABSBIT, 0x40045567)

    def test_pointer_size_dependent_ioctls(self):
        expected = {8: (0xC0185500, 0xC0185502, 0xC0105512), 4: (0xC0105500, 0xC0105502, 0xC00C5512)}[bridge.P]
        self.assertEqual((bridge.USBDEVFS_CONTROL, bridge.USBDEVFS_BULK, bridge.USBDEVFS_IOCTL), expected)

    def test_force_feedback_ioctls(self):
        self.assertEqual(bridge.UI_SET_FFBIT, 0x4004556B)
        self.assertEqual(bridge.UI_BEGIN_FF_ERASE, 0xC00C55CA)
        self.assertEqual(bridge.UI_END_FF_ERASE, 0x400C55CB)
        upload = {8: 0x68, 4: 0x60}[bridge.P]                 # sizeof(struct uinput_ff_upload)
        self.assertEqual(bridge.UI_BEGIN_FF_UPLOAD, 0xC00055C8 | upload << 16)
        self.assertEqual(bridge.UI_END_FF_UPLOAD, 0x400055C9 | upload << 16)

    def test_ff_effect_layout(self):
        """Mirror struct ff_effect from linux/input.h with ctypes to check the hard-coded sizes and offsets."""
        u16, s16, u32 = ctypes.c_uint16, ctypes.c_int16, ctypes.c_uint32
        class Envelope(ctypes.Structure): _fields_ = [(n, u16) for n in ("al", "alev", "fl", "flev")]
        class Periodic(ctypes.Structure):
            _fields_ = [("waveform", u16), ("period", u16), ("magnitude", s16), ("offset", s16), ("phase", u16),
                        ("envelope", Envelope), ("custom_len", u32), ("custom_data", ctypes.c_void_p)]
        class Rumble(ctypes.Structure): _fields_ = [("strong", u16), ("weak", u16)]
        class U(ctypes.Union): _fields_ = [("periodic", Periodic), ("rumble", Rumble)]
        class Effect(ctypes.Structure):
            _fields_ = [("type", u16), ("id", s16), ("direction", u16), ("button", u16), ("interval", u16),
                        ("length", u16), ("delay", u16), ("u", U)]
        self.assertEqual(ctypes.sizeof(Effect), bridge.FF_EFFECT_SIZE)
        self.assertEqual(8 + Effect.length.offset, 18)        # offsets Rumble.upload reads, within uinput_ff_upload
        self.assertEqual(8 + Effect.u.offset, 24)

    def test_struct_sizes(self):
        self.assertEqual(len(struct.pack("HHHH", 0, 0, 0, 0) + bytes(80) + struct.pack("I", 0)), 92)   # uinput_setup
        self.assertEqual(struct.calcsize("HHiiiiii"), 28)                                              # uinput_abs_setup
        self.assertEqual(struct.calcsize(bridge.EVENT_FMT), 2 * ctypes.sizeof(ctypes.c_long) + 8)      # input_event


class ParseReport(unittest.TestCase):
    def test_rejects_bad_reports(self):
        self.assertIsNone(bridge.parse_report(b""))
        self.assertIsNone(bridge.parse_report(report()[:13]))
        self.assertIsNone(bridge.parse_report(b"\x01" + report()[1:]))     # LED status message type
        self.assertIsNone(bridge.parse_report(b"\x00\x03\x03"))             # short non-input message

    def test_covers_every_advertised_code(self):
        s = bridge.parse_report(report())
        self.assertEqual(set(s), {(EV_KEY, b) for b in bridge.BUTTONS} | {(EV_ABS, a[0]) for a in bridge.AXES})

    def test_neutral(self):
        s = bridge.parse_report(report())
        self.assertTrue(all(v == 0 for (t, c), v in s.items() if c not in (bridge.ABS_Y, bridge.ABS_RY)))
        self.assertEqual(s[(EV_ABS, bridge.ABS_Y)], -1)    # ~0, as xpad does it

    def test_buttons(self):
        cases = [(2, 0x10, bridge.BTN_START), (2, 0x20, bridge.BTN_SELECT), (2, 0x40, bridge.BTN_THUMBL),
                 (2, 0x80, bridge.BTN_THUMBR), (3, 0x01, bridge.BTN_TL), (3, 0x02, bridge.BTN_TR),
                 (3, 0x04, bridge.BTN_MODE), (3, 0x10, bridge.BTN_A), (3, 0x20, bridge.BTN_B),
                 (3, 0x40, bridge.BTN_X), (3, 0x80, bridge.BTN_Y)]
        for byte, bit, code in cases:
            with self.subTest(code=hex(code)):
                s = bridge.parse_report(report(**{"buttons%d" % byte: bit}))
                pressed = [c for (t, c), v in s.items() if t == EV_KEY and v]
                self.assertEqual(pressed, [code])
        s = bridge.parse_report(report(buttons2=0xF0, buttons3=0xF7))
        self.assertTrue(all(s[(EV_KEY, b)] == 1 for b in bridge.BUTTONS))

    def test_dpad(self):
        for b2, hat in ((0x01, (0, -1)), (0x02, (0, 1)), (0x04, (-1, 0)), (0x08, (1, 0)),
                        (0x05, (-1, -1)), (0x0A, (1, 1)), (0x03, (0, 0)), (0x0C, (0, 0))):
            with self.subTest(b2=b2):
                s = bridge.parse_report(report(buttons2=b2))
                self.assertEqual((s[(EV_ABS, bridge.ABS_HAT0X)], s[(EV_ABS, bridge.ABS_HAT0Y)]), hat)

    def test_triggers_and_sticks(self):
        s = bridge.parse_report(report(lt=255, rt=17, lx=-32768, ly=-32768, rx=32767, ry=32767))
        self.assertEqual(s[(EV_ABS, bridge.ABS_Z)], 255)
        self.assertEqual(s[(EV_ABS, bridge.ABS_RZ)], 17)
        self.assertEqual(s[(EV_ABS, bridge.ABS_X)], -32768)
        self.assertEqual(s[(EV_ABS, bridge.ABS_RX)], 32767)
        self.assertEqual(s[(EV_ABS, bridge.ABS_Y)], 32767)       # Y is inverted
        self.assertEqual(s[(EV_ABS, bridge.ABS_RY)], -32768)
        for (t, c), v in s.items():
            if t == EV_ABS:
                lo, hi = next((a[1], a[2]) for a in bridge.AXES if a[0] == c)
                self.assertTrue(lo <= v <= hi, (c, v))


class FindReceiver(Base):
    def make_dev(self, name, vid="2dc8", pid="310a", cls="ff", sub="5d", busnum=1, devnum=5, intf=True):
        d = os.path.join(self.tmp.name, name); os.makedirs(d)
        for f, v in (("idVendor", vid), ("idProduct", pid), ("busnum", busnum), ("devnum", devnum)):
            write(os.path.join(d, f), "%s\n" % v)
        if intf:
            i = os.path.join(d, name + ":1.0"); os.makedirs(i)
            write(os.path.join(i, "bInterfaceClass"), cls + "\n")
            write(os.path.join(i, "bInterfaceSubClass"), sub + "\n")
        return d

    def find(self):
        with mock.patch.object(bridge.glob, "glob", lambda pat: sorted(
                os.path.join(self.tmp.name, n) for n in os.listdir(self.tmp.name) if not n.endswith(".log"))):
            return bridge.find_receiver()

    def test_none(self):
        self.assertIsNone(self.find())

    def test_finds_matching_device(self):
        d = self.make_dev("1-1", busnum=1, devnum=7)
        self.assertEqual(self.find(), (d, 1, 7))

    def test_skips_non_matching(self):
        self.make_dev("1-1", pid="3106")
        self.make_dev("1-2", vid="045e")
        self.make_dev("1-3", cls="03", sub="00")           # receiver in HID mode
        self.make_dev("1-4", intf=False)                   # interface not populated yet
        os.makedirs(os.path.join(self.tmp.name, "1-0:1.0"))  # interface entries have no idVendor
        self.assertIsNone(self.find())
        d = self.make_dev("2-1", busnum=2, devnum=3)
        self.assertEqual(self.find(), (d, 2, 3))


class UinputTest(Base):
    def setUp(self):
        super().setUp()
        self.ioctls, self.writes = [], []
        for target, attr, fn in ((bridge.os, "open", lambda *a: 42), (bridge.os, "close", lambda fd: None),
                                 (bridge.fcntl, "ioctl", lambda fd, req, arg=0: self.ioctls.append((req, arg))),
                                 (bridge.os, "write", lambda fd, buf: self.writes.append(buf) or len(buf))):
            p = mock.patch.object(target, attr, fn); p.start(); self.addCleanup(p.stop)

    def test_setup(self):
        ui = bridge.Uinput(0x2dc8, 0x310a, 0x0114)
        reqs = [r for r, _ in self.ioctls]
        self.assertEqual(reqs[-2:], [bridge.UI_DEV_SETUP, bridge.UI_DEV_CREATE])
        self.assertEqual({a for r, a in self.ioctls if r == bridge.UI_SET_EVBIT}, {EV_KEY, EV_ABS})
        self.assertEqual({a for r, a in self.ioctls if r == bridge.UI_SET_KEYBIT}, set(bridge.BUTTONS))
        self.assertEqual({a for r, a in self.ioctls if r == bridge.UI_SET_ABSBIT}, {a[0] for a in bridge.AXES})
        absinfo = [struct.unpack("HHiiiiii", a) for r, a in self.ioctls if r == bridge.UI_ABS_SETUP]
        self.assertEqual([(c, mn, mx, fz, fl) for c, _, _, mn, mx, fz, fl, _ in absinfo], bridge.AXES)
        setup = next(a for r, a in self.ioctls if r == bridge.UI_DEV_SETUP)
        self.assertEqual(struct.unpack_from("HHHH", setup), (0x03, 0x2dc8, 0x310a, 0x0114))
        self.assertEqual(setup[8:88].rstrip(b"\0").decode(), bridge.UINPUT_NAME)
        ui.close()
        self.assertEqual(self.ioctls[-1][0], bridge.UI_DEV_DESTROY)

    def events(self, buf):
        return [struct.unpack_from(bridge.EVENT_FMT, buf, i)[2:] for i in range(0, len(buf), bridge.EVENT_SIZE)]

    def test_forward_writes_only_changes(self):
        ui = bridge.Uinput(1, 2, 3)
        self.assertEqual(ui.forward({(EV_KEY, bridge.BTN_A): 1, (EV_ABS, bridge.ABS_X): -5}), 2)
        self.assertEqual(self.events(self.writes[0]), [(EV_KEY, bridge.BTN_A, 1), (EV_ABS, bridge.ABS_X, -5), (bridge.EV_SYN, 0, 0)])
        self.assertEqual(ui.forward({(EV_KEY, bridge.BTN_A): 1, (EV_ABS, bridge.ABS_X): -5}), 0)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(ui.forward({(EV_KEY, bridge.BTN_A): 0, (EV_ABS, bridge.ABS_X): -5}), 1)
        self.assertEqual(self.events(self.writes[1]), [(EV_KEY, bridge.BTN_A, 0), (bridge.EV_SYN, 0, 0)])

    def test_forward_error_keeps_old_state(self):
        ui = bridge.Uinput(1, 2, 3)
        with mock.patch.object(bridge.os, "write", side_effect=OSError(errno.EAGAIN, "busy")):
            self.assertIsNone(ui.forward({(EV_KEY, bridge.BTN_A): 1}))
        self.assertIn("uinput write error", self.logged())
        self.assertEqual(ui.state, {})
        self.assertEqual(ui.forward({(EV_KEY, bridge.BTN_A): 1}), 1)   # retried on the next report

    def test_forward_short_write_is_an_error(self):
        ui = bridge.Uinput(1, 2, 3)
        with mock.patch.object(bridge.os, "write", lambda fd, buf: len(buf) - 1):
            self.assertIsNone(ui.forward({(EV_KEY, bridge.BTN_A): 1}))

    def test_rumble_setup(self):
        opened = []
        with mock.patch.object(bridge.os, "open", lambda path, flags: opened.append(flags) or 42):
            bridge.Uinput(1, 2, 3, rumble=True)
        self.assertTrue(opened[0] & bridge.os.O_RDWR)
        self.assertIn((bridge.UI_SET_EVBIT, bridge.EV_FF), self.ioctls)
        self.assertIn((bridge.UI_SET_FFBIT, bridge.FF_RUMBLE), self.ioctls)
        setup = next(a for r, a in self.ioctls if r == bridge.UI_DEV_SETUP)
        self.assertEqual(struct.unpack_from("I", setup, 88)[0], bridge.FF_EFFECTS_MAX)

    def test_no_rumble_by_default(self):
        bridge.Uinput(1, 2, 3)
        self.assertNotIn((bridge.UI_SET_EVBIT, bridge.EV_FF), self.ioctls)
        setup = next(a for r, a in self.ioctls if r == bridge.UI_DEV_SETUP)
        self.assertEqual(struct.unpack_from("I", setup, 88)[0], 0)

    def test_setup_failure_closes_fd(self):
        closed = []
        def ioctl(fd, req, arg=0):
            if req == bridge.UI_DEV_CREATE: raise OSError(errno.EINVAL, "bad")
        with mock.patch.object(bridge.fcntl, "ioctl", ioctl), mock.patch.object(bridge.os, "close", closed.append):
            with self.assertRaises(OSError): bridge.Uinput(1, 2, 3)
        self.assertEqual(closed, [42])


class ReceiverTest(Base):
    """Fake usbfs: decode the structs the code passes and fill the data buffers through their pointers."""
    def setUp(self):
        super().setUp()
        self.calls, self.in_data, self.fail = [], b"", {}
        for target, attr, fn in ((bridge.os, "open", lambda *a: 7), (bridge.os, "close", lambda fd: None),
                                 (bridge.fcntl, "ioctl", self.ioctl)):
            p = mock.patch.object(target, attr, fn); p.start(); self.addCleanup(p.stop)

    def ioctl(self, fd, req, arg=0):
        arg = bytes(arg); A = bridge._align(12, bridge.P)
        ptr = struct.unpack_from("P", arg, A)[0] if req in (bridge.USBDEVFS_BULK, bridge.USBDEVFS_CONTROL) else None
        if req == bridge.USBDEVFS_BULK:
            ep, length, timeout = struct.unpack_from("III", arg)
            if ep & 0x80:
                data = self.in_data[:length]; ctypes.memmove(ptr, data, len(data)); call = ("bulk_in", ep, length, timeout)
            else:
                data = ctypes.string_at(ptr, length); call = ("bulk_out", ep, data, timeout)
        elif req == bridge.USBDEVFS_CONTROL:
            rtype, r, value, index, length, timeout = struct.unpack_from("BBHHHI", arg)
            call = ("control", rtype, r, value, index, length); data = bytes(length)
        elif req == bridge.USBDEVFS_IOCTL:
            call = ("driver", struct.unpack_from("ii", arg)[1]); data = b""
        else:
            call = (req, struct.unpack("I", arg)[0] if arg else None); data = b""
        self.calls.append(call)
        key = call[0] if isinstance(call[0], str) and call[0] != "driver" else call
        if key in self.fail: raise OSError(self.fail[key], os.strerror(self.fail[key]))
        return len(data)

    def test_claim_detaches_then_claims(self):
        rx = bridge.Receiver(1, 5); rx.claim()
        self.assertEqual(self.calls, [("driver", bridge.USBDEVFS_DISCONNECT), (bridge.USBDEVFS_CLAIM, 0)])
        self.assertTrue(rx.claimed)

    def test_claim_with_no_driver_bound_is_quiet(self):
        self.fail[("driver", bridge.USBDEVFS_DISCONNECT)] = errno.ENODATA
        bridge.Receiver(1, 5).claim()
        self.assertEqual(self.logged(), "")

    def test_claim_failure_propagates(self):
        self.fail[(bridge.USBDEVFS_CLAIM, 0)] = errno.EBUSY
        rx = bridge.Receiver(1, 5)
        with self.assertRaises(OSError): rx.claim()
        self.assertFalse(rx.claimed)

    def test_handshake(self):
        self.assertTrue(bridge.Receiver(1, 5).handshake())
        self.assertEqual(self.calls, [("control", 0xC1, 0x01, 0x0100, 0, 20), ("control", 0xC1, 0x01, 0x0000, 0, 4),
                                      ("bulk_out", bridge.EP_OUT, b"\x01\x03\x02", 500)])

    def test_handshake_survives_capability_failure(self):
        self.fail["control"] = errno.EPIPE
        self.assertTrue(bridge.Receiver(1, 5).handshake())
        self.assertEqual(self.calls[-1][:3], ("bulk_out", bridge.EP_OUT, bridge.LED_PACKET))
        self.assertIn("capability request", self.logged())

    def test_handshake_reports_led_failure(self):
        self.fail["bulk_out"] = errno.ETIMEDOUT
        self.assertFalse(bridge.Receiver(1, 5).handshake())
        self.assertIn("LED packet failed", self.logged())

    def test_read(self):
        self.in_data = report(buttons3=0x10)
        self.assertEqual(bridge.Receiver(1, 5).xfer(bridge.EP_IN, length=32), self.in_data)
        self.assertEqual(self.calls, [("bulk_in", bridge.EP_IN, 32, 500)])

    def test_close(self):
        rx = bridge.Receiver(1, 5); rx.close()
        self.assertEqual(self.calls, [])                     # nothing to give back if never claimed
        rx = bridge.Receiver(1, 5); rx.claim(); del self.calls[:]; rx.close()
        self.assertEqual(self.calls, [(bridge.USBDEVFS_RELEASE, 0), ("driver", bridge.USBDEVFS_CONNECT)])


class FakeReceiver:
    """Plays a script of reads: bytes are reports, ints are errnos. Each timeout advances the clock 0.5 s."""
    def __init__(self, test, script):
        self.test, self.script, self.handshakes, self.closed, self.claimed = test, list(script), [], False, False
    def __call__(self, bus, dev): return self
    def claim(self): self.claimed = True
    def handshake(self): self.handshakes.append(self.test.now); return True
    def xfer(self, ep, data=None, length=32, timeout=500):
        if not self.script: raise OSError(errno.ENODEV, "gone")
        item = self.script.pop(0)
        if isinstance(item, bytes): self.test.now += 0.01; return item
        if item == errno.ETIMEDOUT: self.test.now += 0.5
        raise OSError(item, os.strerror(item))
    def close(self): self.closed = True


class FakeUinput:
    def __init__(self, *a, rumble=False):
        self.args, self.rumble, self.state, self.emitted, self.closed = a, rumble, {}, [], False
    def forward(self, state):
        changes = [(t, c, v) for (t, c), v in state.items() if self.state.get((t, c)) != v]
        if not changes: return 0
        self.emitted.append(changes); self.state.update(state); return len(changes)
    def close(self): self.closed = True


class ServeTest(Base):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.sys = os.path.join(self.tmp.name, "1-1"); os.makedirs(self.sys)
        write(os.path.join(self.sys, "bcdDevice"), "0114\n")
        for target, attr, fn in ((bridge.time, "time", lambda: self.now), (bridge.time, "sleep", lambda s: None)):
            p = mock.patch.object(target, attr, fn); p.start(); self.addCleanup(p.stop)

    def serve(self, script, uinput=None):
        rx = FakeReceiver(self, script); ui = uinput or FakeUinput(); session = bridge.PadSession(False)
        with mock.patch.object(bridge, "Receiver", rx), \
             mock.patch.object(bridge, "Uinput", lambda *a, **k: ui.__init__(*a, **k) or ui):
            bridge.serve(self.sys, 1, 5, session)
        self.session = session
        return rx, ui

    def test_emits_only_changes_and_holds_pad(self):
        rx, ui = self.serve([report(), report(), report(buttons3=0x10), report(buttons3=0x10), report()])
        self.assertEqual(ui.args, (bridge.VID, bridge.PID, 0x0114))
        self.assertTrue(rx.claimed)
        self.assertEqual(len(ui.emitted[0]), 19)             # first report sets every value
        self.assertEqual(ui.emitted[1:3], [[(EV_KEY, bridge.BTN_A, 1)], [(EV_KEY, bridge.BTN_A, 0)]])
        self.assertTrue(all(v == 0 for v in ui.state.values()))   # inputs released when the receiver went away
        self.assertTrue(rx.closed)
        self.assertFalse(ui.closed)                          # pad held for the reconnect grace period
        self.assertIsNotNone(self.session.remove_at)
        self.assertIn("input reports flowing", self.logged())
        self.assertIn("receiver gone", self.logged())

    def test_ignores_non_input_messages(self):
        rx, ui = self.serve([b"\x01\x03\x02", b"\x02\x03\x00", b"\x03\x03\x03"])
        self.assertEqual(ui.emitted, [])
        self.assertNotIn("input reports flowing", self.logged())

    def test_handshake_retried_every_10s_until_first_report(self):
        rx, _ = self.serve([errno.ETIMEDOUT] * 45)            # 22.5 s of silence
        self.assertEqual([round(t - 1000.0, 1) for t in rx.handshakes], [0.0, 10.0, 20.0])

    def test_handshake_retried_after_60s_silence_once_streaming(self):
        rx, _ = self.serve([report()] + [errno.ETIMEDOUT] * 130)   # ~65 s of silence after a report
        self.assertEqual(len(rx.handshakes), 2)
        self.assertGreaterEqual(rx.handshakes[1] - 1000.01, 60.0)

    def test_transient_error_continues(self):
        rx, ui = self.serve([errno.EOVERFLOW, report(buttons3=0x20)])
        self.assertIn("read error", self.logged())
        self.assertIn((EV_KEY, bridge.BTN_B, 1), ui.emitted[0])

    def test_disconnect_errnos_end_serve(self):
        for e in (errno.ENODEV, errno.ENOENT, errno.EIO, errno.EPIPE, errno.ESHUTDOWN):
            with self.subTest(errno=errno.errorcode[e]):
                rx, _ = self.serve([e, report()])
                self.assertEqual(rx.script, [report()])       # stopped before the next read
                self.assertTrue(rx.closed)

    def test_stops_when_running_cleared(self):
        class Stop(FakeUinput):
            def forward(s, state): bridge.running = False; return 0
        rx, _ = self.serve([report(), report(buttons3=0x10)], uinput=Stop())
        self.assertEqual(rx.script, [report(buttons3=0x10)])
        self.assertTrue(rx.closed)

    def test_cleanup_when_claim_fails(self):
        rx = FakeReceiver(self, []); ui = FakeUinput(); session = bridge.PadSession(False)
        rx.claim = mock.Mock(side_effect=OSError(errno.EBUSY, "busy"))
        with mock.patch.object(bridge, "Receiver", rx), mock.patch.object(bridge, "Uinput", lambda *a, **k: ui):
            with self.assertRaises(OSError): bridge.serve(self.sys, 1, 5, session)
        self.assertTrue(rx.closed)
        self.assertIsNotNone(session.remove_at)
        session.close()
        self.assertTrue(ui.closed)


class PadSessionTest(Base):
    def setUp(self):
        super().setUp()
        self.now, self.pads = 500.0, []
        def make(*a, **k):
            if k.get("rumble") and getattr(self, "no_ff", False): raise OSError(errno.EINVAL, "no ff")
            pad = FakeUinput(*a, **k); self.pads.append(pad); return pad
        for target, attr, fn in ((bridge, "Uinput", make), (bridge.time, "sleep", lambda s: None),
                                 (bridge.time, "monotonic", lambda: self.now)):
            p = mock.patch.object(target, attr, fn); p.start(); self.addCleanup(p.stop)

    def dev(self, port, serial=None):
        d = os.path.join(self.tmp.name, port); os.makedirs(d, exist_ok=True)
        if serial: write(os.path.join(d, "serial"), serial + "\n")
        return d

    def cycle(self, session, syspath):
        """One receiver session: prepare, attach, some input, receiver goes away."""
        session.prepare(syspath, 0x114); session.attach(object())
        session.forward({(EV_KEY, bridge.BTN_A): 1}); session.detach()

    def test_same_serial_reuses_pad_even_on_another_port(self):
        s = bridge.PadSession(False)
        self.cycle(s, self.dev("1-1", "AAA"))
        self.cycle(s, self.dev("1-2", "AAA"))
        self.assertEqual(len(self.pads), 1)
        self.assertIn("reusing the existing uinput device", self.logged())

    def test_different_serial_replaces_held_pad(self):
        s = bridge.PadSession(False)
        self.cycle(s, self.dev("1-1", "AAA"))
        s.prepare(self.dev("1-2", "BBB"), 0x114)
        self.assertEqual(len(self.pads), 2)
        self.assertTrue(self.pads[0].closed)

    def test_without_serial_matches_by_port(self):
        s = bridge.PadSession(False)
        self.cycle(s, self.dev("1-1"))
        self.cycle(s, self.dev("1-1"))
        self.assertEqual(len(self.pads), 1)
        s.prepare(self.dev("1-2"), 0x114)
        self.assertEqual(len(self.pads), 2)

    def test_detach_releases_held_inputs(self):
        s = bridge.PadSession(False)
        self.cycle(s, self.dev("1-1", "AAA"))
        self.assertEqual(self.pads[0].emitted[-1], [(EV_KEY, bridge.BTN_A, 0)])
        self.assertIn("holding the neutral uinput device", self.logged())

    def test_held_pad_removed_after_grace(self):
        s = bridge.PadSession(False)
        self.cycle(s, self.dev("1-1", "AAA"))
        self.now += bridge.RECONNECT_GRACE_S - 0.1; s.expire()
        self.assertFalse(self.pads[0].closed)
        self.now += 0.2; s.expire()
        self.assertTrue(self.pads[0].closed)
        self.assertIsNone(s.pad)

    def test_rumble_falls_back_to_input_only(self):
        self.no_ff = True
        s = bridge.PadSession(True)
        s.prepare(self.dev("1-1", "AAA"), 0x114)
        self.assertFalse(self.pads[0].rumble)
        self.assertIsNone(s.feedback)
        self.assertIn("force feedback unavailable", self.logged())


class FakeMotors:
    def __init__(self): self.sent = []
    def rumble(self, strong, weak): self.sent.append((strong, weak))


class RumbleTest(Base):
    """Rumble worker logic, driven directly (the worker thread is not started)."""
    def setUp(self):
        super().setUp()
        self.now, self.pending, self.ended = 100.0, None, []
        for target, attr, fn in ((bridge.fcntl, "ioctl", self.ioctl), (bridge.time, "monotonic", lambda: self.now)):
            p = mock.patch.object(target, attr, fn); p.start(); self.addCleanup(p.stop)
        self.motors = FakeMotors()
        self.r = bridge.Rumble(types.SimpleNamespace(fd=9), self.motors)

    def ioctl(self, fd, req, data=0):
        if req == bridge.UI_BEGIN_FF_UPLOAD:
            etype, eid, length, delay, strong, weak = self.pending
            struct.pack_into("=Hh", data, 8, etype, eid)
            struct.pack_into("=HH", data, 18, length, delay)
            struct.pack_into("=HH", data, 24, strong, weak)
        elif req == bridge.UI_BEGIN_FF_ERASE:
            struct.pack_into("=I", data, 8, self.pending)
        elif req in (bridge.UI_END_FF_UPLOAD, bridge.UI_END_FF_ERASE):
            self.ended.append((req, struct.unpack_from("=Ii", data)))

    def upload(self, eid=3, length=500, delay=0, strong=0xFFFF, weak=0x8000, etype=None, request=7):
        self.pending = (bridge.FF_RUMBLE if etype is None else etype, eid, length, delay, strong, weak)
        self.r.handle_event(bridge.EV_UINPUT, bridge.UI_FF_UPLOAD, request)

    def play(self, eid=3, count=1):
        self.r.last_report = self.now
        self.r.handle_event(bridge.EV_FF, eid, count)
        self.r.update_motors()

    def test_upload_rumble_effect(self):
        self.upload()
        self.assertEqual(self.r.effects, {3: (255, 128, 500, 0)})
        self.assertEqual(self.ended, [(bridge.UI_END_FF_UPLOAD, (7, 0))])

    def test_upload_rejects_other_effect_types(self):
        self.upload(etype=0x51)                               # FF_PERIODIC
        self.assertEqual(self.r.effects, {})
        self.assertEqual(self.ended, [(bridge.UI_END_FF_UPLOAD, (7, -errno.EINVAL))])

    def test_play_until_length_expires(self):
        self.upload(length=500); self.play()
        self.assertEqual(self.motors.sent, [(255, 128)])
        self.now += 0.2; self.r.last_report = self.now; self.r.update_motors()
        self.assertEqual(self.motors.sent, [(255, 128)])      # no repeat while unchanged
        self.now += 0.4; self.r.last_report = self.now; self.r.update_motors()
        self.assertEqual(self.motors.sent, [(255, 128), (0, 0)])

    def test_delay(self):
        self.upload(delay=100); self.play()
        self.assertEqual(self.motors.sent, [])
        self.now += 0.15; self.r.last_report = self.now; self.r.update_motors()
        self.assertEqual(self.motors.sent, [(255, 128)])

    def test_stops_when_input_goes_quiet(self):
        self.upload(length=0); self.play()                    # length 0 = until stopped
        self.now += bridge.RUMBLE_INPUT_TIMEOUT_S + 0.01; self.r.update_motors()
        self.assertEqual(self.motors.sent, [(255, 128), (0, 0)])
        self.assertIn("rumble stopped after input reports went quiet", self.logged())

    def test_stop_event_and_erase(self):
        self.upload(length=0); self.play()
        self.r.handle_event(bridge.EV_FF, 3, 0); self.r.update_motors()
        self.assertEqual(self.motors.sent[-1], (0, 0))
        self.play()
        self.pending = 3; self.r.handle_event(bridge.EV_UINPUT, bridge.UI_FF_ERASE, 8); self.r.update_motors()
        self.assertEqual(self.motors.sent[-1], (0, 0))
        self.assertEqual(self.r.effects, {})
        self.assertEqual(self.ended[-1], (bridge.UI_END_FF_ERASE, (8, 0)))

    def test_detach_stops_motors(self):
        self.upload(length=0); self.play()
        self.r.detach()
        self.assertEqual(self.motors.sent, [(255, 128), (0, 0)])
        self.assertIsNone(self.r.receiver)
        self.play()                                           # no receiver: ignored
        self.assertEqual(self.motors.sent, [(255, 128), (0, 0)])

    def test_rumble_packet(self):
        rx = bridge.Receiver.__new__(bridge.Receiver); rx.output_lock = __import__("threading").Lock(); sent = []
        rx.xfer = lambda ep, data=None, length=32, timeout=500: sent.append((ep, data, timeout))
        rx.rumble(255, 128)
        self.assertEqual(sent, [(bridge.EP_OUT, bytes([0, 8, 0, 255, 128, 0, 0, 0]), 200)])


class MainTest(Base):
    def test_waits_for_new_instance_after_serve(self):
        found = ("/sys/x", 1, 5)
        seq = iter([None, found, found, found, ("/sys/x", 1, 6), ("/sys/x", 1, 6)])
        served = []
        def fake_serve(*a):
            served.append(a[:3])
            if len(served) == 2: bridge.running = False
        with mock.patch.object(bridge, "find_receiver", lambda: next(seq)), \
             mock.patch.object(bridge, "serve", fake_serve), mock.patch.object(bridge.time, "sleep", lambda s: None), \
             mock.patch.object(bridge.signal, "signal"):
            bridge.main()
        self.assertEqual(served, [found, ("/sys/x", 1, 6)])   # same devnum is not served twice
        self.assertIn("bridge stopped", self.logged())

    def test_serve_failure_is_logged(self):
        calls = iter([("/sys/x", 1, 5)])
        def find():
            try: return next(calls)
            except StopIteration: bridge.running = False; return None
        with mock.patch.object(bridge, "find_receiver", find), \
             mock.patch.object(bridge, "serve", side_effect=OSError(errno.EACCES, "denied")), \
             mock.patch.object(bridge.time, "sleep", lambda s: None), mock.patch.object(bridge.signal, "signal"):
            bridge.main()
        self.assertIn("serve failed", self.logged())


if __name__ == "__main__":
    unittest.main()
