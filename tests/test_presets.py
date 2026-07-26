"""The documented preset table must match what the captures actually contain.

The preset string is the only control over which sensors the headset runs, and
it used to be undocumented -- the package contained three hints in total. The
table in OpenMuse.PRESETS is therefore derived from measurement, and this test
re-derives it from the same committed captures so it cannot silently drift.
"""

import os
import unittest

from OpenMuse.decode import decode_rawdata
from OpenMuse.muse import PRESETS, describe_preset, preset_for_sensors

TEST_DATA = os.path.join(os.path.dirname(__file__), "test_data")
N_LINES = 2500  # ~35 s of capture: enough for every stream, including battery


def _load(path, n=N_LINES):
    messages = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= n:
                break
            line = line.strip()
            if line:
                messages.append(line)
    return messages


def _capture_for(preset):
    path = os.path.join(TEST_DATA, f"data_{preset}.txt")
    return path if os.path.exists(path) else None


class TestPresetTable(unittest.TestCase):
    def test_every_preset_has_a_capture(self):
        missing = [p for p in PRESETS if _capture_for(p) is None]
        self.assertEqual(
            missing,
            [],
            "PRESETS documents presets with no committed capture to back them: "
            f"{missing}",
        )

    def test_composition_matches_captures(self):
        for preset, documented in PRESETS.items():
            path = _capture_for(preset)
            if path is None:
                continue
            with self.subTest(preset=preset):
                frames = decode_rawdata(_load(path))
                measured = {}
                for stream in ("EEG", "OPTICS", "ACCGYRO", "BATTERY"):
                    frame = frames[stream]
                    measured[stream] = (
                        None if len(frame) == 0 else int(frame.shape[1] - 1)
                    )
                self.assertEqual(
                    measured,
                    documented,
                    f"{preset}: documented {documented} but the capture decodes "
                    f"to {measured}",
                )

    def test_optics_free_presets_really_have_no_optics(self):
        # The whole point of documenting the table: these are the presets that
        # turn the LEDs off, which is the largest available battery saving.
        optics_free = [p for p, layout in PRESETS.items() if not layout["OPTICS"]]
        self.assertTrue(optics_free, "no optics-free preset documented")
        for preset in optics_free:
            with self.subTest(preset=preset):
                frames = decode_rawdata(_load(_capture_for(preset)))
                self.assertEqual(
                    len(frames["OPTICS"]),
                    0,
                    f"{preset} is documented as optics-free but produced optics data",
                )
                self.assertGreater(len(frames["EEG"]), 0)
                self.assertGreater(len(frames["ACCGYRO"]), 0)


class TestPresetSelection(unittest.TestCase):
    def test_sensor_list_resolves_to_a_preset(self):
        self.assertEqual(preset_for_sensors(["EEG", "ACCGYRO"]), "p21")
        self.assertEqual(preset_for_sensors(["EEG", "ACCGYRO", "OPTICS"]), "p1041")

    def test_omitting_optics_never_returns_an_optics_preset(self):
        for sensors in (["EEG"], ["ACCGYRO"], ["EEG", "ACCGYRO"], ["EEG", "BATTERY"]):
            with self.subTest(sensors=sensors):
                preset = preset_for_sensors(sensors)
                self.assertIsNone(
                    PRESETS[preset]["OPTICS"],
                    f"{sensors} resolved to {preset}, which streams optics",
                )

    def test_channel_counts_are_honoured(self):
        self.assertEqual(PRESETS[preset_for_sensors(["EEG"], eeg_channels=4)]["EEG"], 4)
        preset = preset_for_sensors(["EEG", "OPTICS"], optics_channels=4)
        self.assertEqual(PRESETS[preset]["OPTICS"], 4)

    def test_impossible_request_raises(self):
        with self.assertRaises(ValueError):
            preset_for_sensors(["EEG"], eeg_channels=99)
        with self.assertRaises(ValueError):
            preset_for_sensors(["NOSUCHSENSOR"])
        with self.assertRaises(ValueError):
            preset_for_sensors([])

    def test_describe_preset(self):
        self.assertEqual(describe_preset("p1041")["OPTICS"], 16)
        self.assertIsNone(describe_preset("p21")["OPTICS"])
        self.assertIsNone(describe_preset("nonexistent"))


if __name__ == "__main__":
    unittest.main()
