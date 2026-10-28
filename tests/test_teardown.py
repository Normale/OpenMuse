"""The session must be torn down with ``L1, h``, including when killed.

The headset latches its streaming configuration. A session that ends without a
halt it can service is the leading suspect for the state where it reconnects
happily, reports ``rc: 0``, and then streams ACC/OPTICS with no EEG until it is
power-cycled -- observed 2026-08-05, -06, -08 and -10, the last costing seven
hours of a night across 343 restarts.

Two defects are covered:

* ``stop_streaming`` sent a bare ``h``. The vendor app sends ``L1, h``: with
  ``L0`` in force the connection interval is long, and the halt -- the one
  command that has to land -- was being written into it.

* The teardown never ran on the path that matters. Python's default disposition
  for SIGTERM ends the process immediately, so the ``finally`` was skipped
  whenever a supervisor killed the recorder, which is what happens on *every*
  dropout detected by a file-growth watchdog. The ``except CancelledError`` arm
  had been written for this and never received one.
"""

import asyncio
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest import mock

import pytest

from OpenMuse.muse import MuseS
from OpenMuse.record import _TEARDOWN_TIMEOUT, _record_async, record

from test_record import FakeClient, RecordTestCase

POSIX = os.name == "posix"


def _teardown_tail(commands):
    """The commands issued after streaming started, i.e. the teardown."""
    return commands[commands.index("dc001") + 1 :]


class TestTeardownOrder(RecordTestCase):
    def test_sends_l1_then_h(self):
        self._record(stall_timeout=None, battery_interval=None)
        tail = _teardown_tail(FakeClient.instances[0].commands)
        self.assertEqual(
            tail[-2:],
            ["L1", "h"],
            f"vendor teardown is L1 then h; got {tail!r}",
        )

    def test_halt_is_the_last_word(self):
        """Nothing may follow the halt -- it is what releases the config."""
        self._record(stall_timeout=None, battery_interval=None)
        commands = FakeClient.instances[0].commands
        self.assertEqual(commands[-1], "h")

    def test_l1_reverses_the_l0_from_the_handshake(self):
        """Recording stands low latency down with L0; teardown must undo it."""
        commands = self._record(stall_timeout=None, battery_interval=None) and \
            FakeClient.instances[0].commands
        self.assertIn("L0", commands)
        self.assertGreater(
            len(commands) - 1 - commands[::-1].index("L1"),
            commands.index("L0"),
            "the teardown L1 must come after the handshake's L0",
        )


class TestCancellationRunsTeardown(unittest.TestCase):
    """A cancelled recording still halts the headset.

    This is the mechanism a signal reaches; see TestSigtermRunsTeardown for the
    signal itself, which only exists on POSIX.
    """

    def setUp(self):
        FakeClient.instances = []
        FakeClient.emit_data = True
        self.tmp = tempfile.mkdtemp()
        self.outfile = os.path.join(self.tmp, "rec.txt")

    def _run_and_cancel(self, wait_for=None):
        """Start a recording, cancel it once ``wait_for`` has been written."""

        async def main():
            task = asyncio.ensure_future(
                _record_async(
                    address="AA:BB:CC:DD:EE:FF",
                    duration=30.0,          # far longer than the test needs
                    outfile=self.outfile,
                    preset="p1035",
                    verbose=False,
                    stall_timeout=None,
                    battery_interval=None,
                )
            )
            deadline = time.monotonic() + 10.0
            while wait_for is not None and time.monotonic() < deadline:
                issued = FakeClient.instances[0].commands if FakeClient.instances else []
                if wait_for in issued:
                    break
                await asyncio.sleep(0.02)
            else:
                await asyncio.sleep(0.05)
            task.cancel()
            return await task

        with mock.patch("bleak.BleakClient", FakeClient):
            return asyncio.run(main())

    def test_cancel_mid_recording_still_halts(self):
        self._run_and_cancel(wait_for="dc001")
        tail = _teardown_tail(FakeClient.instances[0].commands)
        self.assertEqual(
            tail[-2:], ["L1", "h"], "a cancelled session must still be halted"
        )

    def test_cancel_during_handshake_still_halts(self):
        """The connect-timeout kill lands here: preset sent, nothing flowing.

        MUSE_CONNECT_TIMEOUT gives 90 s for data to appear and then kills. By
        then the preset and dc001 have long since gone out, so the headset is
        configured with nobody attached -- the state this whole change is about.
        """
        self._run_and_cancel(wait_for="p1035")
        commands = FakeClient.instances[0].commands
        self.assertEqual(
            commands[-2:], ["L1", "h"], f"expected a halt; got {commands!r}"
        )

    def test_cancel_does_not_propagate_as_a_crash(self):
        """The caller gets a result, not a CancelledError escaping the stack."""
        result = self._run_and_cancel(wait_for="dc001")
        self.assertIsNotNone(result)


@pytest.mark.skipif(not POSIX, reason="add_signal_handler is POSIX-only")
class TestSigtermRunsTeardown(RecordTestCase):
    """The real signal, on the real entry point.

    Skipped on Windows, where asyncio cannot install a signal handler and the
    default disposition stands. The Pi is the platform that matters here.
    """

    def _sigterm_after(self, token, timeout=10.0):
        """Fire SIGTERM at ourselves once ``token`` has gone out on the wire.

        Timing it by wall clock instead makes the test depend on how fast the
        handshake runs, which differs enough between a desktop and the Pi to
        land in a different phase -- that is how the first version of this test
        managed to pass while asserting the wrong thing.
        """

        def fire():
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                issued = FakeClient.instances[0].commands if FakeClient.instances else []
                if token is None or token in issued:
                    break
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=fire, daemon=True)
        previous = signal.getsignal(signal.SIGTERM)
        thread.start()
        try:
            self._record(duration=30.0, stall_timeout=None, battery_interval=None)
        finally:
            thread.join(timeout=timeout + 2)
            signal.signal(signal.SIGTERM, previous)
        return FakeClient.instances[0].commands

    def test_sigterm_mid_stream_halts_before_exit(self):
        commands = self._sigterm_after("dc001")
        self.assertEqual(
            commands[-2:],
            ["L1", "h"],
            "SIGTERM must unwind through the teardown, not kill us where we stand",
        )
        self.assertIn("dc001", commands, "the stream should have started first")

    def test_sigterm_during_handshake_halts_before_exit(self):
        """Observed on the Pi: the signal can beat the preset out of the door."""
        commands = self._sigterm_after("v6")
        self.assertEqual(commands[-2:], ["L1", "h"], f"got {commands!r}")

    def test_sigterm_returns_promptly(self):
        """It must not sit out the remaining duration after being signalled."""
        started = time.monotonic()
        self._sigterm_after("dc001")
        self.assertLess(time.monotonic() - started, 15.0)


class TestTeardownIsBounded(RecordTestCase):
    """A dead link makes a GATT write hang, not fail.

    The teardown runs inside a supervisor's patience -- record_session.sh gives
    it MUSE_TEARDOWN_GRACE seconds and then SIGKILLs. If a hung write could
    block forever, the change would trade a latched headset for a wedged unit.
    """

    def test_hung_teardown_does_not_block_forever(self):
        class HangingClient(FakeClient):
            async def write_gatt_char(self, uuid, data, response=False):
                token = bytes(data)[1:].decode("ascii").strip()
                if token in ("L1", "h") and "dc001" in self.commands:
                    self.commands.append(token)
                    await asyncio.sleep(3600)   # the link is gone
                    return
                return await FakeClient.write_gatt_char(self, uuid, data, response)

        started = time.monotonic()
        with mock.patch("bleak.BleakClient", HangingClient):
            record(
                address="AA:BB:CC:DD:EE:FF",
                duration=0.4,
                outfile=self.outfile,
                verbose=False,
                stall_timeout=None,
                battery_interval=None,
            )
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            _TEARDOWN_TIMEOUT + 5.0,
            f"teardown was not bounded: took {elapsed:.1f}s",
        )


if __name__ == "__main__":
    unittest.main()
