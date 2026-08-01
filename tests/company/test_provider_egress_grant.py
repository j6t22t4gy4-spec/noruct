from __future__ import annotations

import unittest

from dynamic_firm.company.provider_egress_grant import (
    ContextProjection,
    ContextProjectionItem,
    ProviderEgressGrant,
    authorize_provider_egress,
    send_authorized_provider_context,
)


def projection(*items: tuple[str, str, str], payload: bytes = b"redacted context") -> ContextProjection:
    return ContextProjection(tuple(ContextProjectionItem(*item) for item in items), payload)


class ProviderEgressGrantTests(unittest.TestCase):
    def grant(self, value: ContextProjection) -> ProviderEgressGrant:
        return ProviderEgressGrant("route-a", "a" * 64, "b" * 64, ("public-source",), ("PUBLIC",), value.digest)

    def test_exact_route_and_redacted_bytes_are_required_at_provider_boundary(self) -> None:
        value = projection(("public-source", "PUBLIC", "c" * 64))
        grant = self.grant(value)
        sent: list[bytes] = []
        self.assertEqual(send_authorized_provider_context(grant, "route-a", "a" * 64, "b" * 64, value, sent.append), None)
        self.assertEqual(sent, [b"redacted context"])
        with self.assertRaises(ValueError):
            authorize_provider_egress(grant, "route-b", "a" * 64, "b" * 64, value, b"redacted context")
        with self.assertRaises(ValueError):
            authorize_provider_egress(grant, "route-a", "a" * 64, "b" * 64, value, b"full prompt with private content")

    def test_no_grant_mixed_sensitivity_and_projection_shape_fail_closed(self) -> None:
        public = projection(("public-source", "PUBLIC", "c" * 64))
        grant = self.grant(public)
        mixed = projection(("public-source", "PUBLIC", "c" * 64), ("restricted-source", "RESTRICTED", "d" * 64))
        with self.assertRaises(ValueError): authorize_provider_egress(None, "route-a", "a" * 64, "b" * 64, public, public.outbound_payload)
        with self.assertRaises(ValueError): authorize_provider_egress(grant, "route-a", "a" * 64, "b" * 64, mixed, mixed.outbound_payload)
        with self.assertRaises(ValueError): ContextProjection((), b"redacted")
        with self.assertRaises(ValueError): projection(("public-source", "PUBLIC", "c" * 64), ("public-source", "PUBLIC", "d" * 64))

    def test_digest_and_classification_drift_fail_closed(self) -> None:
        public = projection(("public-source", "PUBLIC", "c" * 64))
        grant = self.grant(public)
        changed = projection(("public-source", "INTERNAL", "c" * 64))
        with self.assertRaises(ValueError): authorize_provider_egress(grant, "route-a", "a" * 64, "b" * 64, changed, changed.outbound_payload)
        with self.assertRaises(ValueError): authorize_provider_egress(grant, "route-a", "a" * 64, "c" * 64, public, public.outbound_payload)
