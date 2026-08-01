import unittest

from calculator import safe_divide


class SafeDivideTests(unittest.TestCase):
    def test_zero_denominator_returns_none(self) -> None:
        self.assertIsNone(safe_divide(10, 0))

    def test_regular_division_is_preserved(self) -> None:
        self.assertEqual(safe_divide(9, 3), 3)
