import unittest

from window import within_window


class WindowTests(unittest.TestCase):
    def test_bounds_are_inclusive(self) -> None:
        self.assertTrue(within_window(1, 1, 3))
        self.assertTrue(within_window(3, 1, 3))

    def test_outside_value_is_rejected(self) -> None:
        self.assertFalse(within_window(4, 1, 3))
