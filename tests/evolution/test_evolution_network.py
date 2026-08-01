from __future__ import annotations

import io
import json
import hashlib
import os
import asyncio
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
from pathlib import Path

from dynamic_firm.cli import (
    EXIT_INPUT,
    EXIT_OK,
    RunCommandConfig,
    _mcp_policy_for_frozen_artifacts,
    main,
    run_goal,
)
from dynamic_firm.company import CompanyStateStore, EvolutionAutonomyMode
from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.compiler import CompilerExecutionProfile
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.evolution import (
    BlueprintAdmissionDecision,
    BlueprintDeltaHoldoutDecision,
    EvolutionNetworkService,
    EvolutionStore,
    evaluate_blueprint_admission,
    evaluate_blueprint_delta_holdout,
    evaluate_blueprint_delta_holdout_suite,
    network_gate_status,
    preview_network_worker,
    preview_capability_grant,
    canonical_evolution_json,
    evolution_content_digest,
)
from dynamic_firm.evolution.artifact_bundle import build_artifact_registry_bundle
from dynamic_firm.evolution.runtime_adapter import (
    EvolutionRuntimeArtifactAdapter,
    project_network_workflow_priors,
    runtime_artifact_scopes,
)
from dynamic_firm.evolution.mcp_package import build_mcp_policy_artifact
from dynamic_firm.evolution.managed_skill_package import build_managed_skill_artifact
from dynamic_firm.evolution.signing import allowed_signers_digest
from dynamic_firm.evolution.service import validate_capsule, validate_evolution_artifact
from dynamic_firm.evolution.hosted_transport import (
    assemble_candidates,
    authorize_artifact_registry_publication,
    expire_pending_contributions,
    finalize_pending_contribution,
    list_operator_candidates,
    publish_artifact_registry,
    record_candidate_evaluation,
    retire_artifact_registry,
)
from dynamic_firm.product.routing import InputRoute
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import CompletionEnvelope, ModelResponse, RunLimits
from dynamic_firm.mcp_connector import (
    McpReadOnlyConfig,
    McpReadOnlyConfigSet,
    session_binding_digest as mcp_session_binding_digest,
)


POLICY_DIGEST = "a" * 64


class _HostedIntakeHandler(BaseHTTPRequestHandler):
    """Small loopback peer for the explicit HTTPS-client contract tests."""

    records: dict[str, dict] = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, value: dict, status: int = 200) -> None:
        raw = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/internal/pending-contributions/contribution-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/finalize":
            if self.headers.get("authorization") != "Bearer finalizer-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            self._json({
                "status": "FINALIZED_SIGNAL",
                "contribution_id": "contribution-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "signal_id": "signal-fixture",
                "learning_key": "repository_analysis:software:analyze:LOW",
                "proposal_recorded": True,
            })
            return
        if self.path == "/v1/internal/candidates/assemble":
            if self.headers.get("authorization") != "Bearer finalizer-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            self._json({
                "schema": "noruct.evolution-candidate-assembly.v1",
                "assembled_at": "2026-07-21T00:02:00+00:00",
                "finalized_proposal_groups": 1,
                "evaluation_ready": [],
            })
            return
        if self.path == "/v1/internal/candidates/candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/evaluations":
            if self.headers.get("authorization") != "Bearer finalizer-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self._json({
                "status": "OPERATOR_REVIEW_READY",
                "candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "evaluation_digest": evolution_content_digest(body),
                "evaluated_at": "2026-07-21T00:02:00+00:00",
                "idempotent": False,
            })
            return
        if self.path == "/v1/internal/pending-contributions/expire":
            if self.headers.get("authorization") != "Bearer finalizer-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            self._json({
                "schema": "noruct.evolution-pending-expiry.v1",
                "expired_count": 1,
                "bounded_limit": 100,
                "processed_at": "2026-07-21T00:02:00+00:00",
                "more_may_remain": False,
            })
            return
        if self.path == "/v1/internal/artifact-registries/fixture_registry/authorizations":
            if self.headers.get("authorization") != "Bearer reviewer-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self._json({
                "schema": "noruct.evolution-artifact-registry-publication-authorization-receipt.v1",
                "authorization_id": "authorization-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "authorization_digest": content_digest(body),
                "registry_id": body["registry_id"],
                "bundle_digest": body["bundle_digest"],
                "evidence_digest": content_digest({
                    "candidate_evidence_digests": body["candidate_evidence_digests"],
                    "evaluation_evidence_digests": body["evaluation_evidence_digests"],
                    "artifact_manifest_digests": body["artifact_manifest_digests"],
                }),
                "status": "PENDING",
                "authorized_at": "2026-07-21T00:02:30+00:00",
                "idempotent": False,
            }, 201)
            return
        if self.path == "/v1/internal/artifact-registries/fixture_registry/publish":
            if self.headers.get("authorization") != "Bearer publisher-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self._json({
                "schema": "noruct.evolution-artifact-registry-publication-receipt.v1",
                "authorization_id": body["authorization_id"],
                "registry_id": body["registry_id"],
                "bundle_digest": body["bundle"]["bundle_digest"],
                "signature_digest": hashlib.sha256(body["signature"].encode("utf-8")).hexdigest(),
                "published_at": "2026-07-21T00:03:00+00:00",
                "status": "ACTIVE",
                "idempotent": False,
            })
            return
        if self.path == "/v1/internal/artifact-registries/fixture_registry/retire":
            if self.headers.get("authorization") != "Bearer publisher-token":
                self._json({"code": "UNAUTHORIZED"}, 401)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self._json({
                "schema": "noruct.evolution-artifact-registry-retirement.v1",
                "registry_id": body["registry_id"], "bundle_digest": "e" * 64,
                "status": "RETIRED", "retired_at": "2026-07-21T00:04:00+00:00",
                "reason_code": body["reason_code"],
            })
            return
        if self.path != "/v1/contributions" or self.headers.get("authorization") != "Bearer fixture-token":
            self._json({"code": "UNAUTHORIZED"}, 401)
            return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        capsule_id = body["capsule_id"]
        capsule_digest = evolution_content_digest(body["capsule"])
        contribution_id = f"contribution-{capsule_id.removeprefix('capsule-')}"
        unsigned = {
            "schema": "noruct.evolution-network-intake-receipt.v1",
            "event_type": "ACCEPTED",
            "contribution_id": contribution_id,
            "capsule_id": capsule_id,
            "capsule_digest": capsule_digest,
            "recorded_at": "2026-07-21T00:00:00+00:00",
        }
        withdrawal_capability = "b" * 64
        _HostedIntakeHandler.records[contribution_id] = {
            "capsule_id": capsule_id,
            "capsule_digest": capsule_digest,
            "withdrawal_capability": withdrawal_capability,
        }
        self._json({
            **unsigned,
            "expires_at": "2026-08-20T00:00:00+00:00",
            "receipt_digest": hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest(),
            "withdrawal_capability": withdrawal_capability,
            "idempotent": False,
        }, 201)

    def do_DELETE(self) -> None:  # noqa: N802
        contribution_id = self.path.removeprefix("/v1/contributions/")
        record = _HostedIntakeHandler.records.get(contribution_id)
        if (
            self.headers.get("authorization") != "Bearer fixture-token"
            or record is None
            or self.headers.get("x-noruct-withdrawal-capability") != record["withdrawal_capability"]
        ):
            self._json({"code": "NOT_FOUND"}, 404)
            return
        unsigned = {
            "schema": "noruct.evolution-network-intake-receipt.v1",
            "event_type": "WITHDRAWN",
            "contribution_id": contribution_id,
            "capsule_id": record["capsule_id"],
            "capsule_digest": record["capsule_digest"],
            "recorded_at": "2026-07-21T00:01:00+00:00",
        }
        self._json({
            **unsigned,
            "receipt_digest": hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest(),
            "idempotent": False,
        })

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/v1/internal/candidates" or self.headers.get("authorization") != "Bearer finalizer-token":
            self._json({"code": "UNAUTHORIZED"}, 401)
            return
        self._json({
            "schema": "noruct.evolution-candidate-list.v1",
            "candidates": [{
                "candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "learning_key": "repository_analysis:software:analyze:LOW",
                "proposal_digest": "e" * 64,
                "proposal": {"schema": "noruct.evolution-proposal.v1", "kind": "BLUEPRINT_DELTA", "delta": {}},
                "successful_support_count": 3,
                "negative_evidence_count": 0,
                "total_signal_count": 3,
                "status": "EVALUATION_READY",
                "assembled_at": "2026-07-21T00:00:00+00:00",
                "updated_at": "2026-07-21T00:00:00+00:00",
            }],
        })


def capsule() -> dict:
    return {
        "schema": "noruct.learning-capsule.v1",
        "capability": "repository_analysis",
        "authority": "organization_owner",
        "task_schema": {
            "domain": "software",
            "operation": "analyze",
            "input_fields": ["repository_shape"],
            "risk_level": "LOW",
        },
        "execution_summary": {
            "workflow_shape": ["solo"],
            "tool_classes": ["workspace_read"],
            "decision_count": 2,
            "redaction_applied": True,
        },
        "outcome": {
            "status": "SUCCEEDED",
            "quality_score": 0.8,
            "cost_bucket": "LOW",
            "evaluator_kind": "LOCAL_TEST",
            "metric_names": ["acceptance_passed"],
        },
    }


def capsule_with_proposal() -> dict:
    value = capsule()
    value["schema"] = "noruct.learning-capsule.v2"
    value["proposal"] = {
        "schema": "noruct.evolution-proposal.v1",
        "kind": "BLUEPRINT_DELTA",
        "delta": capability_alias_delta(),
    }
    return value


def blueprint(version: str = "1.0.0") -> dict:
    return {
        "schema": "noruct.employee-blueprint.v1",
        "blueprint_id": "repository_researcher",
        "version": version,
        "role": "researcher",
        "capabilities": ["repository_analysis"],
        "evaluator": "offline_fixture",
        "policy_digest": POLICY_DIGEST,
    }


def capability_alias_delta(
    *, base_version: str = "1.0.0", candidate_version: str = "1.1.0"
) -> dict:
    return {
        "schema": "noruct.blueprint-delta.v1",
        "blueprint_id": "repository_researcher",
        "base_version": base_version,
        "candidate_version": candidate_version,
        "kind": "CAPABILITY_ALIAS_ADD",
        "alias": "repository_inspection",
        "target_capability": "repository_analysis",
        "rollback": {
            "kind": "CAPABILITY_ALIAS_REMOVE",
            "alias": "repository_inspection",
        },
    }


def evolution_artifact(
    *,
    version: str = "1.0.0",
    channel: str = "STABLE",
    artifact_id: str = "repository_skill",
    required_capabilities: list[str] | None = None,
) -> dict:
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": artifact_id,
        "version": version,
        "kind": "SKILL_PACKAGE",
        "release_channel": channel,
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": required_capabilities or ["workspace_read"],
        },
        "content": {
            "skill_key": "repository_analysis",
            "applies_to": ["repository_analysis"],
            "steps": ["Inspect workspace shape before choosing a workflow"],
            "required_capabilities": [],
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {"suite_id": "repository_suite", "version": "1.0.0", "digest": "b" * 64},
            "metrics": {"quality_score": 0.8, "safety_score": 1.0, "cost_bucket": "LOW", "latency_bucket": "LOW"},
            "limitations": [],
        },
    }


class EvolutionNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = EvolutionStore(self.root / "evolution.db")
        self.service = EvolutionNetworkService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _write_json(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _register_local_derivative(
        self,
        manifest: dict,
        *,
        base_version: str = "1.0.0",
        service: EvolutionNetworkService | None = None,
    ) -> dict:
        selected = self.service if service is None else service
        return dict(
            selected.register_local_derived_artifact_manifest(
                manifest,
                base_artifact_id=str(manifest["artifact_id"]),
                base_version=base_version,
                producer="test_deriver",
                evidence_digest=content_digest(
                    {
                        "artifact_id": manifest["artifact_id"],
                        "version": manifest["version"],
                        "fixture": "local-derivation",
                    }
                ),
            )
        )

    def _record_passing_shadow(
        self,
        *,
        scope_key: str = "employee_researcher",
        artifact_id: str = "repository_skill",
        candidate_version: str = "1.1.0",
        service: EvolutionNetworkService | None = None,
    ) -> dict:
        selected = self.service if service is None else service
        return dict(
            selected.record_artifact_shadow_evaluation(
                scope_key=scope_key,
                artifact_id=artifact_id,
                candidate_version=candidate_version,
                fixture_kind="SYNTHETIC",
                fixture_id="repository_shadow_fixture",
                fixture_version="1.0.0",
                fixture_digest=content_digest(
                    {
                        "fixture": "repository_shadow_fixture",
                        "version": "1.0.0",
                    }
                ),
                baseline_quality=0.8,
                candidate_quality=0.85,
                baseline_safety=1.0,
                candidate_safety=1.0,
                baseline_cost=1.0,
                candidate_cost=1.0,
                cost_ceiling=1.1,
                terminal_state="COMPLETE",
                complete=True,
                attempt_count=1,
                failure_count=0,
                failure_history_digest=content_digest([]),
            )
        )

    def _consent_id(self) -> str:
        record = self.service.grant_consent(
            purpose="BLUEPRINT_IMPROVEMENT",
            allowed_reuse="EVALUATE_AND_PROMOTE_BLUEPRINT",
            retention_days=30,
            authority="ORGANIZATION_OWNER",
        )
        return str(record["consent_id"])

    def test_default_is_local_and_network_transport_is_disabled(self) -> None:
        status = self.service.status()
        self.assertEqual(status["network_transport"], "DISABLED")
        self.assertEqual(status["remote_worker_execution"], "DISABLED")
        self.assertEqual(status["active_consents"], 0)
        sovereignty = status["local_sovereignty"]
        self.assertEqual(sovereignty["mode"], "LOCAL_SOVEREIGN")
        self.assertFalse(sovereignty["company_runtime_requires_consent"])
        self.assertEqual(sovereignty["company_runtime_state_authority"], "LOCAL_CUSTOMER")
        self.assertFalse(sovereignty["network_request_performed"])

    def test_contributor_consent_never_changes_local_company_runtime_authority(self) -> None:
        self._consent_id()
        status = self.service.status()
        sovereignty = status["local_sovereignty"]
        self.assertEqual(sovereignty["mode"], "LOCAL_CONTRIBUTOR_PREVIEW")
        self.assertFalse(sovereignty["company_runtime_requires_consent"])
        self.assertEqual(sovereignty["company_runtime_state_authority"], "LOCAL_CUSTOMER")
        self.assertEqual(sovereignty["raw_workspace_upload"], "PROHIBITED")
        self.assertEqual(sovereignty["shared_blueprint_effect"], "CATALOG_ONLY")

    def test_versioned_skill_install_activate_rollback_and_job_pin_are_local(self) -> None:
        source_path = self._write_json(
            "skill-100.json", evolution_artifact(version="1.0.0")
        )
        source_bytes = source_path.read_bytes()
        first = self.service.register_artifact_file(
            source_path
        )
        self.assertEqual(first["release_channel"], "STABLE")
        self.assertEqual(self.service.list_active_artifacts("employee_researcher"), ())
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        initial = self.service.activate_artifact(
            scope_key="employee_researcher", artifact_id="repository_skill", version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self.assertEqual(initial["status"], "ACTIVE")

        second = self._register_local_derivative(
            evolution_artifact(version="1.1.0")
        )
        self._record_passing_shadow()
        self.assertEqual(first["origin_kind"], "USER_IMPORTED")
        self.assertEqual(second["origin_kind"], "LOCAL_DERIVED")
        self.assertEqual(
            self.service.apply_artifact_update_subscriptions(
                scope_key="employee_researcher", allowed_capabilities=("workspace_read",)
            ),
            (),
        )
        pinned = self.service.pin_active_artifacts_for_job(
            job_id="job_version_pin", scope_key="employee_researcher"
        )
        self.assertEqual(pinned[0]["version"], "1.0.0")

        subscription = self.service.set_artifact_update_subscription(
            scope_key="employee_researcher", kind="SKILL_PACKAGE", artifact_id="repository_skill",
            mode="TRACK_STABLE",
        )
        update = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher", allowed_capabilities=("workspace_read",)
        )
        self.assertEqual(subscription["mode"], "TRACK_STABLE")
        self.assertEqual(update[0]["decision"], "ACTIVATED_NEXT_JOB")
        self.assertEqual(self.service.list_active_artifacts("employee_researcher")[0]["version"], "1.1.0")
        self.assertEqual(
            self.service.pin_active_artifacts_for_job(
                job_id="job_version_pin", scope_key="employee_researcher"
            )[0]["version"],
            "1.0.0",
        )
        restored = self.service.rollback_artifact(scope_key="employee_researcher", kind="SKILL_PACKAGE")
        self.assertEqual(restored["version"], "1.0.0")
        self.assertEqual(source_path.read_bytes(), source_bytes)

    def test_tracker_requires_shadow_compatible_contract_before_future_job_promotion(self) -> None:
        self.service.register_artifact_file(
            self._write_json("shadow-compatible-100.json", evolution_artifact(version="1.0.0"))
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher",
            artifact_id="repository_skill",
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        candidate = evolution_artifact(version="1.1.0")
        candidate["compatibility"]["runtime_contract"] = "noruct_v2"
        self._register_local_derivative(candidate)
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher",
            kind="SKILL_PACKAGE",
            artifact_id="repository_skill",
            mode="TRACK_STABLE",
        )

        outcome = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher",
            allowed_capabilities=("workspace_read",),
        )

        self.assertEqual(
            outcome[0]["decision"],
            "STAGED_PENDING_SHADOW_EVALUATION",
        )
        self.assertEqual(outcome[0]["shadow_state"], "CONTRACT_MISMATCH")
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.0.0",
        )

    def test_direct_local_file_is_user_imported_and_never_automatic(self) -> None:
        base = self.service.register_artifact_file(
            self._write_json("user-import-100.json", evolution_artifact(version="1.0.0"))
        )
        candidate = self.service.register_artifact_file(
            self._write_json("user-import-110.json", evolution_artifact(version="1.1.0"))
        )
        self.assertEqual(base["origin_kind"], "USER_IMPORTED")
        self.assertEqual(candidate["origin_kind"], "USER_IMPORTED")
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher",
            artifact_id="repository_skill",
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher",
            kind="SKILL_PACKAGE",
            artifact_id="repository_skill",
            mode="TRACK_STABLE",
        )

        outcome = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher",
            allowed_capabilities=("workspace_read",),
        )

        self.assertEqual(
            outcome[0]["decision"],
            "NON_LOCAL_DERIVED_REQUIRES_EXPLICIT_ACTIVATION",
        )
        self.assertEqual(outcome[0]["origin_kinds"], ("USER_IMPORTED",))
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.0.0",
        )

    def test_artifact_origin_is_immutable_for_an_existing_version(self) -> None:
        manifest = evolution_artifact(version="1.0.0")
        self.service.register_artifact_manifest(manifest)

        with self.assertRaisesRegex(ValueError, "origin is immutable"):
            self.service.register_artifact_manifest(
                manifest, ingress="MCP_POLICY_REGISTRATION"
            )

    def test_tampered_local_derivation_origin_fails_closed(self) -> None:
        self.service.register_artifact_manifest(evolution_artifact(version="1.0.0"))
        self._register_local_derivative(evolution_artifact(version="1.1.0"))
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher",
            artifact_id="repository_skill",
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher",
            kind="SKILL_PACKAGE",
            artifact_id="repository_skill",
            mode="TRACK_STABLE",
        )
        candidate = self.store.get_artifact_version("repository_skill", "1.1.0")
        metadata = dict(candidate["origin_metadata"])
        metadata["base_manifest_digest"] = "f" * 64
        with self.store._transaction() as connection:  # noqa: SLF001 - tamper regression
            connection.execute(
                """UPDATE evolution_artifact_versions
                      SET origin_metadata_json = ?
                    WHERE artifact_id = 'repository_skill' AND version = '1.1.0'""",
                (canonical_json(metadata),),
            )

        outcome = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher",
            allowed_capabilities=("workspace_read",),
        )

        self.assertEqual(
            outcome[0]["decision"], "LOCAL_DERIVATION_PROVENANCE_INVALID"
        )
        self.assertEqual(
            self.service.list_active_artifacts("employee_researcher")[0]["version"],
            "1.0.0",
        )

    def test_runtime_artifact_adapter_projects_only_frozen_applicable_skills(self) -> None:
        self.service.register_artifact_file(
            self._write_json("runtime-skill.json", evolution_artifact())
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="repository_skill",
            version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        roster = (
            EmployeeRecord("employee-repository-analyst", "Repository Analyst", ("repository_analysis",)),
            EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),
        )
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-runtime-adapter",
            scope_keys=runtime_artifact_scopes(roster),
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-runtime-adapter", roster=roster, pins=pins
        )
        analyst_skills = resolution.employee_skills["employee-repository-analyst"]
        self.assertEqual(len(analyst_skills), 1)
        self.assertEqual(analyst_skills[0].revision, "1.0.0")
        self.assertIn("Inspect workspace shape", analyst_skills[0].content)
        self.assertEqual(resolution.employee_skills["employee-generalist"], ())
        self.assertEqual(
            resolution.effects[0]["decision"],
            "PROJECTED_TO_EMPLOYEE_SKILL_SNAPSHOT",
        )

    def test_registered_workflow_playbook_projects_only_to_a_compatible_compiler_prior(self) -> None:
        artifact = evolution_artifact(
            artifact_id="repository_linear_playbook",
            required_capabilities=["repository_analysis"],
        )
        artifact["kind"] = "WORKFLOW_PLAYBOOK"
        artifact["content"] = {
            "workflow_shape": ["repository_analysis"],
            "reviewer_policy": "none",
            "required_capabilities": [],
            "adapter_reference": "compiler_linear_playbook_v1",
            "execution_profile": "read_only",
        }
        self.service.register_artifact_manifest(artifact)
        self.service.stage_artifact("repository_linear_playbook", "1.0.0")
        self.service.install_artifact("repository_linear_playbook", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="repository_linear_playbook",
            version="1.0.0",
            allowed_capabilities=("repository_analysis",),
        )
        roster = (
            EmployeeRecord(
                "employee-repository-analyst",
                "Repository Analyst",
                ("repository_analysis",),
            ),
        )
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-workflow-adapter", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-workflow-adapter", roster=roster, pins=pins
        )
        priors, effects = project_network_workflow_priors(
            resolution,
            execution_profile=CompilerExecutionProfile.READ_ONLY,
            available_capabilities=("repository_analysis",),
        )
        self.assertEqual(len(priors), 1)
        self.assertEqual(priors[0].tasks[0].required_capabilities, ("repository_analysis",))
        self.assertIn(
            "PROJECTED_WORKFLOW_ADAPTER_COMPILER_PRIOR",
            [item["decision"] for item in effects],
        )
        blocked, blocked_effects = project_network_workflow_priors(
            resolution,
            execution_profile=CompilerExecutionProfile.HOST_DIRECT,
            available_capabilities=("repository_analysis",),
        )
        self.assertEqual(blocked, ())
        self.assertIn(
            "IGNORED_WORKFLOW_ADAPTER_EXECUTION_PROFILE_MISMATCH",
            [item["decision"] for item in blocked_effects],
        )

    def test_registered_benchmark_and_evaluator_pair_is_pinned_without_publisher_code(self) -> None:
        benchmark = evolution_artifact(
            artifact_id="capability_delta_suite",
            required_capabilities=[],
        )
        benchmark["kind"] = "BENCHMARK_SUITE"
        benchmark["compatibility"]["required_capabilities"] = []
        benchmark["content"] = {
            "fixture_ids": [
                "public_synthetic_capability_alias_v1",
                "public_synthetic_capability_alias_safety_v1",
            ],
            "scorer": "capability_route_delta",
            "required_capabilities": [],
            "adapter_reference": "blueprint_delta_holdout_suite_v1",
        }
        evaluator = evolution_artifact(
            artifact_id="capability_delta_evaluator",
            required_capabilities=[],
        )
        evaluator["kind"] = "EVALUATOR_PROFILE"
        evaluator["compatibility"]["required_capabilities"] = []
        evaluator["content"] = {
            "evaluator": "capability_route_delta",
            "threshold_profile": "manual_review_only",
            "required_capabilities": [],
            "adapter_reference": "blueprint_delta_holdout_suite_v1",
        }
        for artifact in (benchmark, evaluator):
            self.service.register_artifact_manifest(artifact)
            self.service.stage_artifact(str(artifact["artifact_id"]), "1.0.0")
            self.service.install_artifact(str(artifact["artifact_id"]), "1.0.0")
            self.service.activate_artifact(
                scope_key="company_default",
                artifact_id=str(artifact["artifact_id"]),
                version="1.0.0",
                allowed_capabilities=(),
            )
        roster = (EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),)
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-evaluator-adapter", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-evaluator-adapter", roster=roster, pins=pins
        )
        self.assertEqual(len(resolution.evaluation_adapters), 1)
        self.assertEqual(
            resolution.evaluation_adapters[0].adapter_reference,
            "blueprint_delta_holdout_suite_v1",
        )
        self.assertIn(
            "PROJECTED_BENCHMARK_EVALUATOR_ADAPTER",
            [item["decision"] for item in resolution.effects],
        )

    def test_receipt_bound_managed_skill_package_projects_only_reviewed_semantic_steps(self) -> None:
        raw_instruction = "Never put this raw managed SKILL.md content into the Artifact"
        artifact = build_managed_skill_artifact(
            artifact_id="managed_repository_review",
            version="1.0.0",
            skill_key="repository_review",
            applies_to=("repository_analysis",),
            steps=("Inspect repository structure before proposing a change.",),
            required_capabilities=("repository_analysis",),
            receipt={"tree_sha256": "b" * 64},
        )
        self.assertNotIn(raw_instruction, json.dumps(artifact))
        self.service.register_artifact_manifest(artifact)
        self.service.stage_artifact("managed_repository_review", "1.0.0")
        self.service.install_artifact("managed_repository_review", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="managed_repository_review",
            version="1.0.0",
            allowed_capabilities=("repository_analysis",),
        )
        roster = (
            EmployeeRecord("employee-repository-analyst", "Repository Analyst", ("repository_analysis",)),
            EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),
        )
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-managed-skill", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-managed-skill", roster=roster, pins=pins
        )
        rendered = resolution.employee_skills["employee-repository-analyst"][0].content
        self.assertIn("Inspect repository structure", rendered)
        self.assertIn("b" * 64, rendered)
        self.assertNotIn(raw_instruction, rendered)
        self.assertEqual(resolution.employee_skills["employee-generalist"], ())

    def test_mcp_policy_package_pins_existing_local_policy_without_storing_it(self) -> None:
        policy = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=(),
            tool_names=("read_issue",),
            profile="repository-context",
        )
        manifest = build_mcp_policy_artifact(
            config=policy,
            artifact_id="repository_mcp_policy",
            version="1.0.0",
        )
        self.assertEqual(manifest["release_channel"], "EXPERIMENTAL")
        self.assertNotIn(str(policy.python_command), json.dumps(manifest))
        self.assertNotIn("read_issue", json.dumps(manifest))
        self.service.register_artifact_manifest(manifest)
        self.service.stage_artifact("repository_mcp_policy", "1.0.0")
        self.service.install_artifact("repository_mcp_policy", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="repository_mcp_policy",
            version="1.0.0",
            allowed_capabilities=("external_read",),
        )
        roster = (EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),)
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-mcp-policy", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-mcp-policy", roster=roster, pins=pins
        )
        permitted, decision = _mcp_policy_for_frozen_artifacts(policy, resolution)
        self.assertIs(permitted, policy)
        self.assertEqual(decision, "MCP_POLICY_PACKAGE_BOUND")
        drifted = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=("changed",),
            tool_names=("read_issue",),
            profile="repository-context",
        )
        refused, drift_decision = _mcp_policy_for_frozen_artifacts(drifted, resolution)
        self.assertIsNone(refused)
        self.assertEqual(drift_decision, "MCP_POLICY_PACKAGE_BINDING_MISMATCH")
        self.assertIn(
            "PROJECTED_MCP_POLICY_BINDING",
            [item["decision"] for item in resolution.effects],
        )

    def test_mcp_policy_package_can_bind_only_one_profile_from_a_local_set(self) -> None:
        repository = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=(),
            tool_names=("read_repository",),
            profile="repository-context",
        )
        issue = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=("issue",),
            tool_names=("read_issue",),
            profile="issue-context",
        )
        policy = McpReadOnlyConfigSet((repository, issue))
        with self.assertRaisesRegex(ValueError, "needs --profile"):
            build_mcp_policy_artifact(
                config=policy, artifact_id="ambiguous_mcp_policy", version="1.0.0"
            )
        manifest = build_mcp_policy_artifact(
            config=policy,
            artifact_id="repository_mcp_policy_only",
            version="1.0.0",
            profile="repository-context",
        )
        self.assertNotIn("repository-context", json.dumps(manifest))
        self.assertNotIn("read_repository", json.dumps(manifest))
        self.service.register_artifact_manifest(manifest)
        self.service.stage_artifact("repository_mcp_policy_only", "1.0.0")
        self.service.install_artifact("repository_mcp_policy_only", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="repository_mcp_policy_only",
            version="1.0.0",
            allowed_capabilities=("external_read",),
        )
        roster = (EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),)
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-mcp-profile-policy", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-mcp-profile-policy", roster=roster, pins=pins
        )
        permitted, decision = _mcp_policy_for_frozen_artifacts(policy, resolution)
        self.assertIsInstance(permitted, McpReadOnlyConfig)
        assert isinstance(permitted, McpReadOnlyConfig)
        self.assertEqual(permitted.profile, "repository-context")
        self.assertEqual(decision, "MCP_POLICY_PACKAGE_PROFILE_SUBSET_BOUND")

    def test_existing_whole_multi_profile_policy_package_remains_compatible(self) -> None:
        repository = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=(),
            tool_names=("read_repository",),
            profile="repository-context",
        )
        issue = McpReadOnlyConfig(
            python_command=Path(sys.executable).resolve(),
            server_command=Path(sys.executable).resolve(),
            server_args=("issue",),
            tool_names=("read_issue",),
            profile="issue-context",
        )
        policy = McpReadOnlyConfigSet((repository, issue))
        legacy_manifest = {
            **build_mcp_policy_artifact(
                config=repository,
                artifact_id="whole_multi_profile_legacy",
                version="1.0.0",
            ),
            "content": {
                **build_mcp_policy_artifact(
                    config=repository,
                    artifact_id="whole_multi_profile_legacy",
                    version="1.0.0",
                )["content"],
                "binding_digest": mcp_session_binding_digest(policy),
            },
        }
        self.service.register_artifact_manifest(legacy_manifest)
        self.service.stage_artifact("whole_multi_profile_legacy", "1.0.0")
        self.service.install_artifact("whole_multi_profile_legacy", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default",
            artifact_id="whole_multi_profile_legacy",
            version="1.0.0",
            allowed_capabilities=("external_read",),
        )
        roster = (EmployeeRecord("employee-generalist", "Generalist", ("conversation",)),)
        pins = self.store.pin_active_artifacts_for_runtime_job(
            job_id="job-mcp-legacy-policy", scope_keys=runtime_artifact_scopes(roster)
        )
        resolution = EvolutionRuntimeArtifactAdapter(self.store).resolve(
            job_id="job-mcp-legacy-policy", roster=roster, pins=pins
        )
        permitted, decision = _mcp_policy_for_frozen_artifacts(policy, resolution)
        self.assertIs(permitted, policy)
        self.assertEqual(decision, "MCP_POLICY_PACKAGE_BOUND")

    def test_empty_runtime_artifact_snapshot_stays_empty_for_the_same_job(self) -> None:
        scopes = ("company_default", "employee-repository-analyst")
        self.assertEqual(
            self.store.pin_active_artifacts_for_runtime_job(
                job_id="job-empty-runtime-snapshot", scope_keys=scopes
            ),
            (),
        )
        self.service.register_artifact_file(
            self._write_json("late-runtime-skill.json", evolution_artifact())
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="company_default", artifact_id="repository_skill", version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self.assertEqual(
            self.store.pin_active_artifacts_for_runtime_job(
                job_id="job-empty-runtime-snapshot", scope_keys=scopes
            ),
            (),
        )

    def test_active_shared_manager_skill_is_injected_into_next_direct_company_job(self) -> None:
        artifact = evolution_artifact()
        # DIRECT now belongs to the persistent Executive Manager.  A shared
        # procedure is projected only when its declared audience exactly
        # matches a frozen employee id, role, or capability; ``conversation``
        # is deliberately not an implicit alias for the Manager's separate
        # ``company_management`` contract.
        artifact["content"]["applies_to"] = ["company_management"]
        artifact["content"]["steps"] = ["Answer the company user directly and concisely."]
        artifact_path = self._write_json("conversation-skill.json", artifact)
        # The CLI deliberately pairs runtime.db with runtime.evolution.db.
        # Seed that exact local catalog instead of the independent fixture DB.
        with EvolutionStore(self.root / "runtime.evolution.db") as runtime_evolution:
            runtime_service = EvolutionNetworkService(runtime_evolution)
            runtime_service.register_artifact_file(artifact_path)
            runtime_service.stage_artifact("repository_skill", "1.0.0")
            runtime_service.install_artifact("repository_skill", "1.0.0")
            runtime_service.activate_artifact(
                scope_key="company_default",
                artifact_id="repository_skill",
                version="1.0.0",
                allowed_capabilities=("workspace_read",),
            )
        provider = ScriptedModelProvider(
            [
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary="Hello from the company.",
                        acceptance_evidence=("direct response",),
                    )
                )
            ]
        )
        config = RunCommandConfig(
            goal="hello",
            workspace=self.root,
            state_path=self.root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="scripted",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=5.0,
            permission_mode="read-only",
            run_limits=RunLimits(),
        )
        result = asyncio.run(run_goal(config, provider, route=InputRoute.CONVERSATION))
        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(provider.call_count, 1)
        rendered_messages = "\n".join(str(message.content) for message in provider.requests[0].messages)
        self.assertIn("employee-executive-manager", rendered_messages)
        self.assertIn("Answer the company user directly and concisely.", rendered_messages)

    def test_only_user_selected_always_approve_advances_a_local_derivative_for_next_job(self) -> None:
        evolution_path = self.root / "runtime.evolution.db"
        with EvolutionStore(evolution_path) as runtime_evolution:
            runtime_service = EvolutionNetworkService(runtime_evolution)
            base = evolution_artifact(
                version="1.0.0",
                required_capabilities=["company_management"],
            )
            base["content"]["applies_to"] = ["company_management"]
            runtime_service.register_artifact_manifest(base)
            candidate = evolution_artifact(
                version="1.1.0",
                required_capabilities=["company_management"],
            )
            candidate["content"]["applies_to"] = ["company_management"]
            self._register_local_derivative(candidate, service=runtime_service)
            runtime_service.stage_artifact("repository_skill", "1.0.0")
            runtime_service.install_artifact("repository_skill", "1.0.0")
            runtime_service.activate_artifact(
                scope_key="company_default",
                artifact_id="repository_skill",
                version="1.0.0",
                allowed_capabilities=("company_management",),
            )
            self._record_passing_shadow(
                scope_key="company_default",
                service=runtime_service,
            )

        config = RunCommandConfig(
            goal="hello",
            workspace=self.root,
            state_path=self.root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="scripted",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=5.0,
            permission_mode="read-only",
            run_limits=RunLimits(),
        )

        def provider() -> ScriptedModelProvider:
            return ScriptedModelProvider(
                [
                    ModelResponse(
                        completion=CompletionEnvelope(
                            summary="Hello from the company.",
                            acceptance_evidence=("direct response",),
                        )
                    )
                ]
            )

        asyncio.run(
            run_goal(
                config,
                provider(),
                route=InputRoute.CONVERSATION,
                job_id="job-before-always-approve",
            )
        )
        with EvolutionStore(evolution_path) as runtime_evolution:
            self.assertEqual(
                runtime_evolution.list_active_artifact_activations(
                    "company_default"
                )[0]["version"],
                "1.0.0",
            )

        with CompanyStateStore(config.state_path) as company_store:
            company_store.set_evolution_autonomy_mode(
                EvolutionAutonomyMode.ALWAYS_APPROVE,
                actor="user:test",
            )

        asyncio.run(
            run_goal(
                config,
                provider(),
                route=InputRoute.CONVERSATION,
                job_id="job-after-always-approve",
            )
        )
        with EvolutionStore(evolution_path) as runtime_evolution:
            self.assertEqual(
                runtime_evolution.list_active_artifact_activations(
                    "company_default"
                )[0]["version"],
                "1.1.0",
            )
            self.assertEqual(
                runtime_evolution.list_runtime_job_artifact_pins(
                    "job-before-always-approve"
                )[0]["version"],
                "1.0.0",
            )

    def test_tracker_stages_but_does_not_activate_a_release_that_expands_authority(self) -> None:
        self.service.register_artifact_file(
            self._write_json("authority-100.json", evolution_artifact(version="1.0.0"))
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher", artifact_id="repository_skill", version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self._register_local_derivative(
            evolution_artifact(
                version="1.1.0",
                required_capabilities=["workspace_read", "workspace_write"],
            )
        )
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher", kind="SKILL_PACKAGE", artifact_id="repository_skill",
            mode="TRACK_STABLE",
        )
        outcome = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher", allowed_capabilities=("workspace_read",)
        )
        self.assertEqual(
            outcome[0]["decision"], "STAGED_PENDING_SHADOW_EVALUATION"
        )
        self.assertEqual(outcome[0]["shadow_state"], "PERMISSION_EXPANSION")
        self.assertEqual(self.service.list_active_artifacts("employee_researcher")[0]["version"], "1.0.0")
        self.assertEqual(self.store.list_artifact_installations()[-1]["status"], "STAGED")

    def test_experimental_tracker_never_activates_without_a_local_confirmation(self) -> None:
        self.service.register_artifact_file(
            self._write_json("experimental-100.json", evolution_artifact(version="1.0.0"))
        )
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher", artifact_id="repository_skill", version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self._register_local_derivative(
            evolution_artifact(version="1.1.0", channel="EXPERIMENTAL")
        )
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher", kind="SKILL_PACKAGE", artifact_id="repository_skill",
            mode="TRACK_EXPERIMENTAL",
        )
        outcome = self.service.apply_artifact_update_subscriptions(
            scope_key="employee_researcher", allowed_capabilities=("workspace_read",)
        )
        self.assertEqual(outcome[0]["decision"], "STAGED_EXPERIMENTAL_REQUIRES_CONFIRMATION")
        self.assertEqual(self.service.list_active_artifacts("employee_researcher")[0]["version"], "1.0.0")

    def test_artifact_registry_bundle_is_digest_checked_and_loopback_fetch_is_explicit(self) -> None:
        self.service.register_artifact_file(
            self._write_json("bundle-skill.json", evolution_artifact(version="1.0.0"))
        )
        bundle = self.service.build_artifact_registry_bundle("noruct_fixture_registry")
        path = self._write_json("artifact-registry.json", bundle)
        self.assertEqual(
            EvolutionNetworkService.inspect_artifact_registry_bundle(path)["bundle_digest"],
            bundle["bundle_digest"],
        )
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            EvolutionNetworkService.fetch_artifact_registry_bundle("http://example.test/artifacts.json")

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            fetched = EvolutionNetworkService.fetch_artifact_registry_bundle(
                f"http://127.0.0.1:{server.server_port}/artifact-registry.json",
                allow_insecure_loopback=True,
            )
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(fetched["bundle_digest"], bundle["bundle_digest"])

    def test_artifact_registry_discovery_is_bounded_public_metadata_only(self) -> None:
        entry = {
            "registry_id": "fixture_registry",
            "bundle_digest": "a" * 64,
            "signature_digest": "b" * 64,
            "published_at": "2026-07-29T00:00:00+00:00",
        }

        class DiscoveryHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/artifact-registries":
                    self.send_error(404)
                    return
                body = json.dumps({
                    "schema": "noruct.public-evolution-artifact-registry-index.v1",
                    "registries": [entry],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), DiscoveryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            discovered = EvolutionNetworkService.discover_artifact_registries(
                f"http://127.0.0.1:{server.server_port}",
                allow_insecure_loopback=True,
            )
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(discovered, (entry,))
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            EvolutionNetworkService.discover_artifact_registries("http://example.test")

    def test_discovered_artifact_registry_fetch_binds_index_bundle_and_signature(self) -> None:
        bundle = build_artifact_registry_bundle((), registry_id="fixture_registry")
        signature = (
            b"-----BEGIN SSH SIGNATURE-----\n"
            b"fixture-only-not-a-trust-decision\n"
            b"-----END SSH SIGNATURE-----\n"
        )
        entry = {
            "registry_id": "fixture_registry",
            "bundle_digest": bundle["bundle_digest"],
            "signature_digest": hashlib.sha256(signature).hexdigest(),
            "published_at": "2026-07-29T00:00:00+00:00",
        }

        class RegistryHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/v1/artifact-registries":
                    self._send(
                        "application/json",
                        json.dumps(
                            {
                                "schema": "noruct.public-evolution-artifact-registry-index.v1",
                                "registries": [entry],
                            }
                        ).encode("utf-8"),
                    )
                elif self.path == "/v1/artifact-registries/fixture_registry/bundle":
                    self._send("application/json", json.dumps(bundle).encode("utf-8"))
                elif self.path == "/v1/artifact-registries/fixture_registry/signature":
                    self._send("application/ssh-signature", signature)
                else:
                    self.send_error(404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            pointer, fetched, fetched_signature = (
                EvolutionNetworkService.fetch_discovered_artifact_registry(
                    origin,
                    "fixture_registry",
                    allow_insecure_loopback=True,
                )
            )
            self.assertEqual(pointer, entry)
            self.assertEqual(fetched["bundle_digest"], bundle["bundle_digest"])
            self.assertEqual(fetched_signature, signature)
            entry["signature_digest"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "signature does not match"):
                EvolutionNetworkService.fetch_discovered_artifact_registry(
                    origin,
                    "fixture_registry",
                    allow_insecure_loopback=True,
                )
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_evolution_scores_share_one_cross_runtime_digest_contract(self) -> None:
        accepted = {
            0.0: "fae82f4376a888603e4ec2bd6dd2dd0303925680df34a702449a103682d9ae20",
            0.8: "7d3b5e39e07bfccf74c0834a86d0aeadfe9f6b17e9a67653120f2ebc22aa5c98",
            0.95: "450b032b670b8b26472086b4f7cb8b4d428dc59cbdad2839fd4b9b7de8c80753",
            1.0: "234a67f5316da0c18c1bcaf470b3055b876d25363fe706f549acc6797a4128de",
        }
        for score, expected_digest in accepted.items():
            with self.subTest(score=score):
                artifact = evolution_artifact(channel="EXPERIMENTAL")
                artifact["passport"]["metrics"]["quality_score"] = score
                artifact["passport"]["metrics"]["safety_score"] = score
                metrics = validate_evolution_artifact(artifact)["passport"]["metrics"]
                self.assertEqual(
                    content_digest(
                        {
                            "quality_score": metrics["quality_score"],
                            "safety_score": metrics["safety_score"],
                        }
                    ),
                    expected_digest,
                )

        for invalid_score in (1e-7, -0.0, 0.001):
            with self.subTest(invalid_score=invalid_score):
                artifact = evolution_artifact(channel="EXPERIMENTAL")
                artifact["passport"]["metrics"]["quality_score"] = invalid_score
                with self.assertRaisesRegex(ValueError, "0.01 steps"):
                    validate_evolution_artifact(artifact)

                invalid_capsule = capsule()
                invalid_capsule["outcome"]["quality_score"] = invalid_score
                with self.assertRaisesRegex(ValueError, "0.01 steps"):
                    validate_capsule(invalid_capsule)

                invalid_evaluation = {
                    "schema": "noruct.evolution-candidate-evaluation.v1",
                    "suite_id": "public_fixture",
                    "suite_version": "1.0.0",
                    "suite_digest": "f" * 64,
                    "evaluator_id": "offline_fixture",
                    "fixture_scope": "PUBLIC",
                    "quality_score": invalid_score,
                    "safety_score": 0.95,
                    "cost_bucket": "LOW",
                    "decision": "PASS",
                }
                with self.assertRaisesRegex(ValueError, "0.01 steps"):
                    EvolutionNetworkService.validate_candidate_evaluation(invalid_evaluation)

        hosted_capsule = {
            "schema": "noruct.learning-capsule.v1",
            "capability": "repository_analysis",
            "authority": "individual",
            "task_schema": {
                "domain": "software", "operation": "analyze",
                "input_fields": ["repository_shape"], "risk_level": "LOW",
            },
            "execution_summary": {
                "workflow_shape": ["solo"], "tool_classes": ["workspace_read"],
                "decision_count": 2, "redaction_applied": True,
            },
            "outcome": {
                "status": "SUCCEEDED", "quality_score": 1.0,
                "cost_bucket": "LOW", "evaluator_kind": "LOCAL_TEST",
                "metric_names": ["acceptance_passed"],
            },
        }
        hosted_evaluation = {
            "schema": "noruct.evolution-candidate-evaluation.v1",
            "suite_id": "public_fixture", "suite_version": "1.0.0",
            "suite_digest": "f" * 64, "evaluator_id": "offline_fixture",
            "fixture_scope": "PUBLIC", "quality_score": 1.0,
            "safety_score": 1.0, "cost_bucket": "LOW", "decision": "PASS",
        }
        self.assertEqual(
            content_digest(validate_capsule(hosted_capsule)),
            "0e5688a07b6977d848d8a4c0d2d40f51873ddb21efc511433ceacab6b410c60b",
        )
        self.assertEqual(
            content_digest(EvolutionNetworkService.validate_candidate_evaluation(hosted_evaluation)),
            "6e47c70de28c2749368d034780dfd2ec72ba5f5007e0a9737e766aec6845e9a7",
        )

        integer_scores = {"metrics": {"quality_score": 1, "safety_score": 1}}
        float_scores = {"metrics": {"quality_score": 1.0, "safety_score": 1.0}}
        self.assertEqual(
            canonical_evolution_json(integer_scores),
            '{"metrics":{"quality_score":1.0,"safety_score":1.0}}',
        )
        self.assertEqual(
            evolution_content_digest(integer_scores),
            evolution_content_digest(float_scores),
        )
        self.assertEqual(
            evolution_content_digest(integer_scores),
            "c07959fab012e40fe60203fa4b67fc875278c488680d3574ad8930ed2dc41eda",
        )
        for invalid_score in (1e-7, -0.0, 0.001):
            with self.subTest(canonical_invalid_score=invalid_score):
                with self.assertRaisesRegex(ValueError, "0.01 steps"):
                    canonical_evolution_json(
                        {"nested": [{"quality_score": invalid_score}]}
                    )

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").is_file(), "OpenSSH ssh-keygen is unavailable")
    def test_artifact_registry_fetch_stage_can_verify_a_detached_signature_without_a_local_signature_file(self) -> None:
        """The public signature endpoint is a transport, never a local trust root."""
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            EvolutionNetworkService.fetch_artifact_registry_signature("http://example.test/artifact-registry.sig")
        self.service.register_artifact_file(
            self._write_json("signed-bundle-skill.json", evolution_artifact(version="1.0.0"))
        )
        bundle = self.service.build_artifact_registry_bundle("noruct_fixture_registry")
        private_key = self.root / "artifact-registry-signer"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload_path = self.root / "artifact-registry-signing-payload"
        payload_path.write_bytes(EvolutionNetworkService.artifact_registry_bundle_signing_payload(bundle))
        subprocess.run(
            [
                "/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(private_key),
                "-n", "noruct-evolution-release-v1", str(payload_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        signature = Path(f"{payload_path}.sig").read_bytes()
        allowed_signers = self.root / "allowed-artifact-registry-signers"
        allowed_signers.write_text(
            "noruct_artifact_registry_operator "
            + private_key.with_suffix(".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        class SignatureHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/artifact-registry.sig":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("content-type", "application/ssh-signature")
                self.send_header("content-length", str(len(signature)))
                self.end_headers()
                self.wfile.write(signature)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SignatureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fetched_signature = EvolutionNetworkService.fetch_artifact_registry_signature(
                f"http://127.0.0.1:{server.server_port}/artifact-registry.sig",
                allow_insecure_loopback=True,
            )
            receipt = EvolutionNetworkService.verify_artifact_registry_bundle_signature_bytes(
                bundle,
                signature=fetched_signature,
                allowed_signers=allowed_signers,
                principal="noruct_artifact_registry_operator",
                ssh_keygen=Path("/usr/bin/ssh-keygen"),
            )
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(receipt["principal"], "noruct_artifact_registry_operator")
        self.assertEqual(receipt["signature_digest"], content_digest(signature.hex()))
        self.assertFalse(list(self.root.glob("noruct-evolution-signature-*.sig")))

    def test_approved_remote_snapshot_never_advances_an_opted_in_stable_tracker(self) -> None:
        self.service.register_artifact_file(self._write_json("tracked-100.json", evolution_artifact(version="1.0.0")))
        self.service.stage_artifact("repository_skill", "1.0.0")
        self.service.install_artifact("repository_skill", "1.0.0")
        self.service.activate_artifact(
            scope_key="employee_researcher", artifact_id="repository_skill", version="1.0.0",
            allowed_capabilities=("workspace_read",),
        )
        self.service.set_artifact_update_subscription(
            scope_key="employee_researcher", kind="SKILL_PACKAGE", artifact_id="repository_skill", mode="TRACK_STABLE"
        )
        allowed_signers = self.root / "tracked-allowed-signers"
        allowed_signers.write_text("fixture signer", encoding="utf-8")
        self.service.register_public_registry_signer(
            source_label="fixture_registry", allowed_signers=allowed_signers,
            principal="fixture_signer", operator_id="operator_fixture",
        )
        remote_bundle = build_artifact_registry_bundle(
            (evolution_artifact(version="1.1.0"),), registry_id="fixture_registry"
        )
        receipt = {
            "algorithm": "openssh-detached-signature",
            "principal": "fixture_signer",
            "payload_digest": content_digest(
                EvolutionNetworkService.artifact_registry_bundle_signing_payload(remote_bundle).decode("utf-8")
            ),
            "signature_digest": "b" * 64,
            "allowed_signers_digest": allowed_signers_digest(allowed_signers),
        }
        with patch(
            "dynamic_firm.evolution.signing.verify_openssh_signature_bytes",
            return_value=receipt,
        ):
            staged = self.service.stage_verified_artifact_registry_bundle(
                remote_bundle,
                source_label="fixture_registry",
                signature=b"fixture-signature",
                allowed_signers=allowed_signers,
                principal="fixture_signer",
                ssh_keygen=Path("/usr/bin/ssh-keygen"),
            )
        approved = self.service.review_staged_artifact_registry_snapshot(
            str(staged["snapshot_id"]), operator_id="operator_fixture", decision="APPROVE", reason="fixture review"
        )
        result = self.service.import_tracked_artifacts_from_reviewed_snapshot(
            snapshot_id=str(approved["snapshot_id"]), scope_key="employee_researcher",
            allowed_capabilities=("workspace_read",),
        )
        self.assertEqual(result["imported"][0]["version"], "1.1.0")
        self.assertEqual(
            result["updates"][0]["decision"],
            "NON_LOCAL_DERIVED_REQUIRES_EXPLICIT_ACTIVATION",
        )
        self.assertEqual(result["runtime_effect"], "NONE_REQUIRES_EXPLICIT_ACTIVATION")
        self.assertEqual(self.service.list_active_artifacts("employee_researcher")[0]["version"], "1.0.0")
        self.assertEqual(
            self.store.get_artifact_registry_import_provenance(
                "repository_skill", "1.1.0"
            )["snapshot_id"],
            approved["snapshot_id"],
        )
        self.assertEqual(result["imported"][0]["origin_kind"], "NETWORK_IMPORTED")

    def test_artifact_registry_stage_rejects_a_receipt_for_a_different_bundle(self) -> None:
        allowed_signers = self.root / "receipt-allowed-signers"
        allowed_signers.write_text("fixture signer", encoding="utf-8")
        self.service.register_public_registry_signer(
            source_label="receipt_fixture", allowed_signers=allowed_signers,
            principal="fixture_signer", operator_id="operator_fixture",
        )
        bundle = build_artifact_registry_bundle((evolution_artifact(version="1.0.0"),), registry_id="receipt_fixture")
        bad_receipt = {
            "algorithm": "openssh-detached-signature",
            "principal": "fixture_signer",
            "payload_digest": "a" * 64,
            "signature_digest": "b" * 64,
            "allowed_signers_digest": allowed_signers_digest(allowed_signers),
        }
        with patch(
            "dynamic_firm.evolution.signing.verify_openssh_signature_bytes",
            return_value=bad_receipt,
        ):
            with self.assertRaisesRegex(ValueError, "does not bind"):
                self.service.stage_verified_artifact_registry_bundle(
                    bundle,
                    source_label="receipt_fixture",
                    signature=b"fixture-signature",
                    allowed_signers=allowed_signers,
                    principal="fixture_signer",
                    ssh_keygen=Path("/usr/bin/ssh-keygen"),
                )

    def test_hosted_intake_is_explicit_but_network_worker_stays_fail_closed_without_external_release_evidence(self) -> None:
        gate = network_gate_status()
        self.assertEqual(gate.hosted_transport, "IMPLEMENTED_EXPLICIT_OPT_IN_NOT_AUTO_ACTIVATED")
        self.assertFalse(gate.release_authorized)
        preview = preview_network_worker("tenant_acme", "registry-release-example")
        self.assertEqual(preview["decision"], "DENIED_NETWORK_WORKER_DISABLED")
        self.assertFalse(preview["network_request_performed"])
        grant = preview_capability_grant("tenant_acme", "registry-release-example", "job_preview", ("read_redacted_context", "workspace_write"))
        self.assertEqual(grant.decision, "DENIED_HOSTED_GATE_CLOSED")
        self.assertEqual(grant.granted_capabilities, ())
        self.assertIn("workspace_write", grant.denied_capabilities)
        authorization = EvolutionNetworkService.hosted_release_authorization_preview()
        self.assertEqual(authorization["decision"], "REMOTE_WORKER_NOT_AUTHORIZABLE")
        self.assertEqual(
            authorization["network_worker"],
            "DEPLOYED_OPERATOR_CONTROL_PLANE_NOT_CUSTOMER_AUTHORIZED",
        )
        self.assertTrue(authorization["code_cannot_self_authorize"])

    def test_operator_enrollment_preview_is_merge_only_and_never_exposes_token(self) -> None:
        key = "NORUCT_TEST_REVIEWER_TOKEN"
        previous = os.environ.get(key)
        os.environ[key] = "test-reviewer-secret-value"
        try:
            preview = EvolutionNetworkService.operator_enrollment_preview(
                role="reviewer",
                token_env=key,
                identity="operator_reviewer",
            )
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self.assertEqual(preview["worker_secret_name"], "REVIEWER_TOKEN_SHA256_ALLOWLIST")
        self.assertFalse(preview["network_requested"])
        self.assertFalse(preview["worker_mutated"])
        self.assertFalse(preview["token_exposed"])
        self.assertNotIn("test-reviewer-secret-value", json.dumps(preview))
        fragment = preview["merge_only_allowlist_fragment"]
        self.assertIsInstance(fragment, dict)
        self.assertEqual(next(iter(fragment.values())), "operator_reviewer")

    def test_capsule_rejects_raw_prompt_and_never_queues_without_consent(self) -> None:
        invalid = capsule()
        invalid["prompt"] = "private user prompt"
        path = self._write_json("invalid.json", invalid)

        with self.assertRaisesRegex(ValueError, "unsupported field"):
            self.service.preview_capsule_file(path)

        with self.assertRaisesRegex(KeyError, "Unknown evolution consent"):
            self.store.create_capsule("not-a-consent", capsule())

    def test_v2_capsule_accepts_only_a_bounded_typed_blueprint_delta(self) -> None:
        preview = self.service.preview_capsule_file(
            self._write_json("capsule-proposal.json", capsule_with_proposal())
        )
        self.assertEqual(preview["sanitized_capsule"]["schema"], "noruct.learning-capsule.v2")
        self.assertEqual(preview["sanitized_capsule"]["proposal"]["kind"], "BLUEPRINT_DELTA")

        invalid = capsule_with_proposal()
        invalid["proposal"]["delta"]["target_capability"] = "workspace_write"
        # The schema permits an existing capability identifier, but it must not
        # turn a proposal into a raw/executable payload.
        invalid["proposal"]["script"] = "do not execute this"
        with self.assertRaisesRegex(ValueError, "unsupported field"):
            self.service.preview_capsule_file(self._write_json("invalid-proposal.json", invalid))

    def test_submit_then_withdraw_deletes_payload_and_preserves_only_receipt(self) -> None:
        record = self.service.submit_capsule_file(
            self._write_json("capsule.json", capsule()), self._consent_id()
        )
        self.assertEqual(record["status"], "QUEUED_LOCAL_ONLY")
        self.assertEqual(record["transport_state"], "DISABLED")
        self.assertIn("task_schema", record["payload"])

        withdrawn = self.service.withdraw_capsule(str(record["capsule_id"]))
        self.assertEqual(withdrawn["status"], "WITHDRAWN")
        self.assertNotIn("payload", withdrawn)
        self.assertTrue(withdrawn["withdrawn_at"])

    def test_local_retention_expiry_purges_payload_without_contacting_a_worker(self) -> None:
        consent_id = self._consent_id()
        queued = self.service.submit_capsule_file(
            self._write_json("retention-queued.json", capsule()), consent_id
        )
        hosted = self.service.submit_capsule_file(
            self._write_json("retention-hosted.json", capsule()), consent_id
        )
        # Model a locally persisted hosted receipt without exercising a
        # transport: expiry must scrub both local raw projections, while the
        # hosted receipt metadata remains available for audit only.
        self.store.record_hosted_capsule_submission(
            str(hosted["capsule_id"]),
            endpoint_origin="http://127.0.0.1:8787",
            contribution_id="contribution-retention-fixture",
            receipt_digest="a" * 64,
            submitted_at="2026-07-01T00:00:00+00:00",
        )
        with self.store._transaction() as connection:  # noqa: SLF001 - expiry fixture setup
            connection.execute(
                "UPDATE evolution_consents SET granted_at = '2000-01-01T00:00:00+00:00' WHERE consent_id = ?",
                (consent_id,),
            )
        result = self.store.purge_expired_local_capsules()
        self.assertEqual(result, {"expired_consents": 1, "purged_capsules": 2})
        self.assertEqual(self.store.get_consent(consent_id)["status"], "EXPIRED")
        self.assertEqual(
            self.store.get_capsule(str(queued["capsule_id"]))["status"],
            "EXPIRED_LOCAL_ONLY",
        )
        self.assertEqual(
            self.store.get_capsule(str(hosted["capsule_id"]))["status"],
            "EXPIRED_HOSTED_LOCAL",
        )
        self.assertNotIn("payload", self.store.get_capsule(str(queued["capsule_id"])))
        self.assertNotIn("payload", self.store.get_capsule(str(hosted["capsule_id"])))
        self.assertEqual(
            self.store.status()["retention_expired_local_capsules"], 2
        )

    def test_explicit_hosted_intake_and_remote_withdrawal_keep_token_out_of_state(self) -> None:
        _HostedIntakeHandler.records = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            local = self.service.submit_capsule_file(
                self._write_json("capsule-hosted.json", capsule()), self._consent_id()
            )
            submitted = self.service.submit_hosted_capsule(
                str(local["capsule_id"]), endpoint=endpoint, token="fixture-token",
                withdrawal_capability="b" * 64,
                allow_insecure_loopback=True,
            )
            self.assertEqual(submitted["capsule"]["status"], "SUBMITTED_HOSTED")
            self.assertEqual(submitted["capsule"]["transport_state"], "HOSTED_RECEIPT_RECORDED")
            receipt = self.store.hosted_capsule_receipt(str(local["capsule_id"]))
            self.assertEqual(receipt["endpoint_origin"], endpoint)
            withdrawal_capability = submitted["receipt"]["withdrawal_capability"]
            self.assertEqual(withdrawal_capability, "b" * 64)
            self.assertNotIn("fixture-token", json.dumps(self.store.export_payload()))
            self.assertNotIn(withdrawal_capability, json.dumps(self.store.export_payload()))
            with self.assertRaisesRegex(ValueError, "Hosted Capsule withdrawal"):
                self.service.withdraw_capsule(str(local["capsule_id"]))

            withdrawn = self.service.withdraw_hosted_capsule(
                str(local["capsule_id"]), endpoint=endpoint, token="fixture-token",
                withdrawal_capability=withdrawal_capability,
                allow_insecure_loopback=True,
            )
            self.assertEqual(withdrawn["capsule"]["status"], "WITHDRAWN")
            self.assertNotIn("payload", withdrawn["capsule"])
            self.assertEqual(
                self.store.hosted_capsule_receipt(str(local["capsule_id"]))["withdrawal_receipt_digest"],
                withdrawn["receipt"]["receipt_digest"],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_publisher_can_retire_public_artifact_distribution_without_local_runtime_effect(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            receipt = retire_artifact_registry(
                endpoint=f"http://127.0.0.1:{server.server_port}", token="publisher-token",
                registry_id="fixture_registry", reason_code="safety_defect",
                allow_insecure_loopback=True,
            )
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(receipt["status"], "RETIRED")
        self.assertEqual(receipt["registry_id"], "fixture_registry")

    def test_hosted_consent_withdrawal_is_unavailable_without_each_capsule_capability(self) -> None:
        _HostedIntakeHandler.records = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            consent_id = self._consent_id()
            local = self.service.submit_capsule_file(self._write_json("capsule-consent-hosted.json", capsule()), consent_id)
            self.service.submit_hosted_capsule(
                str(local["capsule_id"]),
                endpoint=endpoint,
                token="fixture-token",
                withdrawal_capability="b" * 64,
                allow_insecure_loopback=True,
            )
            with self.assertRaisesRegex(ValueError, "Hosted Capsule withdrawal"):
                self.service.withdraw_consent(consent_id)
            with self.assertRaisesRegex(ValueError, "capability-only intake"):
                self.service.withdraw_hosted_consent(
                    consent_id, endpoint=endpoint, token="fixture-token", allow_insecure_loopback=True,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_separated_operator_transport_is_explicit_and_evidence_bound(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        candidate_id = "candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        contribution_id = "contribution-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        evaluation = {
            "schema": "noruct.evolution-candidate-evaluation.v1",
            "suite_id": "public_fixture",
            "suite_version": "1.0.0",
            "suite_digest": "f" * 64,
            "evaluator_id": "offline_fixture",
            "fixture_scope": "PUBLIC",
            "quality_score": 0.9,
            "safety_score": 0.95,
            "cost_bucket": "LOW",
            "decision": "PASS",
        }
        try:
            finalized = finalize_pending_contribution(
                endpoint=endpoint, token="finalizer-token", contribution_id=contribution_id,
                allow_insecure_loopback=True,
            )
            self.assertTrue(finalized["proposal_recorded"])
            assembled = assemble_candidates(
                endpoint=endpoint, token="finalizer-token", allow_insecure_loopback=True,
            )
            self.assertEqual(assembled["finalized_proposal_groups"], 1)
            candidates = list_operator_candidates(
                endpoint=endpoint, token="finalizer-token", allow_insecure_loopback=True,
            )
            self.assertEqual(candidates["candidates"][0]["candidate_id"], candidate_id)
            receipt = record_candidate_evaluation(
                endpoint=endpoint, token="finalizer-token", candidate_id=candidate_id,
                evaluation=EvolutionNetworkService.validate_candidate_evaluation(evaluation),
                allow_insecure_loopback=True,
            )
            self.assertEqual(receipt["status"], "OPERATOR_REVIEW_READY")
            expiry = expire_pending_contributions(
                endpoint=endpoint, token="finalizer-token", allow_insecure_loopback=True,
            )
            self.assertEqual(expiry["expired_count"], 1)
            authorization = authorize_artifact_registry_publication(
                endpoint=endpoint, token="reviewer-token", registry_id="fixture_registry",
                bundle_digest="a" * 64,
                candidate_evidence_digests=("b" * 64,),
                evaluation_evidence_digests=("c" * 64,),
                artifact_manifest_digests=("d" * 64,),
                reviewer_id="reviewer_fixture", reason_code="accepted_evidence",
                allow_insecure_loopback=True,
            )
            publication = publish_artifact_registry(
                endpoint=endpoint, token="publisher-token", registry_id="fixture_registry",
                authorization_id=str(authorization["authorization_id"]),
                bundle={"registry_id": "fixture_registry", "bundle_digest": "a" * 64},
                signature=b"-----BEGIN SSH SIGNATURE-----\nfixture\n-----END SSH SIGNATURE-----\n",
                allow_insecure_loopback=True,
            )
            self.assertEqual(publication["status"], "ACTIVE")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_candidate_evaluation_rejects_non_scalar_or_non_public_inputs(self) -> None:
        invalid = {
            "schema": "noruct.evolution-candidate-evaluation.v1",
            "suite_id": "public_fixture",
            "suite_version": "1.0.0",
            "suite_digest": "f" * 64,
            "evaluator_id": "offline_fixture",
            "fixture_scope": "PRIVATE",
            "quality_score": 0.9,
            "safety_score": 0.95,
            "cost_bucket": "LOW",
            "decision": "PASS",
        }
        with self.assertRaisesRegex(ValueError, "fixture_scope"):
            EvolutionNetworkService.validate_candidate_evaluation(invalid)

    def test_publication_authorization_requires_both_candidate_and_evaluation_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "Candidate, evaluation, and Artifact evidence"):
            authorize_artifact_registry_publication(
                endpoint="http://127.0.0.1:1",
                token="reviewer-token",
                registry_id="fixture_registry",
                bundle_digest="a" * 64,
                candidate_evidence_digests=(),
                evaluation_evidence_digests=("c" * 64,),
                artifact_manifest_digests=("d" * 64,),
                reviewer_id="reviewer_fixture",
                reason_code="accepted_evidence",
                allow_insecure_loopback=True,
            )

    def test_cli_lists_finalizer_candidates_only_after_explicit_confirmation(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        try:
            output = io.StringIO(); error = io.StringIO()
            with patch.dict(os.environ, {"NORUCT_EVOLUTION_FINALIZER_TOKEN": "finalizer-token"}):
                result = main(
                    [
                        "evolution", "network", "candidates", "--endpoint", endpoint,
                        "--allow-insecure-loopback", "--confirm", "--json",
                    ],
                    stdout=output, stderr=error,
                )
            self.assertEqual(result, EXIT_OK, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["candidates"][0]["status"], "EVALUATION_READY")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_authorizes_exact_artifact_registry_digest_with_reviewer_role(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HostedIntakeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        bundle_path = self._write_json(
            "authorization-bundle.json",
            build_artifact_registry_bundle(
                (evolution_artifact(),), registry_id="fixture_registry"
            ),
        )
        try:
            output = io.StringIO()
            error = io.StringIO()
            with patch.dict(os.environ, {"NORUCT_EVOLUTION_REVIEWER_TOKEN": "reviewer-token"}):
                result = main(
                    [
                        "evolution", "network", "authorize-artifact-registry",
                        "fixture_registry", str(bundle_path),
                        "--candidate-evidence-digest", "b" * 64,
                        "--evaluation-evidence-digest", "c" * 64,
                        "--reviewer-id", "reviewer_fixture",
                        "--reason-code", "accepted_evidence",
                        "--endpoint", endpoint,
                        "--allow-insecure-loopback", "--confirm", "--json",
                    ],
                    stdout=output,
                    stderr=error,
                )
            self.assertEqual(result, EXIT_OK, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "PENDING")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_consent_withdrawal_withdraws_all_queued_payloads(self) -> None:
        consent_id = self._consent_id()
        record = self.service.submit_capsule_file(
            self._write_json("capsule.json", capsule()), consent_id
        )

        consent = self.service.withdraw_consent(consent_id)
        self.assertEqual(consent["status"], "WITHDRAWN")
        capsule_record = self.store.get_capsule(str(record["capsule_id"]))
        self.assertEqual(capsule_record["status"], "WITHDRAWN")
        self.assertNotIn("payload", capsule_record)

    def test_blueprint_version_is_immutable_and_selection_never_changes_company_roster(self) -> None:
        first = self.service.import_blueprint_file(self._write_json("first.json", blueprint("1.0.0")))
        changed = blueprint("1.0.0")
        changed["capabilities"] = ["repository_analysis", "documentation"]
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.service.import_blueprint_file(self._write_json("changed.json", changed))

        self.service.import_blueprint_file(self._write_json("second.json", blueprint("1.1.0")))
        company_path = self.root / "company.db"
        with CompanyStateStore(company_path) as company:
            roster_revision = company.roster().revision
        selected = self.service.select_blueprint(str(first["blueprint_id"]), "1.0.0")
        self.assertEqual(selected["status"], "ACTIVE")
        self.service.select_blueprint(str(first["blueprint_id"]), "1.1.0")
        restored = self.service.rollback_selection("researcher")
        self.assertEqual(restored["version"], "1.0.0")
        with CompanyStateStore(company_path) as company:
            self.assertEqual(company.roster().revision, roster_revision)

    def test_aligned_synthetic_capsules_are_not_promoted_without_an_executable_delta(self) -> None:
        first = capsule()
        second = capsule()
        second["task_schema"]["operation"] = "review"
        second["outcome"]["evaluator_kind"] = "OFFLINE_FIXTURE"
        report = evaluate_blueprint_admission(blueprint(), (first, second))

        self.assertEqual(report.decision, BlueprintAdmissionDecision.NO_EXECUTABLE_DELTA)
        self.assertFalse(report.promotion_allowed)
        self.assertIn(
            "CAPSULE_SCHEMA_HAS_NO_EXECUTABLE_SKILL_OR_WORKFLOW_DELTA",
            report.blockers,
        )
        self.assertIn("NO_HOLDOUT_EXECUTION_OF_A_CANDIDATE_BLUEPRINT", report.blockers)

    def test_incompatible_capsule_is_rejected_before_any_quality_claim(self) -> None:
        incompatible = capsule()
        incompatible["capability"] = "documentation"
        report = evaluate_blueprint_admission(blueprint(), (incompatible,))

        self.assertEqual(report.decision, BlueprintAdmissionDecision.REJECTED)
        self.assertFalse(report.promotion_allowed)
        self.assertIn("CAPSULE_CAPABILITY_NOT_DECLARED_BY_BLUEPRINT", report.blockers)

    def test_reversible_delta_can_only_become_manual_review_eligible_after_holdout_gain(self) -> None:
        report = evaluate_blueprint_delta_holdout(blueprint(), capability_alias_delta())

        self.assertEqual(
            report.decision, BlueprintDeltaHoldoutDecision.ELIGIBLE_FOR_MANUAL_REVIEW
        )
        self.assertGreater(report.candidate_passed, report.baseline_passed)
        self.assertGreater(report.positive_case_gain, 0)
        self.assertEqual(report.negative_case_regression_count, 0)
        self.assertFalse(report.automatic_promotion_allowed)
        self.assertTrue(report.manual_review_eligible)
        self.assertIn("PUBLIC_SYNTHETIC_HOLDOUT_ONLY", report.blockers)

    def test_delta_base_mismatch_is_rejected_before_holdout_claim(self) -> None:
        report = evaluate_blueprint_delta_holdout(
            blueprint(), capability_alias_delta(base_version="0.9.0")
        )

        self.assertEqual(report.decision, BlueprintDeltaHoldoutDecision.REJECTED)
        self.assertFalse(report.manual_review_eligible)
        self.assertIn("DELTA_BASE_VERSION_DOES_NOT_MATCH_BASE", report.blockers)

    def test_holdout_suite_requires_distinct_safety_fixture_and_stays_review_only(self) -> None:
        report = evaluate_blueprint_delta_holdout_suite(blueprint(), capability_alias_delta())

        self.assertEqual(report.fixture_count, 2)
        self.assertEqual(len(set(report.fixture_digests)), 2)
        self.assertTrue(report.manual_review_eligible)
        self.assertFalse(report.automatic_promotion_allowed)

    def test_capsule_withdrawal_revokes_its_pending_release_candidate(self) -> None:
        capsule_record = self.service.submit_capsule_file(
            self._write_json("capsule.json", capsule()), self._consent_id()
        )
        candidate = self.service.create_release_candidate_files(
            self._write_json("blueprint.json", blueprint()),
            self._write_json("delta.json", capability_alias_delta()),
            (str(capsule_record["capsule_id"]),),
        )
        self.assertEqual(candidate["status"], "PENDING_REVIEW")

        self.service.withdraw_capsule(str(capsule_record["capsule_id"]))
        revoked = self.store.get_release_candidate(str(candidate["candidate_id"]))
        self.assertEqual(revoked["status"], "REVOKED")
        self.assertEqual(revoked["revocation_reason"], "CAPSULE_WITHDRAWN")

    def test_operator_approval_requires_signature_before_release_and_is_revocable(self) -> None:
        capsule_record = self.service.submit_capsule_file(
            self._write_json("capsule-review.json", capsule()), self._consent_id()
        )
        candidate = self.service.create_release_candidate_files(
            self._write_json("blueprint-review.json", blueprint()),
            self._write_json("delta-review.json", capability_alias_delta()),
            (str(capsule_record["capsule_id"]),),
        )
        approved = self.service.review_release_candidate(
            str(candidate["candidate_id"]),
            operator_id="operator_alex",
            decision="APPROVE",
            reason="public synthetic suite reviewed",
        )
        self.assertEqual(approved["status"], "APPROVED_PENDING_SIGNATURE")
        self.assertEqual(approved["reviews"][0]["decision"], "APPROVE")
        self.assertNotEqual(approved["status"], "RELEASED")
        signing_payload = self.service.release_candidate_signing_payload(
            str(candidate["candidate_id"])
        ).decode("utf-8")
        self.assertIn(str(candidate["candidate_id"]), signing_payload)
        self.assertIn("holdout_digest", signing_payload)

        self.service.withdraw_capsule(str(capsule_record["capsule_id"]))
        self.assertEqual(
            self.store.get_release_candidate(str(candidate["candidate_id"]))["status"], "REVOKED"
        )

    def test_only_signature_verified_candidate_can_publish_to_local_registry_without_tenant_adoption(self) -> None:
        blueprint_path = self._write_json("blueprint-publish.json", blueprint())
        self.service.import_blueprint_file(blueprint_path)
        capsule_record = self.service.submit_capsule_file(
            self._write_json("capsule-publish.json", capsule()), self._consent_id()
        )
        candidate = self.service.create_release_candidate_files(
            blueprint_path,
            self._write_json("delta-publish.json", capability_alias_delta()),
            (str(capsule_record["capsule_id"]),),
        )
        self.service.review_release_candidate(
            str(candidate["candidate_id"]), operator_id="operator_alex", decision="APPROVE", reason="reviewed"
        )
        self.store.record_verified_signature(
            str(candidate["candidate_id"]),
            {
                "algorithm": "openssh-detached-signature",
                "principal": "operator_alex",
                "payload_digest": "a" * 64,
                "signature_digest": "b" * 64,
                "allowed_signers_digest": "c" * 64,
            },
        )
        release = self.service.publish_release_candidate(str(candidate["candidate_id"]))
        self.assertEqual(release["status"], "PUBLISHED_LOCAL")
        self.assertEqual(release["manifest"]["version"], "1.1.0")
        self.assertEqual(release["manifest"]["capability_aliases"][0]["alias"], "repository_inspection")
        self.assertEqual(self.store.status()["active_selections"], 0)
        preview = self.service.preview_tenant_adoption("tenant_acme", str(release["release_id"]))
        self.assertEqual(preview["runtime_effect"], "NONE")
        adoption = self.service.adopt_registry_release("tenant_acme", str(release["release_id"]))
        self.assertEqual(adoption["status"], "ACTIVE")
        self.assertEqual(self.store.status()["active_selections"], 0)

        bundle = self.service.build_public_registry_bundle("noruct-public-fixtures")
        self.assertEqual(bundle["distribution"], "PUBLIC_READ_ONLY_NO_CAPSULE_INTAKE")
        self.assertEqual(bundle["releases"][0]["release_id"], release["release_id"])
        bundle_path = self._write_json("registry-bundle.json", bundle)
        inspected = EvolutionNetworkService.inspect_public_registry_bundle(bundle_path)
        self.assertEqual(inspected["bundle_digest"], bundle["bundle_digest"])
        signing_payload = EvolutionNetworkService.public_registry_bundle_signing_payload(inspected)
        self.assertIn(bundle["bundle_digest"].encode("utf-8"), signing_payload)

        receipt = {
            "algorithm": "openssh-detached-signature",
            "principal": "noruct_registry_operator",
            "payload_digest": content_digest(signing_payload.decode("utf-8")),
            "signature_digest": "e" * 64,
            "allowed_signers_digest": "f" * 64,
        }
        trust_root = self.store.register_registry_signer_trust_root(
            source_label="noruct-public-staging",
            signer_principal="noruct_registry_operator",
            allowed_signers_digest=receipt["allowed_signers_digest"],
            operator_id="operator_alex",
        )
        stage_arguments = {
            "source_label": "noruct-public-staging",
            "signature": b"fixture-signature",
            "allowed_signers": self.root / "fixture-allowed-signers",
            "principal": "noruct_registry_operator",
            "ssh_keygen": Path("/usr/bin/ssh-keygen"),
        }
        stage_arguments["allowed_signers"].write_text("fixture", encoding="utf-8")
        with patch(
            "dynamic_firm.evolution.signing.verify_openssh_signature_bytes",
            return_value=receipt,
        ):
            staged = self.service.stage_verified_public_registry_bundle(
                inspected, **stage_arguments
            )
        self.assertEqual(staged["status"], "STAGED_TRUSTED_NOT_ADOPTABLE")
        self.assertEqual(staged["runtime_effect"], "NONE")
        self.assertEqual(staged["tenant_adoption_effect"], "NONE")
        self.assertEqual(staged["releases"][0]["status"], "STAGED_TRUSTED_NOT_ADOPTABLE")
        with patch(
            "dynamic_firm.evolution.signing.verify_openssh_signature_bytes",
            return_value=receipt,
        ):
            duplicate = self.service.stage_verified_public_registry_bundle(
                inspected, **stage_arguments
            )
        self.assertEqual(duplicate["snapshot_id"], staged["snapshot_id"])
        self.assertEqual(len(self.service.list_staged_public_registry_bundles()), 1)
        self.assertEqual(self.store.status()["staged_registry_snapshots"], 1)
        compatibility = self.service.preview_staged_registry_compatibility(str(staged["snapshot_id"]))
        self.assertEqual(compatibility["decision"], "REQUIRES_OPERATOR_REVIEW")
        reviewed = self.service.review_staged_registry_snapshot(
            str(staged["snapshot_id"]), operator_id="operator_alex", decision="APPROVE", reason="fixture policy digest inspected"
        )
        self.assertEqual(reviewed["status"], "REVIEW_APPROVED_NOT_ADOPTABLE")
        self.assertEqual(reviewed["review"]["decision"], "APPROVE")
        tenant_preview = self.service.preview_remote_tenant_candidate("tenant_acme", str(staged["snapshot_id"]), str(release["release_id"]))
        self.assertEqual(tenant_preview["decision"], "REQUIRES_TENANT_CONFIRMATION")
        remote_candidate = self.service.propose_remote_tenant_candidate("tenant_acme", str(staged["snapshot_id"]), str(release["release_id"]), operator_id="operator_alex", reason="fixture tenant review")
        approved_candidate = self.service.resolve_remote_tenant_candidate(str(remote_candidate["candidate_id"]), operator_id="operator_alex", decision="APPROVE", reason="tenant accepts candidate")
        self.assertEqual(approved_candidate["status"], "TENANT_CANDIDATE_APPROVED_NOT_APPLIED")
        self.assertEqual(approved_candidate["runtime_effect"], "NONE")
        self.assertEqual(
            self.store.export_payload()["staged_registry_snapshots"][0]["snapshot_id"],
            staged["snapshot_id"],
        )
        revoked_root = self.store.revoke_registry_signer_trust_root(
            str(trust_root["trust_root_id"]), operator_id="operator_alex", reason="test rotation incident"
        )
        self.assertEqual(revoked_root["status"], "REVOKED")
        self.assertEqual(
            self.store.get_staged_registry_snapshot(str(staged["snapshot_id"]))["status"],
            "REVOKED_SIGNER_TRUST",
        )
        self.assertEqual(self.store.get_remote_tenant_candidate(str(remote_candidate["candidate_id"]))["status"], "REVOKED_SOURCE_TRUST")

        tampered = json.loads(bundle_path.read_text(encoding="utf-8"))
        tampered["releases"][0]["manifest"]["role"] = "operator"
        self._write_json("tampered-registry-bundle.json", tampered)
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            EvolutionNetworkService.inspect_public_registry_bundle(
                self.root / "tampered-registry-bundle.json"
            )

    def test_registry_fetch_requires_https_except_explicit_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            EvolutionNetworkService.fetch_public_registry_bundle("http://example.test/registry.json")

    def test_registry_fetch_allows_only_explicit_loopback_integration(self) -> None:
        bundle = self.service.build_public_registry_bundle("noruct-fixture-registry")
        self._write_json("registry.json", bundle)

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(self.root))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/registry.json"
            fetched = EvolutionNetworkService.fetch_public_registry_bundle(
                url, allow_insecure_loopback=True
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(fetched["bundle_digest"], bundle["bundle_digest"])

    @unittest.skipUnless(Path("/usr/bin/ssh-keygen").is_file(), "OpenSSH ssh-keygen is unavailable")
    def test_registry_bundle_signature_uses_external_user_managed_key(self) -> None:
        private_key = self.root / "registry-signer"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        bundle = self.service.build_public_registry_bundle("noruct-signature-fixture")
        payload_path = self.root / "registry-signing-payload"
        payload_path.write_bytes(
            EvolutionNetworkService.public_registry_bundle_signing_payload(bundle)
        )
        subprocess.run(
            [
                "/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(private_key),
                "-n", "noruct-evolution-release-v1", str(payload_path),
            ],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        allowed_signers = self.root / "allowed-signers"
        allowed_signers.write_text(
            "noruct_registry_operator " + private_key.with_suffix(".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        receipt = EvolutionNetworkService.verify_public_registry_bundle_signature(
            bundle,
            signature=Path(f"{payload_path}.sig"),
            allowed_signers=allowed_signers,
            principal="noruct_registry_operator",
            ssh_keygen=Path("/usr/bin/ssh-keygen"),
        )
        self.assertEqual(receipt["principal"], "noruct_registry_operator")


class EvolutionCliTests(unittest.TestCase):
    def test_cli_status_states_that_consent_does_not_gate_local_company_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            error = io.StringIO()
            result = main(
                ["evolution", "status", "--state", str(Path(directory) / "company.db"), "--json"],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(result, EXIT_OK, error.getvalue())
            sovereignty = json.loads(output.getvalue())["local_sovereignty"]
            self.assertEqual(sovereignty["mode"], "LOCAL_SOVEREIGN")
            self.assertFalse(sovereignty["company_runtime_requires_consent"])

    def test_cli_requires_confirmation_and_reports_local_only_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.db"
            capsule_path = root / "capsule.json"
            capsule_path.write_text(json.dumps(capsule()), encoding="utf-8")
            output = io.StringIO()
            error = io.StringIO()
            denied = main(
                [
                    "evolution", "consent", "grant", "--state", str(state),
                    "--retention-days", "30", "--authority", "ORGANIZATION_OWNER",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(denied, EXIT_INPUT)
            self.assertIn("require --confirm", error.getvalue())

            output = io.StringIO()
            error = io.StringIO()
            granted = main(
                [
                    "evolution", "consent", "grant", "--state", str(state),
                    "--retention-days", "30", "--authority", "ORGANIZATION_OWNER",
                    "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(granted, EXIT_OK, error.getvalue())
            consent_id = json.loads(output.getvalue())["consent_id"]

            output = io.StringIO()
            error = io.StringIO()
            submitted = main(
                [
                    "evolution", "capsule", "submit", str(capsule_path),
                    "--state", str(state), "--consent-id", consent_id, "--confirm", "--json",
                ],
                stdout=output,
                stderr=error,
            )
            self.assertEqual(submitted, EXIT_OK, error.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "QUEUED_LOCAL_ONLY")
            self.assertEqual(payload["transport_state"], "DISABLED")

    def test_cli_evaluate_reports_a_blocked_promotion_for_aligned_synthetic_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blueprint_path = root / "blueprint.json"
            first_path = root / "first.json"
            second_path = root / "second.json"
            blueprint_path.write_text(json.dumps(blueprint()), encoding="utf-8")
            first_path.write_text(json.dumps(capsule()), encoding="utf-8")
            second = capsule()
            second["task_schema"]["operation"] = "review"
            second["outcome"]["evaluator_kind"] = "OFFLINE_FIXTURE"
            second_path.write_text(json.dumps(second), encoding="utf-8")
            output = io.StringIO()
            error = io.StringIO()

            result = main(
                [
                    "evolution", "evaluate", str(blueprint_path), str(first_path), str(second_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )

            self.assertEqual(result, EXIT_OK, error.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["decision"], "NO_EXECUTABLE_DELTA")
            self.assertFalse(payload["promotion_allowed"])

    def test_cli_delta_holdout_never_mutates_or_auto_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blueprint_path = root / "blueprint.json"
            delta_path = root / "delta.json"
            blueprint_path.write_text(json.dumps(blueprint()), encoding="utf-8")
            delta_path.write_text(json.dumps(capability_alias_delta()), encoding="utf-8")
            output = io.StringIO()
            error = io.StringIO()

            result = main(
                [
                    "evolution", "delta", "holdout", str(blueprint_path), str(delta_path), "--json",
                ],
                stdout=output,
                stderr=error,
            )

            self.assertEqual(result, EXIT_OK, error.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["decision"], "ELIGIBLE_FOR_MANUAL_REVIEW")
            self.assertTrue(payload["manual_review_eligible"])
            self.assertFalse(payload["automatic_promotion_allowed"])

    def test_cli_local_file_registration_never_becomes_automatic_update_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.db"
            first_path = root / "skill-100.json"
            second_path = root / "skill-110.json"
            first_path.write_text(json.dumps(evolution_artifact(version="1.0.0")), encoding="utf-8")
            second_path.write_text(json.dumps(evolution_artifact(version="1.1.0")), encoding="utf-8")

            def run(arguments: list[str]) -> dict:
                output = io.StringIO(); error = io.StringIO()
                result = main(arguments + ["--state", str(state), "--json"], stdout=output, stderr=error)
                self.assertEqual(result, EXIT_OK, error.getvalue())
                return json.loads(output.getvalue())

            registered = run(["evolution", "artifact", "register", str(first_path), "--confirm"])
            self.assertEqual(registered["artifact_id"], "repository_skill")
            self.assertEqual(registered["origin_kind"], "USER_IMPORTED")
            run(["evolution", "artifact", "stage", "repository_skill", "1.0.0", "--confirm"])
            run(["evolution", "artifact", "install", "repository_skill", "1.0.0", "--confirm"])
            activated = run([
                "evolution", "artifact", "activate", "employee_researcher", "repository_skill", "1.0.0",
                "--allowed-capability", "workspace_read", "--confirm",
            ])
            self.assertEqual(activated["version"], "1.0.0")
            second = run(["evolution", "artifact", "register", str(second_path), "--confirm"])
            self.assertEqual(second["origin_kind"], "USER_IMPORTED")
            run([
                "evolution", "artifact", "subscribe", "employee_researcher", "SKILL_PACKAGE", "repository_skill",
                "--mode", "TRACK_STABLE", "--confirm",
            ])
            updates = run([
                "evolution", "artifact", "update", "employee_researcher",
                "--allowed-capability", "workspace_read", "--confirm",
            ])
            self.assertEqual(
                updates["updates"][0]["decision"],
                "NON_LOCAL_DERIVED_REQUIRES_EXPLICIT_ACTIVATION",
            )
            active = run([
                "evolution", "artifact", "active", "employee_researcher",
            ])
            self.assertEqual(active["active_artifacts"][0]["version"], "1.0.0")

            run(["evolution", "artifact", "stage", "repository_skill", "1.1.0", "--confirm"])
            run(["evolution", "artifact", "install", "repository_skill", "1.1.0", "--confirm"])
            explicit = run([
                "evolution", "artifact", "activate", "employee_researcher", "repository_skill", "1.1.0",
                "--allowed-capability", "workspace_read", "--confirm",
            ])
            self.assertEqual(explicit["version"], "1.1.0")
