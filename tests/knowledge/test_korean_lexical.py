from __future__ import annotations

import unittest

from dynamic_firm.korean_lexical import (
    korean_postposition_variants,
    korean_retrieval_variants,
)


class KoreanLexicalTests(unittest.TestCase):
    def test_postposition_expansion_preserves_surface_and_safe_stem(self) -> None:
        self.assertEqual(
            korean_postposition_variants("가격전략을"),
            ("가격전략을", "가격전략"),
        )
        self.assertEqual(korean_postposition_variants("회의록"), ("회의록",))

    def test_expansion_is_bounded_and_never_claims_short_stems(self) -> None:
        self.assertEqual(korean_postposition_variants("에"), ())
        self.assertEqual(korean_postposition_variants("가" * 129), ())

    def test_retrieval_variant_can_remove_one_safe_compound_connector(self) -> None:
        self.assertEqual(
            korean_retrieval_variants("가격전략의변경을"),
            (
                "가격전략의변경을",
                "가격전략의변경",
                "가격전략변경을",
                "가격전략변경",
            ),
        )
        # A one-syllable right side is deliberately not treated as a compound.
        self.assertEqual(korean_retrieval_variants("회의의"), ("회의의", "회의"))
