from __future__ import annotations

import io
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import main
from dynamic_firm.company.models import content_digest
from dynamic_firm.evolution import EvolutionNetworkService
from dynamic_firm.evolution.artifact_bundle import build_artifact_registry_bundle
from dynamic_firm.evolution.signing import allowed_signers_digest
from dynamic_firm.evolution.store import EvolutionStore
from dynamic_firm.network import (
    FIRST_PARTY_NETWORK_ORIGIN,
    FIRST_PARTY_NETWORK_SIGNER_PRINCIPAL,
    FIRST_PARTY_NETWORK_SOURCE_ID,
    NoructNetworkService,
)


@unittest.skipUnless(Path("/usr/bin/ssh-keygen").is_file(), "OpenSSH ssh-keygen is unavailable")
class NoructNetworkServiceTests(unittest.TestCase):
    @staticmethod
    def _artifact(
        artifact_id: str, kind: str, content: dict[str, object]
    ) -> dict[str, object]:
        return {
            "schema": "noruct.evolution-artifact.v1",
            "artifact_id": artifact_id,
            "version": "1.0.0",
            "kind": kind,
            "release_channel": "STABLE",
            "compatibility": {"runtime_contract": "noruct_v1", "required_capabilities": []},
            "content": content,
            "passport": {
                "schema": "noruct.workforce-passport.v1",
                "benchmark": {"suite_id": "public_fixture", "version": "1.0.0", "digest": "b" * 64},
                "metrics": {"quality_score": 0.9, "safety_score": 1.0, "cost_bucket": "LOW", "latency_bucket": "LOW"},
                "limitations": [],
            },
        }

    def test_network_cli_is_a_first_class_command_not_a_chat_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            result = main(
                [
                    "network",
                    "source",
                    "list",
                    "--evolution-state",
                    str(Path(directory) / "network.db"),
                    "--json",
                ],
                stdout=output,
            )
        self.assertEqual(result, 0)
        self.assertIn('"sources": []', output.getvalue())

    def test_network_details_is_a_local_catalog_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            result = main(
                [
                    "network",
                    "details",
                    "missing_template",
                    "--evolution-state",
                    str(Path(directory) / "network.db"),
                    "--json",
                ],
                stdout=output,
            )
        self.assertEqual(result, 0)
        self.assertIn('"versions": []', output.getvalue())

    def test_source_registration_disables_automatic_updates_for_every_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            with EvolutionStore(root / "network.db") as store:
                service = NoructNetworkService(store)
                with self.assertRaisesRegex(ValueError, "Automatic Network updates are disabled"):
                    service.register_source(
                        source_id="noruct_first_party",
                        publisher_class="FIRST_PARTY",
                        origin="https://network.noruct.example",
                        allowed_signers=allowed,
                        signer_principal="noruct_publisher",
                        ssh_keygen=Path("/usr/bin/ssh-keygen"),
                        operator_id="operator_local",
                        auto_update_enabled=True,
                    )
                result = service.register_source(
                    source_id="noruct_first_party",
                    publisher_class="FIRST_PARTY",
                    origin="https://network.noruct.example",
                    allowed_signers=allowed,
                    signer_principal="noruct_publisher",
                    ssh_keygen=Path("/usr/bin/ssh-keygen"),
                    operator_id="operator_local",
                )
                self.assertEqual(result["source"]["source_id"], "noruct_first_party")
                self.assertEqual(result["source"]["publisher_class"], "FIRST_PARTY")
                self.assertFalse(result["source"]["auto_update_enabled"])
                self.assertEqual(result["runtime_effect"], "NONE")
                self.assertEqual(service.search()["available"], ())
                self.assertEqual(service.search()["staged"], ())
                sync = service.sync_first_party_updates(
                    source_id="noruct_first_party",
                    scope_key="company_default",
                    allowed_capabilities=(),
                )
                self.assertEqual(sync["decision"], "AUTOMATIC_NETWORK_UPDATE_DISABLED")
                self.assertEqual(sync["runtime_effect"], "NONE")

    def test_first_party_bootstrap_uses_the_canonical_origin_without_network_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            with EvolutionStore(root / "network.db") as store:
                service = NoructNetworkService(store)
                result = service.bootstrap_first_party_source(
                    allowed_signers=allowed,
                    operator_id="operator_local",
                    ssh_keygen=Path("/usr/bin/ssh-keygen"),
                )
        self.assertEqual(result["source"]["source_id"], FIRST_PARTY_NETWORK_SOURCE_ID)
        self.assertEqual(result["source"]["origin"], FIRST_PARTY_NETWORK_ORIGIN)
        self.assertEqual(result["source"]["signer_principal"], FIRST_PARTY_NETWORK_SIGNER_PRINCIPAL)
        self.assertFalse(result["source"]["auto_update_enabled"])
        self.assertEqual(result["network_effect"], "TRUST_ROOT_CONFIGURATION_ONLY")
        self.assertEqual(result["runtime_effect"], "NONE")

    def test_first_party_bootstrap_is_a_first_class_cli_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            output = io.StringIO()
            result = main(
                [
                    "network", "source", "first-party",
                    "--allowed-signers", str(allowed),
                    "--operator-id", "operator_local",
                    "--ssh-keygen", "/usr/bin/ssh-keygen",
                    "--confirm",
                    "--evolution-state", str(root / "network.db"),
                    "--json",
                ],
                stdout=output,
            )
        self.assertEqual(result, 0)
        self.assertIn('"network_effect": "TRUST_ROOT_CONFIGURATION_ONLY"', output.getvalue())

    def test_network_source_requires_https_except_explicit_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            with EvolutionStore(root / "network.db") as store:
                service = NoructNetworkService(store)
                with self.assertRaisesRegex(ValueError, "requires HTTPS"):
                    service.register_source(
                        source_id="unsafe_source",
                        publisher_class="PRIVATE_TEAM",
                        origin="http://catalog.example",
                        allowed_signers=allowed,
                        signer_principal="team_publisher",
                        ssh_keygen=Path("/usr/bin/ssh-keygen"),
                        operator_id="operator_local",
                    )
                result = service.register_source(
                    source_id="loopback_source",
                    publisher_class="PRIVATE_TEAM",
                    origin="http://127.0.0.1:8787",
                    allowed_signers=allowed,
                    signer_principal="team_publisher",
                        ssh_keygen=Path("/usr/bin/ssh-keygen"),
                        operator_id="operator_local",
                        credential_env="NORUCT_PRIVATE_SOURCE_TOKEN",
                        private_registry_id="private_team_registry",
                        allow_insecure_loopback=True,
                )
                self.assertEqual(result["source"]["origin"], "http://127.0.0.1:8787")

    def test_registered_evaluator_runs_only_after_reviewed_network_install_and_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed-signers"
            allowed.write_text("fixture signer", encoding="utf-8")
            benchmark = self._artifact(
                "capability_delta_suite",
                "BENCHMARK_SUITE",
                {
                    "fixture_ids": [
                        "public_synthetic_capability_alias_v1",
                        "public_synthetic_capability_alias_safety_v1",
                    ],
                    "scorer": "capability_route_delta",
                    "required_capabilities": [],
                    "adapter_reference": "blueprint_delta_holdout_suite_v1",
                },
            )
            evaluator = self._artifact(
                "capability_delta_evaluator",
                "EVALUATOR_PROFILE",
                {
                    "evaluator": "capability_route_delta",
                    "threshold_profile": "manual_review_only",
                    "required_capabilities": [],
                    "adapter_reference": "blueprint_delta_holdout_suite_v1",
                },
            )
            with EvolutionStore(root / "network.db") as store:
                service = NoructNetworkService(store)
                service.register_source(
                    source_id="community_fixture",
                    publisher_class="COMMUNITY",
                    origin="https://community.example",
                    allowed_signers=allowed,
                    signer_principal="fixture_signer",
                    ssh_keygen=Path("/usr/bin/ssh-keygen"),
                    operator_id="operator_fixture",
                )
                bundle = build_artifact_registry_bundle(
                    (benchmark, evaluator), registry_id="community_fixture"
                )
                receipt = {
                    "algorithm": "openssh-detached-signature",
                    "principal": "fixture_signer",
                    "payload_digest": content_digest(
                        EvolutionNetworkService.artifact_registry_bundle_signing_payload(bundle).decode("utf-8")
                    ),
                    "signature_digest": hashlib.sha256(b"fixture-signature").hexdigest(),
                    "allowed_signers_digest": allowed_signers_digest(allowed),
                }
                with patch(
                    "dynamic_firm.evolution.signing.verify_openssh_signature_bytes",
                    return_value=receipt,
                ):
                    staged = service.evolution.stage_verified_artifact_registry_bundle(
                        bundle,
                        source_label="community_fixture",
                        signature=b"fixture-signature",
                        allowed_signers=allowed,
                        principal="fixture_signer",
                        ssh_keygen=Path("/usr/bin/ssh-keygen"),
                    )
                reviewed = service.review_snapshot(
                    snapshot_id=str(staged["snapshot_id"]),
                    operator_id="operator_fixture",
                    decision="APPROVE",
                    reason="fixture review",
                )
                for artifact_id in ("capability_delta_suite", "capability_delta_evaluator"):
                    service.install(
                        snapshot_id=str(reviewed["snapshot_id"]),
                        artifact_id=artifact_id,
                        version="1.0.0",
                    )
                    service.activate(
                        scope_key="company_default",
                        artifact_id=artifact_id,
                        version="1.0.0",
                        allowed_capabilities=(),
                    )
                result = service.evaluate_registered_benchmark(
                    scope_key="company_default",
                    benchmark_artifact_id="capability_delta_suite",
                    evaluator_artifact_id="capability_delta_evaluator",
                    blueprint={
                        "schema": "noruct.employee-blueprint.v1",
                        "blueprint_id": "repository_researcher",
                        "version": "1.0.0",
                        "role": "researcher",
                        "capabilities": ["repository_analysis"],
                        "evaluator": "offline_fixture",
                        "policy_digest": "a" * 64,
                    },
                    delta={
                        "schema": "noruct.blueprint-delta.v1",
                        "blueprint_id": "repository_researcher",
                        "base_version": "1.0.0",
                        "candidate_version": "1.1.0",
                        "kind": "CAPABILITY_ALIAS_ADD",
                        "alias": "repository_inspection",
                        "target_capability": "repository_analysis",
                        "rollback": {"kind": "CAPABILITY_ALIAS_REMOVE", "alias": "repository_inspection"},
                    },
                )
        self.assertEqual(result["network_effect"], "EXPLICIT_LOCAL_REGISTERED_EVALUATION")
        self.assertFalse(result["report"]["automatic_promotion_allowed"])
