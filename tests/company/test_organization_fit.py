import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.organization_fit import (
    ORGANIZATION_FIT_PROFILE_SCHEMA,
    OrganizationFitLevel,
    OrganizationFitProfile,
)


class OrganizationFitProfileTests(unittest.TestCase):
    def test_missing_dimensions_are_unknown_and_round_trip_digest_is_stable(self) -> None:
        profile = OrganizationFitProfile.from_mapping({"decomposability": "HIGH"})

        self.assertEqual(profile.decomposability, OrganizationFitLevel.HIGH)
        for name in (
            "dependency_coupling",
            "context_coupling",
            "information_dispersion",
            "verifiability",
            "risk_irreversibility",
            "error_correlation",
            "latency_sensitivity",
        ):
            self.assertEqual(getattr(profile, name), OrganizationFitLevel.UNKNOWN)

        restored = OrganizationFitProfile.from_mapping(json.loads(profile.canonical_json()))
        self.assertEqual(restored, profile)
        self.assertEqual(restored.digest, profile.digest)
        self.assertEqual(profile.content_digest, profile.digest)
        self.assertEqual(profile.canonical_payload()["schema"], ORGANIZATION_FIT_PROFILE_SCHEMA)

    def test_unknown_fields_and_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OrganizationFitProfile.from_mapping({"not_a_dimension": "LOW"})
        with self.assertRaises(ValueError):
            OrganizationFitProfile.from_mapping({"decomposability": "MEDIUM"})
        with self.assertRaises(ValueError):
            OrganizationFitProfile.from_mapping({"schema": "other.v1"})

    def test_all_dimensions_accept_only_the_three_levels(self) -> None:
        values = {name: "LOW" for name in (
            "decomposability",
            "dependency_coupling",
            "context_coupling",
            "information_dispersion",
            "verifiability",
            "risk_irreversibility",
            "error_correlation",
            "latency_sensitivity",
        )}
        profile = OrganizationFitProfile.from_mapping(values)
        self.assertEqual(set(profile.canonical_payload().values()) - {ORGANIZATION_FIT_PROFILE_SCHEMA}, {"LOW"})

    def test_profile_is_immutable_and_has_no_mutation_surface(self) -> None:
        profile = OrganizationFitProfile()

        with self.assertRaises(FrozenInstanceError):
            profile.decomposability = OrganizationFitLevel.HIGH
        self.assertFalse(any(name.startswith(("dispatch", "mutate", "admit")) for name in dir(profile)))


if __name__ == "__main__":
    unittest.main()
