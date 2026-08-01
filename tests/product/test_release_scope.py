import unittest

from dynamic_firm.product.release_scope import (
    RELEASE_SCOPE_CLAIMS,
    release_scope_claim,
)


class ReleaseScopeTests(unittest.TestCase):
    def test_matrix_contains_exactly_the_six_disabled_claims(self) -> None:
        expected = {
            "unrestricted_in_process_plugin",
            "silent_marketplace_update",
            "broad_autonomous_replanning",
            "customer_shared_automatic_evolution",
            "silent_oauth_sync",
            "hosted_multi_user_control_plane",
        }

        self.assertEqual(len(RELEASE_SCOPE_CLAIMS), 6)
        self.assertEqual(
            {claim.capability for claim in RELEASE_SCOPE_CLAIMS}, expected
        )
        self.assertTrue(all(not claim.enabled for claim in RELEASE_SCOPE_CLAIMS))
        self.assertEqual(
            len({claim.reason_code for claim in RELEASE_SCOPE_CLAIMS}), 6
        )

    def test_claims_are_immutable(self) -> None:
        with self.assertRaises((AttributeError, TypeError)):
            RELEASE_SCOPE_CLAIMS[0].enabled = True  # type: ignore[misc]
        with self.assertRaises(TypeError):
            RELEASE_SCOPE_CLAIMS[0] = RELEASE_SCOPE_CLAIMS[0]  # type: ignore[index]

    def test_unknown_lookup_fails_closed_without_echoing_input(self) -> None:
        claim = release_scope_claim("/secret/token/provider-path")

        self.assertFalse(claim.enabled)
        self.assertEqual(claim.reason_code, "UNSUPPORTED_CAPABILITY")
        self.assertEqual(claim.capability, "unknown")
        self.assertNotIn("secret", repr(claim))

    def test_known_lookup_returns_the_stable_claim(self) -> None:
        claim = release_scope_claim("silent_oauth_sync")

        self.assertFalse(claim.enabled)
        self.assertEqual(claim.reason_code, "UNSUPPORTED_SILENT_OAUTH")


if __name__ == "__main__":
    unittest.main()
