"""Multi-receiver support (one 2.4 GHz receiver per controller).

Design:
- find_receivers() returns every matching receiver as (syspath, bus, dev), sorted by syspath.
- led_packet(player) returns the Xbox 360 set-LED packet for player 1-4 (01 03 02..05).
- Receiver(bus, dev, player=1) sends that player's LED packet in handshake().
- main() keeps one PadSession per player slot (1..MAX_PLAYERS) and serves each receiver in its own
  thread with serve(syspath, bus, dev, session); the session's player picks the LED packet.
- A new receiver gets the slot already holding its pad (same serial, or same port), else the lowest
  empty slot, else the held pad closest to expiry. Extra receivers wait for a free slot.
- A receiver leaving or failing does not disturb the others, and on stop main() waits for every
  serve() to return and closes every pad.
"""
import errno, os, threading, time, unittest
from unittest import mock

from test_bridge import Base, FakeReceiver, FakeUinput, FindReceiver, ReceiverTest, ServeTest, bridge, report, write


_monotonic, _sleep = time.monotonic, time.sleep   # real clock: some tests patch the time module


def wait_until(cond, timeout=2.0):
    end = _monotonic() + timeout
    while _monotonic() < end:
        if cond(): return True
        _sleep(0.005)
    return cond()


class FindReceivers(FindReceiver):
    def find_all(self):
        with mock.patch.object(bridge.glob, "glob", lambda pat: [
                os.path.join(self.tmp.name, n) for n in reversed(sorted(os.listdir(self.tmp.name))) if not n.endswith(".log")]):
            return bridge.find_receivers()

    def test_returns_all_matching_sorted(self):
        b = self.make_dev("1-2", busnum=1, devnum=8)
        self.make_dev("1-3", pid="3106")
        a = self.make_dev("1-1", busnum=1, devnum=7)
        c = self.make_dev("2-1", busnum=2, devnum=3)
        self.assertEqual(self.find_all(), [(a, 1, 7), (b, 1, 8), (c, 2, 3)])


class LedPacket(unittest.TestCase):
    def test_players(self):
        self.assertEqual([bridge.led_packet(p) for p in (1, 2, 3, 4)],
                         [b"\x01\x03\x02", b"\x01\x03\x03", b"\x01\x03\x04", b"\x01\x03\x05"])

    def test_player_one_matches_known_working_packet(self):
        self.assertEqual(bridge.led_packet(1), bridge.LED_PACKET)

    def test_out_of_range(self):
        for p in (0, 5):
            with self.subTest(player=p), self.assertRaises(ValueError): bridge.led_packet(p)


class ReceiverPlayer(ReceiverTest):
    def test_default_player_is_one(self):
        bridge.Receiver(1, 5).handshake()
        self.assertEqual(self.calls[-1][:3], ("bulk_out", bridge.EP_OUT, b"\x01\x03\x02"))

    def test_handshake_uses_player_led(self):
        bridge.Receiver(1, 5, player=3).handshake()
        self.assertEqual(self.calls[-1][:3], ("bulk_out", bridge.EP_OUT, b"\x01\x03\x04"))


class ServePlayer(ServeTest):
    def test_session_player_passed_to_receiver(self):
        rx = FakeReceiver(self, [report()]); ui = FakeUinput()
        with mock.patch.object(bridge, "Receiver", rx), mock.patch.object(bridge, "Uinput", lambda *a, **k: ui):
            bridge.serve(self.sys, 1, 5, bridge.PadSession(False, player=2))
        self.assertEqual(rx.player, 2)
        self.assertIn("as player 2", self.logged())


class PickSession(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(bridge, "receiver_serial", lambda syspath: {"/sys/a": "A", "/sys/b": "B"}.get(syspath))
        p.start(); self.addCleanup(p.stop)

    def slot(self, player, serial=None, port=None, remove_at=None):
        s = bridge.PadSession(False, player)
        if serial or port: s.pad, s.serial, s.port, s.remove_at = object(), serial, port, remove_at
        return s

    def test_prefers_matching_held_pad(self):
        empty, held = self.slot(1), self.slot(2, serial="A", remove_at=5.0)
        self.assertIs(bridge.pick_session("/sys/a", [empty, held]), held)

    def test_matches_by_port_without_serial(self):
        empty, held = self.slot(1), self.slot(2, port="c", remove_at=5.0)
        self.assertIs(bridge.pick_session("/sys/c", [empty, held]), held)

    def test_otherwise_lowest_empty_slot(self):
        held, e3, e4 = self.slot(1, serial="A", remove_at=5.0), self.slot(3), self.slot(4)
        self.assertIs(bridge.pick_session("/sys/b", [held, e3, e4]), e3)

    def test_takes_held_pad_nearest_expiry_when_no_empty_slot(self):
        late, soon = self.slot(1, serial="A", remove_at=9.0), self.slot(2, port="x", remove_at=3.0)
        self.assertIs(bridge.pick_session("/sys/b", [late, soon]), soon)

    def test_none_when_nothing_idle(self):
        self.assertIsNone(bridge.pick_session("/sys/b", []))


class FakeServe:
    """Stands in for serve(): runs the real session prepare/detach, then blocks until released or stopped."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active, self.history, self.release, self.fail = {}, [], {}, set()
        self.linger = 0.0    # seconds serve() keeps running after the bridge is told to stop

    def __call__(self, syspath, bus, dev, session):
        key = (syspath, bus, dev); ev = threading.Event()
        session.prepare(syspath, 0x114); session.attach(object())
        with self.lock:
            self.active[key] = session.player; self.history.append((key, session.player, session.pad)); self.release[key] = ev
        try:
            if key in self.fail: raise OSError(errno.EBUSY, "busy")
            while bridge.running and not ev.wait(0.005): pass
            if not bridge.running: _sleep(self.linger)
        finally:
            session.detach()
            with self.lock: del self.active[key]

    def players(self):
        with self.lock: return dict(self.active)


class MainMulti(Base):
    def setUp(self):
        super().setUp()
        self.A, self.B, self.C = (self.dev("1-1", "SA"), 1, 5), (self.dev("1-2", "SB"), 1, 6), (self.dev("1-3", "SC"), 1, 7)
        self.present, self.serve, self.pads = [], FakeServe(), []
        def make(*a, **k): pad = FakeUinput(*a, **k); self.pads.append(pad); return pad
        for target, attr, val in ((bridge, "find_receivers", lambda: list(self.present)),
                                  (bridge, "serve", self.serve), (bridge, "Uinput", make),
                                  (bridge, "RUMBLE_FLAG", os.path.join(self.tmp.name, "no-rumble")),
                                  (bridge.signal, "signal", lambda *a: None),
                                  (bridge.time, "sleep", lambda s: _sleep(0.005))):
            p = mock.patch.object(target, attr, val); p.start(); self.addCleanup(p.stop)
        self.thread = threading.Thread(target=bridge.main, daemon=True)
        self.addCleanup(self.stop)

    def dev(self, port, serial=None):
        d = os.path.join(self.tmp.name, port); os.makedirs(d, exist_ok=True)
        if serial: write(os.path.join(d, "serial"), serial + "\n")
        return d

    def start(self): self.thread.start()

    def stop(self):
        bridge.running = False
        if self.thread.is_alive(): self.thread.join(2.0)

    def unplug(self, dev):
        self.present.remove(dev); self.serve.release[dev].set()
        self.assertTrue(wait_until(lambda: dev not in self.serve.players()))

    def test_two_receivers_served_concurrently(self):
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2), self.serve.history)
        self.assertEqual(self.serve.players(), {self.A: 1, self.B: 2})
        self.assertEqual(len(self.pads), 2)

    def test_hotplugged_receiver_gets_next_player(self):
        self.present[:] = [self.A]; self.start()
        self.assertTrue(wait_until(lambda: self.serve.players() == {self.A: 1}))
        self.present.append(self.B)
        self.assertTrue(wait_until(lambda: self.serve.players() == {self.A: 1, self.B: 2}), self.serve.players())

    def test_reconnect_keeps_player_and_pad(self):
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2))
        self.unplug(self.A)
        self.present.append(self.C)                          # a different receiver arrives meanwhile
        self.assertTrue(wait_until(lambda: self.serve.players().get(self.C) == 3), self.serve.players())
        a2 = (self.dev("1-4", "SA"), 1, 9)                   # receiver A again, on another port
        self.present.append(a2)
        self.assertTrue(wait_until(lambda: self.serve.players().get(a2) == 1), self.serve.players())
        pads = {key: pad for key, _, pad in self.serve.history}
        self.assertIs(pads[a2], pads[self.A])
        self.assertEqual(len(self.pads), 3)

    def test_reenumeration_waits_for_old_thread(self):
        self.present[:] = [self.A]; self.start()
        self.assertTrue(wait_until(lambda: self.serve.players() == {self.A: 1}))
        # The old instance vanishes and a new one (new devnum, same serial) appears before serve() returns.
        a2 = (self.A[0], 1, 9); self.present[:] = [a2]
        _sleep(0.1)
        self.assertEqual(self.serve.players(), {self.A: 1})  # not given a second slot
        self.serve.release[self.A].set()
        self.assertTrue(wait_until(lambda: self.serve.players() == {a2: 1}), self.serve.players())
        self.assertEqual(len(self.pads), 1)

    def test_new_receiver_skips_held_slot(self):
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2))
        self.unplug(self.A)                                  # slot 1 holds A's pad for the grace period
        self.present.append(self.C)
        self.assertTrue(wait_until(lambda: self.serve.players() == {self.B: 2, self.C: 3}), self.serve.players())

    def test_held_slot_freed_after_grace(self):
        now = [1000.0]
        with mock.patch.object(bridge.time, "monotonic", lambda: now[0]):
            self.present[:] = [self.A]; self.start()
            self.assertTrue(wait_until(lambda: self.serve.players() == {self.A: 1}))
            self.unplug(self.A)
            now[0] += bridge.RECONNECT_GRACE_S + 1
            self.assertTrue(wait_until(lambda: self.pads[0].closed))
            self.present.append(self.B)
            self.assertTrue(wait_until(lambda: self.serve.players() == {self.B: 1}), self.serve.players())

    def test_failed_receiver_does_not_affect_others(self):
        self.serve.fail.add(self.A)
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: self.B in self.serve.players()))
        self.assertTrue(wait_until(lambda: "serve failed" in self.logged()))
        self.assertIn(self.B, self.serve.players())

    def test_instance_not_reserved_while_still_present(self):
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2))
        self.serve.release[self.A].set()                     # serve returned (e.g. IDLE) but device still listed
        self.assertTrue(wait_until(lambda: self.A not in self.serve.players()))
        _sleep(0.1)
        self.assertEqual([k for k, _, _ in self.serve.history].count(self.A), 1)
        self.present.remove(self.A); a2 = (self.A[0], 1, 9); self.present.append(a2)   # re-enumerated
        self.assertTrue(wait_until(lambda: self.serve.players().get(a2) == 1), self.serve.players())

    def test_fifth_receiver_waits_for_free_slot(self):
        devs = [(self.dev("2-%d" % i, "S%d" % i), 2, 10 + i) for i in range(1, 6)]
        self.present[:] = devs; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 4), self.serve.players())
        _sleep(0.1)
        self.assertEqual(self.serve.players(), {d: i + 1 for i, d in enumerate(devs[:4])})
        self.assertEqual(self.logged().count("waiting: all 4 player slots in use"), 1)   # logged once, not every scan
        self.unplug(devs[1])                                 # slot 2 is held for devs[1], but nothing else is free
        self.assertTrue(wait_until(lambda: self.serve.players().get(devs[4]) == 2), self.serve.players())

    def test_stop_waits_for_all_serves_and_closes_pads(self):
        self.serve.linger = 0.2
        self.present[:] = [self.A, self.B]; self.start()
        self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2))
        bridge.running = False; self.thread.join(2.0)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.serve.players(), {})
        self.assertTrue(all(pad.closed for pad in self.pads))
        self.assertTrue(self.logged().rstrip().endswith("bridge stopped"))

    def test_rumble_flag_enables_rumble_on_every_slot(self):
        write(bridge.RUMBLE_FLAG, "")
        with mock.patch.object(bridge, "Rumble") as worker:
            self.present[:] = [self.A, self.B]; self.start()
            self.assertTrue(wait_until(lambda: len(self.serve.players()) == 2))
        self.assertTrue(all(pad.rumble for pad in self.pads))
        self.assertEqual(worker.call_count, 2)


def load_tests(loader, tests, pattern):
    """Run only the tests defined here, not the ones inherited from test_bridge's classes."""
    suite = unittest.TestSuite()
    for cls in (FindReceivers, LedPacket, ReceiverPlayer, ServePlayer, PickSession, MainMulti):
        suite.addTests(cls(n) for n in loader.getTestCaseNames(cls) if n in vars(cls))
    return suite


if __name__ == "__main__":
    unittest.main()
