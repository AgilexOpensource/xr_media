import math
import unittest

from xr_media.xr import XrSample


def _pose(x=0.1, w=1.0):
    return {"position": {"x": x, "y": 0.0, "z": 0.0}, "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": w}}


class XrSampleValidationTest(unittest.TestCase):
    def test_valid_sample_is_tracked(self):
        sample = XrSample.from_json({"t_ms": 5, "hmd": _pose(), "left": _pose(), "right": _pose()})
        self.assertTrue(sample.hmd_tracked and sample.left.tracked and sample.right.tracked)

    def test_non_finite_or_degenerate_poses_are_untracked(self):
        left = dict(_pose(x=math.nan), joints={"wrist": _pose(), "index_tip": _pose(x=math.inf)})
        sample = XrSample.from_json({"t_ms": 5, "hmd": _pose(w=0.0), "left": left, "right": _pose()})
        self.assertFalse(sample.hmd_tracked)
        self.assertFalse(sample.left.tracked)
        self.assertEqual(list(sample.left.joints), ["wrist"])
        self.assertTrue(sample.right.tracked)

    def test_non_finite_analog_inputs_read_as_zero(self):
        right = dict(_pose(), input={"stick_x": math.nan, "stick_y": math.inf, "trigger_analog": -math.inf})
        hand = XrSample.from_json({"right": right}).right.input
        self.assertEqual((hand.stick_x, hand.stick_y, hand.trigger_analog), (0.0, 0.0, 0.0))

    def test_non_finite_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            XrSample.from_json({"t_ms": math.inf})


if __name__ == "__main__":
    unittest.main()
