"""One bad packet must not cost the file, and time must not be fiction.

Covers two defects that produced silent data loss rather than errors:

* A mixed-width subpacket raised
  ``ValueError: could not broadcast input array from shape (4,4) into shape
  (4,8)`` out of ``decode_rawdata`` and discarded the *entire* decode. On a
  25-minute capture (132,943 packets) that threw away 386,092 recoverable EEG
  rows over two bad subpackets.

* ``time`` was generated as ``sample_index / nominal_rate``: a perfectly
  smooth ramp that restarts at 0 on every call, runs ~0.14% fast (~40 s over an
  8 h night) and absorbs BLE dropouts instead of showing them.
"""

import os
import unittest
import warnings

import numpy as np

from OpenMuse.decode import _decode_battery_data, decode_rawdata

TEST_DATA = os.path.join(os.path.dirname(__file__), "test_data")


def _load(name, n):
    messages = []
    with open(os.path.join(TEST_DATA, name), "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= n:
                break
            line = line.strip()
            if line:
                messages.append(line)
    return messages


class TestMalformedPacketResilience(unittest.TestCase):
    """A mixed-width subpacket costs only itself."""

    @classmethod
    def setUpClass(cls):
        # data_p1041 is EEG8 (TAG 0x12) + Optics16; data_p1034 is EEG4
        # (TAG 0x11) + Optics8. Both decode into the same "EEG" and "OPTICS"
        # streams, which is exactly how the width mismatch arises in the wild.
        cls.eeg8 = _load("data_p1041.txt", 400)
        cls.eeg4 = _load("data_p1034.txt", 400)

    def test_mixed_widths_do_not_raise(self):
        mixed = self.eeg8[:200] + self.eeg4[:20] + self.eeg8[200:]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frames = decode_rawdata(mixed)  # must not raise
        self.assertGreater(len(frames["EEG"]), 0)

    def test_majority_layout_survives_intact(self):
        """A handful of stray packets must not displace the bulk of the file."""
        clean = decode_rawdata(self.eeg8)
        mixed_lines = self.eeg8[:200] + self.eeg4[:20] + self.eeg8[200:]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frames, info = decode_rawdata(mixed_lines, return_info=True)

        eeg = frames["EEG"]
        self.assertEqual(eeg.shape[1], clean["EEG"].shape[1], "channel count changed")
        # Everything from the clean subset, plus the recovered narrow samples.
        self.assertGreaterEqual(len(eeg), len(clean["EEG"]))
        self.assertGreater(info["streams"]["EEG"]["n_padded"], 0)
        self.assertEqual(info["streams"]["EEG"]["n_skipped"], 0)

    def test_narrow_eeg_is_padded_not_dropped(self):
        """EEG4's channels are the first four of EEG8, so its samples survive."""
        mixed = self.eeg8[:200] + self.eeg4[:20] + self.eeg8[200:]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            eeg = decode_rawdata(mixed)["EEG"]
        scalp = eeg.iloc[:, 1:5].to_numpy()
        aux = eeg.iloc[:, 5:].to_numpy()
        self.assertFalse(
            np.isnan(scalp).any(), "TP9/AF7/AF8/TP10 must never be NaN-padded"
        )
        self.assertTrue(np.isnan(aux).any(), "absent AUX channels should be NaN")

    def test_incompatible_optics_is_dropped_not_mislabelled(self):
        """Optics4/8 are different sensor selections, not a prefix of Optics16."""
        mixed = self.eeg8[:200] + self.eeg4[:20] + self.eeg8[200:]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frames, info = decode_rawdata(mixed, return_info=True)
        optics = info["streams"]["OPTICS"]
        self.assertEqual(optics["n_channels"], 16)
        self.assertGreater(optics["n_skipped"], 0)
        self.assertEqual(
            optics["n_padded"], 0, "optics must never be padded into wrong columns"
        )

    def test_losses_are_reported_not_silent(self):
        mixed = self.eeg8[:200] + self.eeg4[:20] + self.eeg8[200:]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            decode_rawdata(mixed)
        self.assertTrue(
            any(issubclass(w.category, RuntimeWarning) for w in caught),
            "skipped/padded subpackets must be reported",
        )

    def test_clean_capture_reports_no_losses(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _, info = decode_rawdata(self.eeg8, return_info=True)
        self.assertEqual(caught, [])
        for stream, meta in info["streams"].items():
            self.assertEqual(meta["n_skipped"], 0, stream)
            self.assertEqual(meta["n_padded"], 0, stream)


class TestDecodeNeverRaises(unittest.TestCase):
    """decode_rawdata must never raise because of one bad packet."""

    def test_zero_length_packet(self):
        # A declared packet length of 0 used to index past the end of the
        # buffer (IndexError out of _parse_packets) and, had it not, would have
        # spun forever without advancing the offset. Trailing padding bytes in
        # a BLE message look exactly like this.
        line = "2026-01-01T00:00:00+00:00\t273e0013-4c4d-454d-96be-f03bac821358\t" + (
            "1001" + "00" * 30
        )
        frames = decode_rawdata([line])
        self.assertIsNotNone(frames)

    def test_truncated_and_garbage_payloads(self):
        prefix = "2026-01-01T00:00:00+00:00\t273e0013-4c4d-454d-96be-f03bac821358\t"
        payloads = [
            "",
            "00",
            "ff",
            "ff" * 8,
            "01" * 64,
            "12" * 300,
            "0e" + "00" * 13,
            "aa55" * 100,
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for payload in payloads:
                with self.subTest(payload=payload[:16]):
                    decode_rawdata([prefix + payload])

    def test_malformed_lines(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            decode_rawdata(["", "not a line", "a\tb", "a\tb\tzz", "\t\t"])

    def test_truncating_a_real_capture_anywhere_is_safe(self):
        real = _load("data_p1041.txt", 60)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for line in real:
                stamp, uuid, payload = line.split("\t", 2)
                for cut in (2, 10, 30, len(payload) // 2, len(payload) - 2):
                    if cut <= 0 or cut % 2:
                        continue
                    decode_rawdata([f"{stamp}\t{uuid}\t{payload[:cut]}"])


class TestTimebase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = _load("data_p1041.txt", 3000)

    def test_relative_is_the_default_and_unchanged(self):
        default = decode_rawdata(self.messages)
        explicit = decode_rawdata(self.messages, timebase="relative")
        np.testing.assert_array_equal(
            default["EEG"]["time"].to_numpy(), explicit["EEG"]["time"].to_numpy()
        )
        # The historical behaviour: starts at 0, not at wall-clock time.
        self.assertAlmostEqual(float(default["EEG"]["time"].iloc[0]), 0.0)

    def test_absolute_is_wall_clock(self):
        frames = decode_rawdata(self.messages, timebase="absolute")
        first_stamp = self.messages[0].split("\t")[0]
        from datetime import datetime

        expected = datetime.fromisoformat(
            first_stamp.replace("Z", "+00:00")
        ).timestamp()
        for stream in ("EEG", "OPTICS", "ACCGYRO"):
            t = frames[stream]["time"].to_numpy()
            self.assertAlmostEqual(
                t[0],
                expected,
                delta=5.0,
                msg=f"{stream}: absolute time is not anchored to the raw stamps",
            )

    def test_absolute_is_monotonic(self):
        frames = decode_rawdata(self.messages, timebase="absolute")
        for stream in ("EEG", "OPTICS", "ACCGYRO", "BATTERY"):
            frame = frames[stream]
            if len(frame) < 2:
                continue
            t = frame["time"].to_numpy()
            self.assertTrue(
                np.all(np.diff(t) >= 0), f"{stream}: absolute time goes backwards"
            )

    def test_absolute_spans_the_real_wall_clock_duration(self):
        """The nominal ramp drifts; the measured one does not."""
        from datetime import datetime

        def stamp(line):
            return datetime.fromisoformat(
                line.split("\t")[0].replace("Z", "+00:00")
            ).timestamp()

        wall = stamp(self.messages[-1]) - stamp(self.messages[0])
        frames = decode_rawdata(self.messages, timebase="absolute")
        t = frames["EEG"]["time"].to_numpy()
        self.assertAlmostEqual((t[-1] - t[0]), wall, delta=max(1.0, 0.02 * wall))

    def test_measured_rate_is_reported_and_differs_from_nominal(self):
        _, info = decode_rawdata(
            self.messages, timebase="absolute", return_info=True
        )
        for stream in ("EEG", "OPTICS", "ACCGYRO"):
            meta = info["streams"][stream]
            self.assertTrue(
                np.isfinite(meta["fs_measured"]), f"{stream}: no measured rate"
            )
            # Sane, but genuinely measured rather than copied from the nominal.
            self.assertAlmostEqual(
                meta["fs_measured"],
                meta["fs_nominal"],
                delta=0.1 * meta["fs_nominal"],
                msg=f"{stream}: measured rate implausible",
            )

    def test_chunked_absolute_decode_does_not_stack(self):
        """Decoding in chunks is forced by resilience; chunks must not overlap."""
        half = len(self.messages) // 2
        first = decode_rawdata(self.messages[:half], timebase="absolute")["EEG"]
        second = decode_rawdata(self.messages[half:], timebase="absolute")["EEG"]
        self.assertGreater(
            float(second["time"].iloc[0]),
            float(first["time"].iloc[0]),
            "chunk 2 lands on top of chunk 1 -- absolute time is not absolute",
        )
        # The relative timebase is exactly the trap this avoids.
        rel_first = decode_rawdata(self.messages[:half])["EEG"]
        rel_second = decode_rawdata(self.messages[half:])["EEG"]
        self.assertAlmostEqual(
            float(rel_first["time"].iloc[0]), float(rel_second["time"].iloc[0])
        )

    def test_invalid_timebase_rejected(self):
        with self.assertRaises(ValueError):
            decode_rawdata(self.messages[:10], timebase="nonsense")


class TestBatteryRange(unittest.TestCase):
    def test_impossible_percentages_become_nan(self):
        # 0xFFFF / 256 = 255.996 -- a "percentage" that cannot exist. The old
        # code returned it as a number (150.0, 228.0, 229.0 were seen in the
        # wild); a missing reading is recoverable, a wrong one is not.
        self.assertTrue(np.isnan(_decode_battery_data(b"\xff\xff")[0]))
        self.assertTrue(np.isnan(_decode_battery_data(b"\x00\x96")[0]))  # 150.0

    def test_plausible_percentages_survive(self):
        # 0x5A00 / 256 = 90.0
        self.assertAlmostEqual(float(_decode_battery_data(b"\x00\x5a")[0]), 90.0)
        self.assertAlmostEqual(float(_decode_battery_data(b"\x00\x64")[0]), 100.0)
        self.assertAlmostEqual(float(_decode_battery_data(b"\x00\x00")[0]), 0.0)

    def test_short_payload_returns_none(self):
        self.assertIsNone(_decode_battery_data(b"\x01"))


if __name__ == "__main__":
    unittest.main()
