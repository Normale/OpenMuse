"""record() behaviour: stall detection, L1 opt-in, periodic battery.

Two defects covered here, both of which cost whole unattended nights:

* When the BLE link dropped, ``_record_async`` kept awaiting notifications that
  would never arrive. It wrote nothing, raised nothing, and returned success
  when the requested duration elapsed -- so a supervisor saw a clean run and
  never restarted it. With the default 12 h duration that is an entire night.

* The handshake unconditionally sent ``L1`` (low-latency mode), asking the
  headset for a short BLE connection interval. The radio is one of the largest
  consumers on the device and the interval sets its duty cycle. Real-time use
  wants that; recording does not -- the whole payload of p1041 is ~6.7 KB/s,
  which fits comfortably in a 50 ms interval.
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from OpenMuse.muse import MuseS
from OpenMuse.record import RecordingStalled, record

STATUS_REPLY = {"bp": 77.5, "ps": 4020, "sn": "7011-AH29-1E2B", "rc": 0}
VERSION_REPLY = {"fw": "3.1.23", "hw": "01.1", "sp": "Athena_RevE", "rc": 0}


def _wire(reply):
    return json.dumps(reply, separators=(",", ":")).encode() + b"\x00" * 5 + b"c"


def _fragments(payload, size=20):
    body = size - 1
    return [
        bytes([len(payload[i : i + body])]) + payload[i : i + body]
        for i in range(0, len(payload), body)
    ]


class FakeClient:
    """Minimal stand-in for bleak.BleakClient.

    ``emit_data`` controls the only thing that matters here: whether sensor
    notifications actually arrive. A live link and a dead one are otherwise
    indistinguishable from inside record().
    """

    emit_data = True
    instances = []

    def __init__(self, address, timeout=None):
        self.address = address
        self.name = "FakeMuse"
        self.commands = []
        self._notify = {}
        self._task = None
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if self._task is not None:
            self._task.cancel()
        return False

    async def start_notify(self, uuid, callback):
        self._notify[uuid] = callback

    async def stop_notify(self, uuid):
        self._notify.pop(uuid, None)

    def _reply(self, reply):
        callback = self._notify.get(MuseS.CONTROL_UUID)
        if callback is None:
            return
        for fragment in _fragments(_wire(reply)):
            callback(0, bytearray(fragment))

    async def _pump(self):
        callback = self._notify.get(MuseS.EEG_UUID)
        while callback is not None:
            callback(0, bytearray(b"\x10\x01" + b"\x00" * 30))
            await asyncio.sleep(0.01)

    async def write_gatt_char(self, uuid, data, response=False):
        token = bytes(data)[1:].decode("ascii").strip()
        self.commands.append(token)
        if token == "v6":
            self._reply(VERSION_REPLY)
        elif token == "s":
            self._reply(STATUS_REPLY)
        elif token == "dc001" and self.emit_data and self._task is None:
            self._task = asyncio.get_event_loop().create_task(self._pump())


class RecordTestCase(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.emit_data = True
        self.tmp = tempfile.mkdtemp()
        self.outfile = os.path.join(self.tmp, "rec.txt")

    def _record(self, **kwargs):
        kwargs.setdefault("address", "AA:BB:CC:DD:EE:FF")
        kwargs.setdefault("duration", 0.4)
        kwargs.setdefault("outfile", self.outfile)
        kwargs.setdefault("verbose", False)
        with mock.patch("bleak.BleakClient", FakeClient):
            return record(**kwargs)


class TestLowLatencyIsOptIn(RecordTestCase):
    def test_l1_not_sent_by_default(self):
        self._record(stall_timeout=None, battery_interval=None)
        commands = FakeClient.instances[0].commands
        self.assertNotIn(
            "L1",
            commands,
            "recording must not request low-latency mode: it costs battery for "
            "throughput nobody asked for",
        )
        self.assertIn("dc001", commands, "streaming should still be started")

    def test_l1_sent_when_requested(self):
        self._record(low_latency=True, stall_timeout=None, battery_interval=None)
        self.assertIn("L1", FakeClient.instances[0].commands)


class TestStallDetection(RecordTestCase):
    def test_dead_link_raises_instead_of_reporting_success(self):
        FakeClient.emit_data = False
        with self.assertRaises(RecordingStalled):
            self._record(duration=30.0, stall_timeout=0.3, battery_interval=None)

    def test_live_link_is_not_mistaken_for_a_stall(self):
        results = self._record(duration=0.4, stall_timeout=5.0, battery_interval=None)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].stalled)
        self.assertGreater(results[0].notifications, 0)

    def test_stall_detection_can_be_disabled(self):
        FakeClient.emit_data = False
        results = self._record(duration=0.3, stall_timeout=None, battery_interval=None)
        self.assertEqual(results[0].notifications, 0)

    def test_result_reports_yield(self):
        results = self._record(stall_timeout=None, battery_interval=None)
        result = results[0]
        self.assertGreater(result.bytes_written, 0)
        self.assertEqual(result.outfile, self.outfile)
        self.assertIn("RecordingResult", repr(result))


class TestPeriodicBattery(RecordTestCase):
    def test_status_replies_are_written_to_the_raw_file(self):
        self._record(duration=0.6, stall_timeout=None, battery_interval=0.1)
        with open(self.outfile, "r", encoding="utf-8") as fh:
            lines = [line for line in fh if MuseS.CONTROL_UUID in line]
        self.assertGreater(
            len(lines),
            0,
            "status replies must be persisted, otherwise no drain curve exists",
        )

    def test_status_lines_decode_into_battery_rows(self):
        from OpenMuse.decode import decode_rawdata

        self._record(duration=0.6, stall_timeout=None, battery_interval=0.1)
        with open(self.outfile, "r", encoding="utf-8") as fh:
            messages = [line.strip() for line in fh if line.strip()]

        frames, info = decode_rawdata(messages, return_info=True)
        self.assertGreater(info["n_status_replies"], 0)
        battery = frames["BATTERY"]
        status_rows = battery[battery["source"] == "status"]
        self.assertGreater(len(status_rows), 0)
        self.assertAlmostEqual(float(status_rows["battery_percent"].iloc[0]), 77.5)
        self.assertAlmostEqual(float(status_rows["battery_millivolts"].iloc[0]), 4020.0)

    def test_can_be_disabled(self):
        self._record(duration=0.3, stall_timeout=None, battery_interval=None)
        with open(self.outfile, "r", encoding="utf-8") as fh:
            body = fh.read()
        # The handshake still queries status once; nothing periodic follows.
        self.assertLessEqual(body.count(MuseS.CONTROL_UUID), 40)


class TestSensorSelection(RecordTestCase):
    def test_sensors_resolve_to_a_preset(self):
        self._record(
            sensors=["EEG", "ACCGYRO"], stall_timeout=None, battery_interval=None
        )
        self.assertIn("p21", FakeClient.instances[0].commands)

    def test_preset_and_sensors_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self._record(preset="p1041", sensors=["EEG"])

    def test_default_preset_unchanged(self):
        self._record(stall_timeout=None, battery_interval=None)
        self.assertIn("p1041", FakeClient.instances[0].commands)


if __name__ == "__main__":
    unittest.main()
