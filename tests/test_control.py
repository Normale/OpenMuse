"""Control-channel status replies (issue #28).

The device reports its own battery level in response to the "s" command, and
``record()``/``stream()`` already sent that command -- the reply was simply
discarded, while the BATTERY stream instead carried a guess at undocumented
packet bytes that produced impossible values (93, 150, 108, 89, 229, 111, 228,
168, 66, 0 across five archived sessions; a percentage cannot exceed 100).

Three properties of the wire format, all verified on firmware 3.1.23, make
naive parsing fail, and they are what these tests pin:

  1. Replies arrive as 20-byte fragments that must be concatenated; each
     notification is length-prefixed.
  2. Replies are NUL-padded and separated by a checksum byte, so there is no
     clean '}{' to split on -- splitting that way produces invalid JSON.
  3. A single reply may straddle any number of fragments.
"""

import json
import unittest

from OpenMuse.decode import (
    ControlChannel,
    _scan_json_objects,
    decode_rawdata,
    is_control_uuid,
)

CONTROL_UUID = "273e0001-4c4d-454d-96be-f03bac821358"

# Verbatim replies from firmware 3.1.23 / hw 01.1 / Athena_RevE.
STATUS_REPLY = {
    "hn": "MuseS-1E2B",
    "sn": "7011-AH29-1E2B",
    "ma": "00-55-da-ba-1e-2b",
    "hs": "DC23-TVDT-1426",
    "id": "0",
    "bp": 89.11,
    "ps": 4161,
    "ln": "0",
    "ch": [0, 0],
    "rc": 0,
}
VERSION_REPLY = {"fw": "3.1.23", "hw": "01.1", "sp": "Athena_RevE", "rc": 0}


def wire(*replies):
    """Serialise replies the way the device does: NUL padding + a checksum byte."""
    out = b""
    for i, reply in enumerate(replies):
        out += json.dumps(reply, separators=(",", ":")).encode()
        out += b"\x00" * 5
        out += bytes([ord("c") + i])  # stand-in for the checksum byte
    return out


def fragments(payload, size=20):
    """Split into length-prefixed notifications, as BLE delivers them."""
    body = size - 1
    return [
        bytes([len(payload[i : i + body])]) + payload[i : i + body]
        for i in range(0, len(payload), body)
    ]


def control_lines(payload, start="2026-01-01T00:00:00+00:00"):
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(start)
    return [
        f"{(t0 + timedelta(seconds=i)).isoformat()}\t{CONTROL_UUID}\t{frag.hex()}"
        for i, frag in enumerate(fragments(payload))
    ]


class TestBraceMatching(unittest.TestCase):
    def test_no_clean_split_point_exists(self):
        """The naive '}{' split cannot work on the real wire format."""
        payload = wire(STATUS_REPLY, VERSION_REPLY).decode("utf-8", errors="ignore")
        self.assertNotIn("}{", payload)
        naive = payload.split("}{")
        with self.assertRaises(ValueError):
            json.loads(naive[0])

    def test_brace_matching_recovers_both_objects(self):
        payload = wire(STATUS_REPLY, VERSION_REPLY).decode("utf-8", errors="ignore")
        objects, _ = _scan_json_objects(payload)
        self.assertEqual(len(objects), 2)
        self.assertEqual(json.loads(objects[0]), STATUS_REPLY)
        self.assertEqual(json.loads(objects[1]), VERSION_REPLY)

    def test_partial_object_is_held_back(self):
        text = '{"a": 1}{"b": '
        objects, consumed = _scan_json_objects(text)
        self.assertEqual(len(objects), 1)
        self.assertEqual(text[consumed:], '{"b": ')

    def test_braces_inside_strings_do_not_confuse_it(self):
        text = '{"name": "a}b{c", "v": 2}'
        objects, _ = _scan_json_objects(text)
        self.assertEqual(len(objects), 1)
        self.assertEqual(json.loads(objects[0])["name"], "a}b{c")


class TestControlChannel(unittest.TestCase):
    def test_reassembles_fragmented_reply(self):
        channel = ControlChannel()
        payload = wire(VERSION_REPLY, STATUS_REPLY)
        frags = fragments(payload)
        self.assertGreater(len(frags), 2, "test should exercise reassembly")

        seen = []
        for frag in frags:
            seen.extend(channel.feed(frag))

        self.assertEqual(len(seen), 2)
        self.assertEqual(channel.battery_percent, 89.11)
        self.assertEqual(channel.battery_millivolts, 4161)
        self.assertEqual(channel.firmware, "3.1.23")

    def test_state_merges_across_replies(self):
        channel = ControlChannel()
        for frag in fragments(wire(VERSION_REPLY)):
            channel.feed(frag)
        for frag in fragments(wire(STATUS_REPLY)):
            channel.feed(frag)
        self.assertEqual(channel.state["fw"], "3.1.23")
        self.assertEqual(channel.state["sn"], "7011-AH29-1E2B")
        self.assertEqual(channel.state["bp"], 89.11)

    def test_garbage_does_not_accumulate(self):
        channel = ControlChannel()
        for _ in range(2000):
            channel.feed(b"\x13" + b"garbage padding...."[:19])
        self.assertEqual(channel.replies, [])
        self.assertLessEqual(len(channel._buffer), channel.MAX_BUFFER)

    def test_uuid_recognition(self):
        self.assertTrue(is_control_uuid(CONTROL_UUID))
        self.assertTrue(is_control_uuid(CONTROL_UUID.upper()))
        self.assertFalse(is_control_uuid("273e0013-4c4d-454d-96be-f03bac821358"))
        self.assertFalse(is_control_uuid(None))


class TestStatusBatteryInDecode(unittest.TestCase):
    def test_status_lines_become_battery_rows(self):
        messages = control_lines(wire(VERSION_REPLY, STATUS_REPLY))
        frames, info = decode_rawdata(messages, return_info=True)

        self.assertEqual(info["n_status_replies"], 1)
        battery = frames["BATTERY"]
        self.assertEqual(len(battery), 1)
        self.assertAlmostEqual(float(battery["battery_percent"].iloc[0]), 89.11)
        self.assertAlmostEqual(float(battery["battery_millivolts"].iloc[0]), 4161.0)
        self.assertEqual(battery["source"].iloc[0], "status")

    def test_status_lines_are_not_parsed_as_sensor_packets(self):
        """Control notifications are JSON, not packets: never decode them as such."""
        messages = control_lines(wire(STATUS_REPLY))
        frames = decode_rawdata(messages)
        for stream in ("EEG", "OPTICS", "ACCGYRO", "Unknown"):
            self.assertEqual(
                len(frames[stream]), 0, f"{stream} manufactured from a status reply"
            )

    def test_absolute_timebase_gives_a_drain_curve(self):
        payload = wire(STATUS_REPLY)
        low = dict(STATUS_REPLY)
        low["bp"] = 42.5
        messages = control_lines(payload, "2026-01-01T00:00:00+00:00")
        messages += control_lines(wire(low), "2026-01-01T02:00:00+00:00")

        battery = decode_rawdata(messages, timebase="absolute")["BATTERY"]
        self.assertEqual(len(battery), 2)
        t = battery["time"].to_numpy()
        percent = battery["battery_percent"].to_numpy()
        self.assertGreater(t[1] - t[0], 3600, "readings should be ~2 h apart")
        self.assertAlmostEqual(percent[0], 89.11)
        self.assertAlmostEqual(percent[1], 42.5)

    def test_captures_without_status_keep_the_old_schema(self):
        import os

        path = os.path.join(os.path.dirname(__file__), "test_data", "data_p1041.txt")
        messages = []
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 2500:
                    break
                if line.strip():
                    messages.append(line.strip())
        frames, info = decode_rawdata(messages, return_info=True)
        self.assertEqual(info["n_status_replies"], 0)
        self.assertEqual(list(frames["BATTERY"].columns), ["time", "battery_percent"])

    def test_out_of_range_status_battery_is_rejected(self):
        bad = dict(STATUS_REPLY)
        bad["bp"] = 150.0
        frames = decode_rawdata(control_lines(wire(bad)))
        self.assertEqual(len(frames["BATTERY"]), 0)


if __name__ == "__main__":
    unittest.main()
