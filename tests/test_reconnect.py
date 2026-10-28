"""In-process reconnect: a lost link is re-established inside record().

Captured from the vendor app on firmware 3.1.23 (2026-10-28 HCI snoop): after a
supervision-timeout disconnect it starts reconnecting 0.16 s later and has data
flowing ~2.2 s after the disconnect event. Our recorder instead returned, waited
for a supervisor's 20 s file-growth watchdog, and paid a process restart: a
median 29.9 s per dropout on the night of 2026-10-19, 14.1 min lost over 23 of
them. These tests pin the behaviour that closes that gap and the rails that keep
an absent headset looking the same as before.
"""

import asyncio
import importlib
import os
import tempfile
import time
import unittest
from unittest import mock

from OpenMuse.muse import HandshakeFailed, MuseS
from OpenMuse.record import RecordingStalled, record

from test_record import FakeClient

# `OpenMuse.record` is also the name of the function the package exports, so
# patch the module object itself.
record_module = importlib.import_module("OpenMuse.record")


def _packet(pkt_id: int, data_len: int = 28) -> bytes:
    """Minimal well-formed packet: length byte, header, pkt_id at offset 9."""
    b = bytearray(14 + data_len)
    b[0] = len(b)
    b[9] = pkt_id
    return bytes(b)


EEG_ACC_OPTICS = _packet(0x11) + _packet(0x47) + _packet(0x34)
ACC_OPTICS_ONLY = _packet(0x47) + _packet(0x34)   # the 2026-08-05 latch


class LinkClient(FakeClient):
    """A FakeClient whose link can fail the way real ones do.

    ``plan`` is consumed one entry per connection attempt:
      ("drop", n)    stream n notifications, then BlueZ reports a disconnect
      ("silent", n)  stream n notifications, then stop sending, still "connected"
      ("ok", None)   stream until the recording ends
      ("absent",)    the connect itself fails
      ("no_eeg", None) stream without EEG (the latched configuration)
    """

    plan = []
    attempts = 0

    def __init__(self, address, timeout=None, disconnected_callback=None):
        LinkClient.attempts += 1
        self.step = LinkClient.plan.pop(0) if LinkClient.plan else ("ok", None)
        self.disconnected_callback = disconnected_callback
        super().__init__(address, timeout)

    async def __aenter__(self):
        if self.step[0] == "absent":
            # Not a real instance: the connection never came up.
            FakeClient.instances.remove(self)
            raise OSError("Device with address AA:BB:CC:DD:EE:FF was not found.")
        return self

    async def _pump(self):
        kind, limit = self.step[0], self.step[1]
        payload = ACC_OPTICS_ONLY if kind == "no_eeg" else EEG_ACC_OPTICS
        sent = 0
        while True:
            callback = self._notify.get(MuseS.EEG_UUID)
            if callback is None:
                return
            if limit is not None and sent >= limit:
                if kind == "drop" and self.disconnected_callback is not None:
                    self.disconnected_callback(self)
                return
            callback(0, bytearray(payload))
            sent += 1
            await asyncio.sleep(0.01)


class ReconnectTestCase(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.emit_data = True
        LinkClient.plan = []
        LinkClient.attempts = 0
        self.tmp = tempfile.mkdtemp()
        self.outfile = os.path.join(self.tmp, "rec.txt")

    def _record(self, plan, **kwargs):
        LinkClient.plan = list(plan)
        kwargs.setdefault("address", "AA:BB:CC:DD:EE:FF")
        # Each handshake costs ~1 s of command pacing even without the 2 s L0
        # settle, which low_latency skips; size durations to cover them.
        kwargs.setdefault("duration", 4.0)
        kwargs.setdefault("low_latency", True)
        kwargs.setdefault("outfile", self.outfile)
        kwargs.setdefault("verbose", False)
        kwargs.setdefault("preset", "p1035")
        kwargs.setdefault("stall_timeout", None)
        kwargs.setdefault("battery_interval", None)
        kwargs.setdefault("sensor_timeout", None)
        with mock.patch("bleak.BleakClient", LinkClient), \
                mock.patch.object(record_module, "_RECONNECT_RETRY", 0.05):
            return record(**kwargs)

    def _data_lines(self):
        with open(self.outfile) as fh:
            return [l for l in fh if MuseS.EEG_UUID in l]


class TestReconnectsAfterDisconnect(ReconnectTestCase):
    def test_disconnect_is_recovered_in_process(self):
        results = self._record([("drop", 20), ("ok", None)], reconnect_window=5)
        self.assertEqual(len(FakeClient.instances), 2, "one reconnect expected")
        # Data from both connections lands in the same file.
        self.assertGreater(results[0].notifications, 20)
        self.assertEqual(len(self._data_lines()), results[0].notifications)

    def test_every_connection_gets_the_full_handshake(self):
        """The headset latches its configuration; never assume it survived."""
        self._record([("drop", 20), ("ok", None)], reconnect_window=5)
        for client in FakeClient.instances:
            self.assertIn("p1035", client.commands)
            self.assertIn("dc001", client.commands)
            self.assertLess(client.commands.index("p1035"),
                            client.commands.index("dc001"))

    def test_dropped_link_is_not_torn_down(self):
        """No command can reach a link BlueZ has dropped; trying costs 3 s."""
        self._record([("drop", 20), ("ok", None)], reconnect_window=5)
        dropped = FakeClient.instances[0].commands
        self.assertNotIn("h", dropped[dropped.index("dc001"):])
        # The surviving connection is still halted the vendor way at the end.
        self.assertEqual(FakeClient.instances[1].commands[-2:], ["L1", "h"])

    def test_reconnect_is_fast(self):
        """The whole point: no supervisor round trip between connections."""
        LinkClient.plan = [("drop", 5), ("ok", None)]
        started = time.monotonic()
        self._record([("drop", 5), ("ok", None)], reconnect_window=5, duration=3.5)
        self.assertLess(time.monotonic() - started, 6.0)

    def test_several_drops_in_one_recording(self):
        plan = [("drop", 10), ("drop", 10), ("drop", 10), ("ok", None)]
        self._record(plan, reconnect_window=5, duration=8.0)
        self.assertEqual(len(FakeClient.instances), 4)


class TestSilentLink(ReconnectTestCase):
    def test_silent_connection_is_dropped_and_reconnected(self):
        """BlueZ may keep a dead link 'up' for its whole supervision timeout."""
        self._record([("silent", 20), ("ok", None)], reconnect_window=5,
                     link_silence=0.3, duration=4.0)
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertGreater(len(self._data_lines()), 20)

    def test_silent_link_gets_a_short_teardown(self):
        """It is probably alive enough to take L1, h - but bounded tightly."""
        self._record([("silent", 20), ("ok", None)], reconnect_window=5,
                     link_silence=0.3, duration=4.0)
        silent = FakeClient.instances[0].commands
        self.assertEqual(silent[-2:], ["L1", "h"])


class TestRailsAreUnchanged(ReconnectTestCase):
    def test_absent_headset_still_raises_on_first_connect(self):
        """Exit 2 from muse_record.py drives the supervisor's absent backoff."""
        with self.assertRaises(OSError):
            self._record([("absent",)], reconnect_window=5)

    def test_reconnect_gives_up_after_the_window(self):
        plan = [("drop", 10)] + [("absent",)] * 200
        started = time.monotonic()
        with self.assertRaises(RecordingStalled) as ctx:
            self._record(plan, reconnect_window=0.5, duration=30)
        self.assertLess(time.monotonic() - started, 8.0,
                        "must give up near the window, not at the duration")
        self.assertIn("not recovered within", str(ctx.exception))

    def test_disabled_reconnect_raises_at_once(self):
        """reconnect_window=None keeps a single-connection recording, but a
        disconnect is now reported immediately instead of after stall_timeout."""
        started = time.monotonic()
        with self.assertRaises(RecordingStalled):
            self._record([("drop", 10), ("ok", None)], reconnect_window=None,
                         duration=30, stall_timeout=120)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertEqual(len(FakeClient.instances), 1)

    def test_silence_without_reconnect_is_left_to_stall_timeout(self):
        """link_silence only acts when reconnecting is on."""
        with self.assertRaises(RecordingStalled):
            self._record([("silent", 10)], reconnect_window=None,
                         link_silence=0.2, stall_timeout=0.6, duration=30)
        self.assertEqual(len(FakeClient.instances), 1)


class TestWatchdogIsRearmed(ReconnectTestCase):
    def test_eeg_missing_after_reconnect_is_caught(self):
        """The EEG-less latch has only ever been seen after a reconnect."""
        with self.assertRaises(HandshakeFailed) as ctx:
            self._record([("drop", 10), ("no_eeg", None)], reconnect_window=5,
                         sensor_timeout=0.4, duration=30)
        self.assertIn("EEG", str(ctx.exception))
        self.assertEqual(len(FakeClient.instances), 2)


if __name__ == "__main__":
    unittest.main()
