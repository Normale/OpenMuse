"""Regression test for the Optics16 channel order (issue #24).

The device transmits wavelength-major, not location-major. Under the old
labelling every channel name pointed at a different sensor, so any analysis
that selected a channel *by name* read the wrong photodiode -- and pulse
oximetry, which depends on a specific RED/IR pair, was impossible to get right.
Nothing raised; the numbers just looked plausible and were wrong.

The decisive evidence is physiological and comes from an overnight capture
(firmware 3.1.23, Athena_RevE, p1041; subject asleep, still, dark room): four
channels labelled AMB under the old order carried a strong heartbeat, cardiac
SNR 59-722, which is impossible for a channel whose LED is off. That capture is
not committed here, so this test instead pins the structural signature that IS
reproducible on this repo's own captures and agrees with it: indices 4-7 form a
tight, high, near-constant quartet, clearly separated from the other twelve --
the signature of an ambient reference rather than a measurement.

    capture            idx 4-7 means              next highest channel
    data_p1041.txt     11.55 11.37 13.59 12.73    10.33
    test_optics16.txt  12.33 12.18 15.24 15.10     5.63
    overnight (3.1.23) 10.274 10.261 10.269 10.257  3.65  (std 0.0005)

Under the corrected mapping those four are exactly the _AMB channels. Under the
old one they were LI_NIR, RI_NIR, LI_IR and RI_IR -- four *illuminated*
channels that would have to be simultaneously saturated and pulse-free.
"""

import os
import unittest

import numpy as np

from OpenMuse.decode import (
    OPTICS4_CHANNELS,
    OPTICS8_CHANNELS,
    OPTICS16_CHANNELS,
    OPTICS_CHANNELS,
    decode_rawdata,
    select_optics_channels,
)

TEST_DATA = os.path.join(os.path.dirname(__file__), "test_data")
OPTICS16_CAPTURES = ("data_p1041.txt", "data_p1042.txt", "test_optics16.txt")


def _load(name, n=6000):
    messages = []
    with open(os.path.join(TEST_DATA, name), "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= n:
                break
            line = line.strip()
            if line:
                messages.append(line)
    return messages


class TestOpticsLabels(unittest.TestCase):
    def test_sixteen_unique_names(self):
        self.assertEqual(len(OPTICS16_CHANNELS), 16)
        self.assertEqual(len(set(OPTICS16_CHANNELS)), 16)

    def test_wavelength_major(self):
        """Each block of four is one wavelength across the four positions."""
        for start, wavelength in ((0, "RED"), (4, "AMB"), (8, "NIR"), (12, "IR")):
            block = OPTICS16_CHANNELS[start : start + 4]
            self.assertEqual(
                [name.rsplit("_", 1)[1] for name in block],
                [wavelength] * 4,
                f"indices {start}-{start+3} should all be {wavelength}: {block}",
            )
            self.assertEqual(
                [name.split("_")[1] for name in block],
                ["LI", "RI", "LO", "RO"],
                f"positions out of order in {block}",
            )

    def test_ambient_group_is_indices_4_to_7(self):
        amb = [i for i, n in enumerate(OPTICS16_CHANNELS) if n.endswith("_AMB")]
        self.assertEqual(amb, [4, 5, 6, 7])

    def test_alias_and_selection(self):
        self.assertIs(OPTICS_CHANNELS, OPTICS16_CHANNELS)
        self.assertEqual(select_optics_channels(16), list(OPTICS16_CHANNELS))
        self.assertEqual(select_optics_channels(8), list(OPTICS8_CHANNELS))
        self.assertEqual(select_optics_channels(4), list(OPTICS4_CHANNELS))
        # Optics4 is a different sensor selection, not a slice of Optics16.
        self.assertEqual(len(OPTICS4_CHANNELS), 4)
        self.assertNotEqual(list(OPTICS4_CHANNELS), list(OPTICS16_CHANNELS[:4]))


class TestAmbientChannelsFromData(unittest.TestCase):
    """The _AMB channels must be the reference quartet, in real captures."""

    def _optics16(self, name):
        frame = decode_rawdata(_load(name))["OPTICS"]
        self.assertGreater(len(frame), 100, f"{name}: too little optics data")
        self.assertEqual(
            list(frame.columns)[1:],
            list(OPTICS16_CHANNELS),
            f"{name} is not a 16-channel capture",
        )
        return frame

    def test_amb_channels_are_the_reference_quartet(self):
        for name in OPTICS16_CAPTURES:
            with self.subTest(capture=name):
                frame = self._optics16(name)
                means = frame[list(OPTICS16_CHANNELS)].mean().to_numpy()

                amb_idx = [
                    i for i, n in enumerate(OPTICS16_CHANNELS) if n.endswith("_AMB")
                ]
                top4 = set(np.argsort(means)[-4:].tolist())
                self.assertEqual(
                    top4,
                    set(amb_idx),
                    f"{name}: the four highest-DC channels are {sorted(top4)}, "
                    f"but the _AMB channels are at {amb_idx}. Either the "
                    f"channel order is wrong or this capture is unusual.",
                )

                # ...and clearly separated from the rest, not a near-tie.
                amb_min = means[amb_idx].min()
                rest_max = means[[i for i in range(16) if i not in amb_idx]].max()
                self.assertGreater(
                    amb_min,
                    rest_max * 1.05,
                    f"{name}: ambient quartet ({amb_min:.2f}) does not stand "
                    f"clear of the brightest LED channel ({rest_max:.2f})",
                )

    def test_ambient_quartet_is_internally_consistent(self):
        """The four ambient channels see the same room light, so they agree."""
        for name in OPTICS16_CAPTURES:
            with self.subTest(capture=name):
                frame = self._optics16(name)
                amb = [n for n in OPTICS16_CHANNELS if n.endswith("_AMB")]
                means = frame[amb].mean().to_numpy()
                spread = (means.max() - means.min()) / means.mean()
                self.assertLess(
                    spread,
                    0.5,
                    f"{name}: ambient channels disagree by {spread:.0%}, which "
                    f"is not how a common reference behaves",
                )


if __name__ == "__main__":
    unittest.main()
