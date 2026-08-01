import unittest

from identifier import canonical_identifier


class IdentifierTests(unittest.TestCase):
    def test_spaces_are_normalized(self) -> None:
        self.assertEqual(canonical_identifier(" Release  Candidate "), "release-candidate")
