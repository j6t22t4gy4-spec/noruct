from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.company.models import content_digest

from .evaluation import (
    evaluate_blueprint_admission,
    evaluate_blueprint_delta_holdout,
    evaluate_blueprint_delta_holdout_suite,
)
from .store import EvolutionStore
from .signing import allowed_signers_digest, release_candidate_payload, verify_openssh_signature
from .network_gate import network_gate_status, preview_network_worker
from .capability_gateway import preview_capability_grant
from .release_authorization import hosted_release_authorization_preview
from .operator_enrollment import operator_enrollment_preview
from .registry_bundle import (
    build_registry_bundle,
    fetch_registry_bundle,
    read_registry_bundle,
    registry_bundle_signing_payload,
)
from .artifact_bundle import (
    artifact_registry_bundle_signing_payload,
    build_artifact_registry_bundle,
    discover_artifact_registries,
    fetch_discovered_artifact_registry,
    fetch_private_network_artifact_registry,
    fetch_artifact_registry_bundle,
    fetch_artifact_registry_signature,
    read_artifact_registry_bundle,
)
from .hosted_transport import (
    endpoint_origin as hosted_endpoint_origin,
    submit_capsule as submit_hosted_capsule,
    withdraw_capsule as withdraw_hosted_capsule,
)
from .score_contract import evolution_content_digest, validate_evolution_score


CAPSULE_SCHEMA = "noruct.learning-capsule.v1"
CAPSULE_SCHEMA_V2 = "noruct.learning-capsule.v2"
EVOLUTION_PROPOSAL_SCHEMA = "noruct.evolution-proposal.v1"
CANDIDATE_EVALUATION_SCHEMA = "noruct.evolution-candidate-evaluation.v1"
BLUEPRINT_SCHEMA = "noruct.employee-blueprint.v1"
BLUEPRINT_DELTA_SCHEMA = "noruct.blueprint-delta.v1"
EVOLUTION_ARTIFACT_SCHEMA = "noruct.evolution-artifact.v1"
WORKFORCE_PASSPORT_SCHEMA = "noruct.workforce-passport.v1"
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_ARTIFACT_KINDS = frozenset(
    {
        "SKILL_PACKAGE",
        "AGENT_BLUEPRINT",
        "TOOL_PACKAGE",
        "WORKFLOW_PLAYBOOK",
        "BENCHMARK_SUITE",
        "EVALUATOR_PROFILE",
        "MODEL_COMPATIBILITY_PROFILE",
        "RELEASE_ADVISORY",
        "GRAPH_BLUEPRINT",
    }
)
_TYPED_ARTIFACT_PROPOSAL_KINDS = {
    "SKILL_PATCH": "SKILL_PACKAGE",
    "WORKFLOW_PATCH": "WORKFLOW_PLAYBOOK",
    "ROSTER_PATCH": "AGENT_BLUEPRINT",
    "TOOL_PATCH": "TOOL_PACKAGE",
    "GRAPH_BLUEPRINT_RELEASE": "GRAPH_BLUEPRINT",
}
_SENSITIVE_VALUE = re.compile(
    r"(?i)(sk-[a-z0-9]{12,}|api[_ -]?key\s*[:=]|authorization:\s*bearer|"
    r"-----begin|password\s*[:=]|token\s*[:=]|"
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
    r"(?:\+?\d[\d .()-]{7,}\d))"
)
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "messages",
        "transcript",
        "repository",
        "source_code",
        "file_content",
        "path",
        "memory",
        "credential",
        "secret",
        "token",
        "api_key",
        "raw_output",
    }
)


def _object(value: object, name: str, allowed: set[str], required: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    keys = set(value)
    unknown = keys - allowed
    missing = required - keys
    if unknown:
        raise ValueError(f"{name} has unsupported field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{name} is missing required field(s): {', '.join(sorted(missing))}")
    if keys & _FORBIDDEN_KEYS:
        raise ValueError(f"{name} includes a raw-data field that is never accepted")
    return value


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a lower-case identifier (2-80 characters)")
    return value


def _safe_text(value: object, name: str, *, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string up to {maximum} characters")
    if "\n" in value or "\r" in value or "/" in value or "\\" in value:
        raise ValueError(f"{name} cannot contain a path or multiline content")
    if _SENSITIVE_VALUE.search(value):
        raise ValueError(f"{name} appears to contain PII, a credential, or a secret")
    return value


def _safe_id_list(value: object, name: str, *, maximum: int = 16) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty list with at most {maximum} entries")
    parsed = tuple(_safe_id(item, name) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} cannot contain duplicate entries")
    return parsed


def _safe_optional_id_list(value: object, name: str, *, maximum: int = 16) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ValueError(f"{name} must be a list with at most {maximum} entries")
    parsed = tuple(_safe_id(item, name) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} cannot contain duplicate entries")
    return parsed


def _semver(value: object, name: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a semantic version like 1.2.3")
    return value


def _semver_key(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:  # validated at the policy boundary; retain a fail-closed guard here.
        raise ValueError(f"Invalid semantic version: {value}")
    return tuple(int(item) for item in match.groups())


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {path}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("The supplied JSON document must be an object")
    return raw


def validate_capsule(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Learning Capsule must be an object")
    schema = value.get("schema")
    if schema not in {CAPSULE_SCHEMA, CAPSULE_SCHEMA_V2}:
        raise ValueError(f"Learning Capsule schema must be {CAPSULE_SCHEMA} or {CAPSULE_SCHEMA_V2}")
    root_fields = {"schema", "capability", "task_schema", "execution_summary", "outcome", "authority"}
    if schema == CAPSULE_SCHEMA_V2:
        root_fields.add("proposal")
    root = _object(
        value,
        "Learning Capsule",
        root_fields,
        root_fields,
    )
    task = _object(
        root["task_schema"],
        "task_schema",
        {"domain", "operation", "input_fields", "risk_level"},
        {"domain", "operation", "input_fields", "risk_level"},
    )
    execution = _object(
        root["execution_summary"],
        "execution_summary",
        {"workflow_shape", "tool_classes", "decision_count", "redaction_applied"},
        {"workflow_shape", "tool_classes", "decision_count", "redaction_applied"},
    )
    outcome = _object(
        root["outcome"],
        "outcome",
        {"status", "quality_score", "cost_bucket", "evaluator_kind", "metric_names"},
        {"status", "quality_score", "cost_bucket", "evaluator_kind", "metric_names"},
    )
    if task["risk_level"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("task_schema.risk_level must be LOW, MEDIUM, or HIGH")
    if not isinstance(execution["decision_count"], int) or not 0 <= execution["decision_count"] <= 10000:
        raise ValueError("execution_summary.decision_count must be an integer from 0 to 10000")
    if not isinstance(execution["redaction_applied"], bool) or not execution["redaction_applied"]:
        raise ValueError("execution_summary.redaction_applied must be true")
    if outcome["status"] not in {"SUCCEEDED", "FAILED", "PARTIAL"}:
        raise ValueError("outcome.status must be SUCCEEDED, FAILED, or PARTIAL")
    quality_score = validate_evolution_score(
        outcome["quality_score"], "outcome.quality_score"
    )
    if outcome["cost_bucket"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("outcome.cost_bucket must be LOW, MEDIUM, or HIGH")
    if outcome["evaluator_kind"] not in {"LOCAL_TEST", "USER_REVIEW", "OFFLINE_FIXTURE"}:
        raise ValueError("outcome.evaluator_kind is not permitted")
    normalized: dict[str, Any] = {
        "schema": str(schema),
        "capability": _safe_id(root["capability"], "capability"),
        "authority": _safe_id(root["authority"], "authority"),
        "task_schema": {
            "domain": _safe_id(task["domain"], "task_schema.domain"),
            "operation": _safe_id(task["operation"], "task_schema.operation"),
            "input_fields": _safe_id_list(task["input_fields"], "task_schema.input_fields"),
            "risk_level": task["risk_level"],
        },
        "execution_summary": {
            "workflow_shape": _safe_id_list(execution["workflow_shape"], "execution_summary.workflow_shape"),
            "tool_classes": _safe_id_list(execution["tool_classes"], "execution_summary.tool_classes"),
            "decision_count": execution["decision_count"],
            "redaction_applied": True,
        },
        "outcome": {
            "status": outcome["status"],
            "quality_score": quality_score,
            "cost_bucket": outcome["cost_bucket"],
            "evaluator_kind": outcome["evaluator_kind"],
            "metric_names": _safe_id_list(outcome["metric_names"], "outcome.metric_names"),
        },
    }
    if schema == CAPSULE_SCHEMA_V2:
        normalized["proposal"] = validate_evolution_proposal(root["proposal"])
    return normalized


def validate_blueprint(value: object) -> Mapping[str, Any]:
    root = _object(
        value,
        "Employee Blueprint",
        {"schema", "blueprint_id", "version", "role", "capabilities", "evaluator", "policy_digest"},
        {"schema", "blueprint_id", "version", "role", "capabilities", "evaluator", "policy_digest"},
    )
    if root["schema"] != BLUEPRINT_SCHEMA:
        raise ValueError(f"Employee Blueprint schema must be {BLUEPRINT_SCHEMA}")
    policy_digest = root["policy_digest"]
    if not isinstance(policy_digest, str) or not _SHA256.fullmatch(policy_digest):
        raise ValueError("policy_digest must be a lowercase SHA-256 hex digest")
    return {
        "schema": BLUEPRINT_SCHEMA,
        "blueprint_id": _safe_id(root["blueprint_id"], "blueprint_id"),
        "version": _safe_text(root["version"], "version", maximum=40),
        "role": _safe_id(root["role"], "role"),
        "capabilities": _safe_id_list(root["capabilities"], "capabilities"),
        "evaluator": _safe_id(root["evaluator"], "evaluator"),
        "policy_digest": policy_digest,
    }


def validate_blueprint_delta(value: object) -> Mapping[str, Any]:
    root = _object(
        value,
        "Blueprint Delta",
        {
            "schema",
            "blueprint_id",
            "base_version",
            "candidate_version",
            "kind",
            "alias",
            "target_capability",
            "rollback",
        },
        {
            "schema",
            "blueprint_id",
            "base_version",
            "candidate_version",
            "kind",
            "alias",
            "target_capability",
            "rollback",
        },
    )
    if root["schema"] != BLUEPRINT_DELTA_SCHEMA:
        raise ValueError(f"Blueprint Delta schema must be {BLUEPRINT_DELTA_SCHEMA}")
    if root["kind"] != "CAPABILITY_ALIAS_ADD":
        raise ValueError("Only CAPABILITY_ALIAS_ADD is supported by the public synthetic holdout")
    rollback = _object(
        root["rollback"],
        "rollback",
        {"kind", "alias"},
        {"kind", "alias"},
    )
    alias = _safe_id(root["alias"], "alias")
    if rollback["kind"] != "CAPABILITY_ALIAS_REMOVE" or rollback["alias"] != alias:
        raise ValueError("rollback must exactly remove the same capability alias")
    base_version = _safe_text(root["base_version"], "base_version", maximum=40)
    candidate_version = _safe_text(root["candidate_version"], "candidate_version", maximum=40)
    if base_version == candidate_version:
        raise ValueError("candidate_version must differ from base_version")
    return {
        "schema": BLUEPRINT_DELTA_SCHEMA,
        "blueprint_id": _safe_id(root["blueprint_id"], "blueprint_id"),
        "base_version": base_version,
        "candidate_version": candidate_version,
        "kind": "CAPABILITY_ALIAS_ADD",
        "alias": alias,
        "target_capability": _safe_id(root["target_capability"], "target_capability"),
        "rollback": {"kind": "CAPABILITY_ALIAS_REMOVE", "alias": alias},
    }


def validate_candidate_evaluation(value: object) -> Mapping[str, Any]:
    root = _object(
        value,
        "Candidate evaluation",
        {
            "schema", "suite_id", "suite_version", "suite_digest", "evaluator_id",
            "fixture_scope", "quality_score", "safety_score", "cost_bucket", "decision",
        },
        {
            "schema", "suite_id", "suite_version", "suite_digest", "evaluator_id",
            "fixture_scope", "quality_score", "safety_score", "cost_bucket", "decision",
        },
    )
    if root["schema"] != CANDIDATE_EVALUATION_SCHEMA:
        raise ValueError(f"Candidate evaluation schema must be {CANDIDATE_EVALUATION_SCHEMA}")
    suite_digest = root["suite_digest"]
    if not isinstance(suite_digest, str) or _SHA256.fullmatch(suite_digest) is None:
        raise ValueError("suite_digest must be a lowercase SHA-256 hex digest")
    normalized_scores = {
        field: validate_evolution_score(root[field], field)
        for field in ("quality_score", "safety_score")
    }
    if root["fixture_scope"] not in {"PUBLIC", "SYNTHETIC"}:
        raise ValueError("fixture_scope must be PUBLIC or SYNTHETIC")
    if root["cost_bucket"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("cost_bucket must be LOW, MEDIUM, or HIGH")
    if root["decision"] not in {"PASS", "FAIL"}:
        raise ValueError("decision must be PASS or FAIL")
    return {
        "schema": CANDIDATE_EVALUATION_SCHEMA,
        "suite_id": _safe_id(root["suite_id"], "suite_id"),
        "suite_version": _semver(root["suite_version"], "suite_version"),
        "suite_digest": suite_digest,
        "evaluator_id": _safe_id(root["evaluator_id"], "evaluator_id"),
        "fixture_scope": root["fixture_scope"],
        "quality_score": normalized_scores["quality_score"],
        "safety_score": normalized_scores["safety_score"],
        "cost_bucket": root["cost_bucket"],
        "decision": root["decision"],
    }


def _validate_workforce_passport(value: object) -> Mapping[str, Any]:
    root = _object(
        value,
        "Workforce Passport",
        {"schema", "benchmark", "metrics", "limitations"},
        {"schema", "benchmark", "metrics", "limitations"},
    )
    if root["schema"] != WORKFORCE_PASSPORT_SCHEMA:
        raise ValueError(f"Workforce Passport schema must be {WORKFORCE_PASSPORT_SCHEMA}")
    benchmark = _object(
        root["benchmark"], "passport.benchmark", {"suite_id", "version", "digest"},
        {"suite_id", "version", "digest"},
    )
    metrics = _object(
        root["metrics"], "passport.metrics", {"quality_score", "safety_score", "cost_bucket", "latency_bucket"},
        {"quality_score", "safety_score", "cost_bucket", "latency_bucket"},
    )
    normalized_scores = {
        name: validate_evolution_score(metrics[name], f"passport.metrics.{name}")
        for name in ("quality_score", "safety_score")
    }
    if metrics["cost_bucket"] not in {"LOW", "MEDIUM", "HIGH"} or metrics["latency_bucket"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("passport cost and latency buckets must be LOW, MEDIUM, or HIGH")
    limitations = _safe_optional_id_list(root["limitations"], "passport.limitations")
    digest = benchmark["digest"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("passport.benchmark.digest must be a lowercase SHA-256 hex digest")
    return {
        "schema": WORKFORCE_PASSPORT_SCHEMA,
        "benchmark": {"suite_id": _safe_id(benchmark["suite_id"], "passport.benchmark.suite_id"), "version": _semver(benchmark["version"], "passport.benchmark.version"), "digest": digest},
        "metrics": {"quality_score": normalized_scores["quality_score"], "safety_score": normalized_scores["safety_score"], "cost_bucket": metrics["cost_bucket"], "latency_bucket": metrics["latency_bucket"]},
        "limitations": limitations,
    }


def _validate_artifact_content(kind: str, value: object) -> Mapping[str, Any]:
    if kind == "GRAPH_BLUEPRINT":
        content = _object(
            value,
            "Community Graph Blueprint content",
            {"release"},
            {"release"},
        )
        # The local Community parser owns the more specific, privacy-critical
        # release grammar.  Keeping the exact public release nested here lets
        # the common signed registry distribute it without obtaining a second
        # execution or authority path.
        from dynamic_firm.company.community_blueprints import community_release_from_payload

        release = community_release_from_payload(content["release"])
        return {"release": release.public_payload()}
    if kind == "SKILL_PACKAGE":
        content = _object(value, "Skill Package content", {"skill_key", "applies_to", "steps", "required_capabilities", "source_receipt_digest"}, {"skill_key", "applies_to", "steps", "required_capabilities"})
        steps = content["steps"]
        if not isinstance(steps, (list, tuple)) or not 1 <= len(steps) <= 16:
            raise ValueError("Skill Package steps must contain 1 to 16 bounded procedure steps")
        result = {"skill_key": _safe_id(content["skill_key"], "content.skill_key"), "applies_to": _safe_id_list(content["applies_to"], "content.applies_to"), "steps": tuple(_safe_text(step, "content.steps", maximum=240) for step in steps), "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities")}
        source_receipt_digest = content.get("source_receipt_digest")
        if source_receipt_digest is not None:
            if not isinstance(source_receipt_digest, str) or _SHA256.fullmatch(source_receipt_digest) is None:
                raise ValueError("Skill Package source_receipt_digest must be a lowercase SHA-256 hex digest")
            result["source_receipt_digest"] = source_receipt_digest
        return result
    if kind == "AGENT_BLUEPRINT":
        content = _object(value, "Agent Blueprint content", {"role", "skill_refs", "required_capabilities"}, {"role", "skill_refs", "required_capabilities"})
        return {"role": _safe_id(content["role"], "content.role"), "skill_refs": _safe_optional_id_list(content["skill_refs"], "content.skill_refs"), "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities")}
    if kind == "TOOL_PACKAGE":
        content = _object(value, "Tool Package content", {"tool_class", "adapter_reference", "binding_digest", "input_fields", "output_fields", "required_capabilities"}, {"tool_class", "adapter_reference", "input_fields", "output_fields", "required_capabilities"})
        tool_class = _safe_id(content["tool_class"], "content.tool_class")
        adapter_reference = _safe_id(content["adapter_reference"], "content.adapter_reference")
        result = {"tool_class": tool_class, "adapter_reference": adapter_reference, "input_fields": _safe_id_list(content["input_fields"], "content.input_fields"), "output_fields": _safe_id_list(content["output_fields"], "content.output_fields"), "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities")}
        if adapter_reference == "mcp_readonly_policy_v1":
            binding_digest = content.get("binding_digest")
            if tool_class != "external_read" or not isinstance(binding_digest, str) or _SHA256.fullmatch(binding_digest) is None:
                raise ValueError("MCP policy Tool Package requires external_read and a SHA-256 binding_digest")
            result["binding_digest"] = binding_digest
        elif "binding_digest" in content:
            raise ValueError("Only the MCP policy Tool Package adapter may carry binding_digest")
        return result
    if kind == "WORKFLOW_PLAYBOOK":
        content = _object(
            value,
            "Workflow Playbook content",
            {"workflow_shape", "reviewer_policy", "required_capabilities", "adapter_reference", "execution_profile"},
            {"workflow_shape", "reviewer_policy", "required_capabilities"},
        )
        result = {
            "workflow_shape": _safe_id_list(content["workflow_shape"], "content.workflow_shape"),
            "reviewer_policy": _safe_id(content["reviewer_policy"], "content.reviewer_policy"),
            "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities"),
        }
        adapter_reference = content.get("adapter_reference")
        execution_profile = content.get("execution_profile")
        if adapter_reference is None and execution_profile is None:
            return result
        if adapter_reference != "compiler_linear_playbook_v1":
            raise ValueError("Workflow Playbook adapter_reference is unsupported")
        if execution_profile not in {"read_only", "host_action", "host_direct", "shadow_coding"}:
            raise ValueError("Workflow Playbook adapter requires a supported execution_profile")
        if len(result["workflow_shape"]) > 6:
            raise ValueError("Workflow Playbook compiler adapter supports at most 6 capability steps")
        result["adapter_reference"] = adapter_reference
        result["execution_profile"] = execution_profile
        return result
    if kind == "BENCHMARK_SUITE":
        content = _object(
            value,
            "Benchmark Suite content",
            {"fixture_ids", "scorer", "required_capabilities", "adapter_reference"},
            {"fixture_ids", "scorer", "required_capabilities"},
        )
        result = {
            "fixture_ids": _safe_id_list(content["fixture_ids"], "content.fixture_ids"),
            "scorer": _safe_id(content["scorer"], "content.scorer"),
            "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities"),
        }
        adapter_reference = content.get("adapter_reference")
        if adapter_reference is None:
            return result
        if (
            adapter_reference != "blueprint_delta_holdout_suite_v1"
            or result["scorer"] != "capability_route_delta"
            or set(result["fixture_ids"])
            != {
                "public_synthetic_capability_alias_v1",
                "public_synthetic_capability_alias_safety_v1",
            }
        ):
            raise ValueError("Benchmark Suite adapter must name the registered public holdout suite")
        result["adapter_reference"] = adapter_reference
        return result
    if kind == "EVALUATOR_PROFILE":
        content = _object(
            value,
            "Evaluator Profile content",
            {"evaluator", "threshold_profile", "required_capabilities", "adapter_reference"},
            {"evaluator", "threshold_profile", "required_capabilities"},
        )
        result = {
            "evaluator": _safe_id(content["evaluator"], "content.evaluator"),
            "threshold_profile": _safe_id(content["threshold_profile"], "content.threshold_profile"),
            "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities"),
        }
        adapter_reference = content.get("adapter_reference")
        if adapter_reference is None:
            return result
        if (
            adapter_reference != "blueprint_delta_holdout_suite_v1"
            or result["evaluator"] != "capability_route_delta"
            or result["threshold_profile"] != "manual_review_only"
        ):
            raise ValueError("Evaluator Profile adapter must name the registered public holdout evaluator")
        result["adapter_reference"] = adapter_reference
        return result
    if kind == "MODEL_COMPATIBILITY_PROFILE":
        content = _object(value, "Model Compatibility Profile content", {"model_family", "required_capabilities"}, {"model_family", "required_capabilities"})
        return {"model_family": _safe_id(content["model_family"], "content.model_family"), "required_capabilities": _safe_optional_id_list(content["required_capabilities"], "content.required_capabilities")}
    content = _object(value, "Release Advisory content", {"advisory_code", "severity"}, {"advisory_code", "severity"})
    if content["severity"] not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        raise ValueError("content.severity must be LOW, MEDIUM, HIGH, or CRITICAL")
    return {"advisory_code": _safe_id(content["advisory_code"], "content.advisory_code"), "severity": content["severity"]}


def validate_evolution_artifact(value: object) -> Mapping[str, Any]:
    root = _object(value, "Evolution Artifact", {"schema", "artifact_id", "version", "kind", "release_channel", "compatibility", "content", "passport"}, {"schema", "artifact_id", "version", "kind", "release_channel", "compatibility", "content", "passport"})
    if root["schema"] != EVOLUTION_ARTIFACT_SCHEMA:
        raise ValueError(f"Evolution Artifact schema must be {EVOLUTION_ARTIFACT_SCHEMA}")
    kind = root["kind"]
    if kind not in _ARTIFACT_KINDS:
        raise ValueError("Evolution Artifact kind is not permitted")
    if root["release_channel"] not in {"STABLE", "EXPERIMENTAL"}:
        raise ValueError("Evolution Artifact release_channel must be STABLE or EXPERIMENTAL")
    compatibility = _object(root["compatibility"], "artifact.compatibility", {"runtime_contract", "required_capabilities"}, {"runtime_contract", "required_capabilities"})
    required_capabilities = _safe_optional_id_list(compatibility["required_capabilities"], "compatibility.required_capabilities")
    content = _validate_artifact_content(str(kind), root["content"])
    passport = _validate_workforce_passport(root["passport"])
    if root["release_channel"] == "STABLE" and (passport["metrics"]["quality_score"] < 0.5 or passport["metrics"]["safety_score"] < 0.9):
        raise ValueError("STABLE Evolution Artifacts require quality >= 0.5 and safety >= 0.9")
    return {"schema": EVOLUTION_ARTIFACT_SCHEMA, "artifact_id": _safe_id(root["artifact_id"], "artifact_id"), "version": _semver(root["version"], "version"), "kind": kind, "release_channel": root["release_channel"], "compatibility": {"runtime_contract": _safe_id(compatibility["runtime_contract"], "compatibility.runtime_contract"), "required_capabilities": required_capabilities}, "content": content, "passport": passport}


def validate_evolution_proposal(value: object) -> Mapping[str, Any]:
    """Validate the closed, data-only Proposal union accepted by Capsule v2.

    Blueprint Delta remains backward compatible.  Skill, Workflow, Roster and
    Tool proposals carry exactly one already strict immutable Artifact
    manifest; they cannot carry code, paths, prompts, credentials, or an
    executable adapter implementation.
    """

    if not isinstance(value, Mapping):
        raise ValueError("proposal must be an object")
    kind = value.get("kind")
    if kind == "BLUEPRINT_DELTA":
        proposal = _object(
            value,
            "proposal",
            {"schema", "kind", "delta"},
            {"schema", "kind", "delta"},
        )
        if proposal["schema"] != EVOLUTION_PROPOSAL_SCHEMA:
            raise ValueError(f"proposal.schema must be {EVOLUTION_PROPOSAL_SCHEMA}")
        return {
            "schema": EVOLUTION_PROPOSAL_SCHEMA,
            "kind": "BLUEPRINT_DELTA",
            "delta": validate_blueprint_delta(proposal["delta"]),
        }
    expected_artifact_kind = _TYPED_ARTIFACT_PROPOSAL_KINDS.get(str(kind))
    if expected_artifact_kind is None:
        permitted = ", ".join(
            ("BLUEPRINT_DELTA", *_TYPED_ARTIFACT_PROPOSAL_KINDS)
        )
        raise ValueError(f"proposal.kind must be one of: {permitted}")
    proposal = _object(
        value,
        "proposal",
        {"schema", "kind", "artifact"},
        {"schema", "kind", "artifact"},
    )
    if proposal["schema"] != EVOLUTION_PROPOSAL_SCHEMA:
        raise ValueError(f"proposal.schema must be {EVOLUTION_PROPOSAL_SCHEMA}")
    artifact = validate_evolution_artifact(proposal["artifact"])
    if artifact["kind"] != expected_artifact_kind:
        raise ValueError(
            f"{kind} requires an {expected_artifact_kind} Artifact manifest"
        )
    return {
        "schema": EVOLUTION_PROPOSAL_SCHEMA,
        "kind": kind,
        "artifact": artifact,
    }


from .artifact_lifecycle import ArtifactLifecycleMixin
from .artifact_regression_lifecycle import ArtifactRegressionLifecycleMixin


class EvolutionNetworkService(ArtifactRegressionLifecycleMixin, ArtifactLifecycleMixin):
    """Policy service for opt-in local state and explicit hosted intake."""

    def __init__(self, store: EvolutionStore) -> None:
        self.store = store

    def status(self) -> Mapping[str, Any]:
        status = dict(self.store.status())
        active_consents = int(status.get("active_consents", 0))
        status["local_sovereignty"] = {
            "mode": "LOCAL_SOVEREIGN" if active_consents == 0 else "LOCAL_CONTRIBUTOR_PREVIEW",
            "company_runtime_requires_consent": False,
            "company_runtime_state_authority": "LOCAL_CUSTOMER",
            "network_request_performed": False,
            "raw_workspace_upload": "PROHIBITED",
            "shared_blueprint_effect": "CATALOG_ONLY",
        }
        return status

    def grant_consent(
        self, *, purpose: str, allowed_reuse: str, retention_days: int, authority: str
    ) -> Mapping[str, Any]:
        permitted_scopes = {
            ("BLUEPRINT_IMPROVEMENT", "EVALUATE_AND_PROMOTE_BLUEPRINT"),
            (
                "SHARED_EVOLUTION_IMPROVEMENT",
                "EVALUATE_AND_PROMOTE_VERSIONED_ARTIFACT",
            ),
        }
        if (purpose, allowed_reuse) not in permitted_scopes:
            raise ValueError(
                "Consent scope must be the Blueprint compatibility scope or the "
                "versioned Shared Evolution Artifact scope"
            )
        if authority not in {"INDIVIDUAL", "ORGANIZATION_OWNER"}:
            raise ValueError("authority must be INDIVIDUAL or ORGANIZATION_OWNER")
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        return self.store.grant_consent(
            purpose=purpose,
            allowed_reuse=allowed_reuse,
            retention_days=retention_days,
            authority=authority,
        )

    def withdraw_consent(self, consent_id: str) -> Mapping[str, Any]:
        return self.store.withdraw_consent(consent_id)

    def preview_capsule_file(self, path: Path) -> Mapping[str, Any]:
        capsule = validate_capsule(_load_json(path))
        return {
            "accepted": True,
            "network_transport": "DISABLED",
            "payload_digest": evolution_content_digest(capsule),
            "sanitized_capsule": capsule,
        }

    def submit_capsule_file(self, path: Path, consent_id: str) -> Mapping[str, Any]:
        capsule = validate_capsule(_load_json(path))
        return self.store.create_capsule(consent_id, capsule)

    def withdraw_capsule(self, capsule_id: str) -> Mapping[str, Any]:
        return self.store.withdraw_capsule(capsule_id)

    def submit_hosted_capsule(
        self,
        capsule_id: str,
        *,
        endpoint: str,
        token: str,
        withdrawal_capability: str,
        allow_insecure_loopback: bool = False,
    ) -> Mapping[str, Any]:
        capsule = self.store.get_capsule(capsule_id)
        if capsule.get("status") == "SUBMITTED_HOSTED":
            return {"capsule": capsule, "receipt": self.store.hosted_capsule_receipt(capsule_id), "idempotent": True}
        if capsule.get("status") != "QUEUED_LOCAL_ONLY" or not isinstance(capsule.get("payload"), Mapping):
            raise ValueError("Only an active locally queued Capsule may be submitted to the Evolution Network")
        consent = self.store.get_consent(str(capsule["consent_id"]))
        if consent.get("status") != "ACTIVE":
            raise ValueError("An active local contribution consent is required for hosted submission")
        receipt = submit_hosted_capsule(
            endpoint=endpoint,
            token=token,
            capsule_id=capsule_id,
            capsule=capsule["payload"],
            consent=consent,
            withdrawal_capability=withdrawal_capability,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        updated = self.store.record_hosted_capsule_submission(
            capsule_id,
            endpoint_origin=receipt.endpoint_origin,
            contribution_id=receipt.contribution_id,
            receipt_digest=receipt.receipt_digest,
            submitted_at=receipt.recorded_at,
        )
        return {"capsule": updated, "receipt": receipt.to_dict(), "idempotent": False}

    def withdraw_hosted_capsule(
        self,
        capsule_id: str,
        *,
        endpoint: str,
        token: str,
        withdrawal_capability: str | None = None,
        allow_insecure_loopback: bool = False,
    ) -> Mapping[str, Any]:
        capsule = self.store.get_capsule(capsule_id)
        if capsule.get("status") != "SUBMITTED_HOSTED" or not isinstance(capsule.get("payload"), Mapping):
            raise ValueError("Only an active hosted Capsule may be withdrawn from the Evolution Network")
        local_receipt = self.store.hosted_capsule_receipt(capsule_id)
        requested_origin = hosted_endpoint_origin(
            endpoint,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        if requested_origin != local_receipt["endpoint_origin"]:
            raise ValueError(
                "Hosted withdrawal endpoint does not match the recorded submission endpoint"
            )
        receipt = withdraw_hosted_capsule(
            endpoint=endpoint,
            token=token,
            contribution_id=str(local_receipt["contribution_id"]),
            capsule_id=capsule_id,
            capsule_digest=str(capsule["payload_digest"]),
            allow_insecure_loopback=allow_insecure_loopback,
            withdrawal_capability=withdrawal_capability,
        )
        updated = self.store.record_hosted_capsule_withdrawal(
            capsule_id,
            receipt_digest=receipt.receipt_digest,
            withdrawn_at=receipt.recorded_at,
        )
        return {"capsule": updated, "receipt": receipt.to_dict()}

    def withdraw_hosted_consent(
        self,
        consent_id: str,
        *,
        endpoint: str,
        token: str,
        allow_insecure_loopback: bool = False,
    ) -> Mapping[str, Any]:
        # Hosted deletion is capability-only.  The capability is intentionally
        # neither persisted locally nor derivable from a consent, so a blanket
        # consent operation could falsely claim deletion for a Capsule it
        # cannot prove was withdrawn.
        del consent_id, endpoint, token, allow_insecure_loopback
        raise ValueError(
            "Hosted consent withdrawal is unavailable for capability-only intake; "
            "withdraw each pending Capsule with its own capability"
        )

    def preview_blueprint_file(self, path: Path) -> Mapping[str, Any]:
        manifest = validate_blueprint(_load_json(path))
        return {
            "accepted": True,
            "manifest_digest": content_digest(manifest),
            "blueprint": manifest,
            "execution": "CATALOG_ONLY",
        }

    def import_blueprint_file(self, path: Path) -> Mapping[str, Any]:
        return self.store.import_blueprint(validate_blueprint(_load_json(path)))

    def list_blueprints(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_blueprints()

    def select_blueprint(self, blueprint_id: str, version: str) -> Mapping[str, Any]:
        return self.store.select_blueprint(blueprint_id, version)

    def rollback_selection(self, role: str) -> Mapping[str, Any]:
        return self.store.rollback_selection(role)


    @staticmethod
    def evaluate_admission_files(
        blueprint_path: Path, capsule_paths: tuple[Path, ...]
    ) -> Mapping[str, Any]:
        blueprint = validate_blueprint(_load_json(blueprint_path))
        capsules = tuple(validate_capsule(_load_json(path)) for path in capsule_paths)
        return evaluate_blueprint_admission(blueprint, capsules).to_dict()

    @staticmethod
    def preview_delta_file(path: Path) -> Mapping[str, Any]:
        delta = validate_blueprint_delta(_load_json(path))
        return {
            "accepted": True,
            "delta_digest": content_digest(delta),
            "delta": delta,
            "execution": "PUBLIC_SYNTHETIC_HOLDOUT_ONLY",
        }

    @staticmethod
    def evaluate_delta_holdout_files(
        blueprint_path: Path, delta_path: Path
    ) -> Mapping[str, Any]:
        blueprint = validate_blueprint(_load_json(blueprint_path))
        delta = validate_blueprint_delta(_load_json(delta_path))
        return evaluate_blueprint_delta_holdout(blueprint, delta).to_dict()

    @staticmethod
    def evaluate_delta_holdout_suite_files(
        blueprint_path: Path, delta_path: Path
    ) -> Mapping[str, Any]:
        blueprint = validate_blueprint(_load_json(blueprint_path))
        delta = validate_blueprint_delta(_load_json(delta_path))
        return evaluate_blueprint_delta_holdout_suite(blueprint, delta).to_dict()

    def export_payload(self) -> Mapping[str, Any]:
        return self.store.export_payload()

    def create_release_candidate_files(
        self, blueprint_path: Path, delta_path: Path, capsule_ids: tuple[str, ...]
    ) -> Mapping[str, Any]:
        blueprint = validate_blueprint(_load_json(blueprint_path))
        delta = validate_blueprint_delta(_load_json(delta_path))
        holdout = evaluate_blueprint_delta_holdout_suite(blueprint, delta).to_dict()
        return self.store.create_release_candidate(
            blueprint=blueprint, delta=delta, holdout=holdout, capsule_ids=capsule_ids
        )

    def list_release_candidates(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_release_candidates()

    def release_candidate_signing_payload(self, candidate_id: str) -> bytes:
        return release_candidate_payload(self.store.get_release_candidate(candidate_id))

    def verify_release_candidate_signature(
        self, candidate_id: str, *, signature: Path, allowed_signers: Path, principal: str, ssh_keygen: Path
    ) -> Mapping[str, Any]:
        receipt = verify_openssh_signature(self.release_candidate_signing_payload(candidate_id), signature_path=signature, allowed_signers_path=allowed_signers, principal=principal, command=ssh_keygen)
        return self.store.record_verified_signature(candidate_id, receipt)

    def publish_release_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        return self.store.publish_release_candidate(candidate_id)

    def list_registry_releases(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_registry_releases()

    def build_public_registry_bundle(self, registry_id: str) -> Mapping[str, Any]:
        return build_registry_bundle(self.list_registry_releases(), registry_id=registry_id)

    @staticmethod
    def inspect_public_registry_bundle(path: Path) -> Mapping[str, Any]:
        return read_registry_bundle(path)

    @staticmethod
    def fetch_public_registry_bundle(
        url: str, *, allow_insecure_loopback: bool = False
    ) -> Mapping[str, Any]:
        return fetch_registry_bundle(url, allow_insecure_loopback=allow_insecure_loopback)

    @staticmethod
    def public_registry_bundle_signing_payload(bundle: Mapping[str, Any]) -> bytes:
        return registry_bundle_signing_payload(bundle)

    @staticmethod
    def verify_public_registry_bundle_signature(
        bundle: Mapping[str, Any], *, signature: Path, allowed_signers: Path,
        principal: str, ssh_keygen: Path,
    ) -> Mapping[str, str]:
        return verify_openssh_signature(
            registry_bundle_signing_payload(bundle), signature_path=signature,
            allowed_signers_path=allowed_signers, principal=principal, command=ssh_keygen,
        )

    def stage_verified_public_registry_bundle(
        self,
        bundle: Mapping[str, Any],
        *,
        source_label: str,
        signature: bytes,
        allowed_signers: Path,
        principal: str,
        ssh_keygen: Path,
    ) -> Mapping[str, Any]:
        return self.store.stage_verified_registry_bundle(
            bundle,
            source_label=source_label,
            signature=signature,
            allowed_signers_path=allowed_signers,
            principal=principal,
            command=ssh_keygen,
        )

    def list_staged_public_registry_bundles(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_staged_registry_snapshots()

    def register_public_registry_signer(
        self, *, source_label: str, allowed_signers: Path, principal: str, operator_id: str
    ) -> Mapping[str, Any]:
        return self.store.register_registry_signer_trust_root(
            source_label=source_label, signer_principal=principal,
            allowed_signers_digest=allowed_signers_digest(allowed_signers), operator_id=operator_id,
        )

    def list_public_registry_signers(self) -> tuple[Mapping[str, Any], ...]:
        return self.store.list_registry_signer_trust_roots()

    def retire_public_registry_signer(self, trust_root_id: str, *, operator_id: str) -> Mapping[str, Any]:
        return self.store.retire_registry_signer_trust_root(trust_root_id, operator_id=operator_id)

    def revoke_public_registry_signer(
        self, trust_root_id: str, *, operator_id: str, reason: str
    ) -> Mapping[str, Any]:
        return self.store.revoke_registry_signer_trust_root(
            trust_root_id, operator_id=operator_id, reason=reason
        )

    def preview_staged_registry_compatibility(self, snapshot_id: str) -> Mapping[str, Any]:
        return self.store.preview_staged_registry_compatibility(snapshot_id)

    def review_staged_registry_snapshot(self, snapshot_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        return self.store.review_staged_registry_snapshot(snapshot_id, operator_id=operator_id, decision=decision, reason=reason)

    def preview_remote_tenant_candidate(self, tenant_id: str, snapshot_id: str, remote_release_id: str) -> Mapping[str, Any]:
        return self.store.preview_remote_tenant_candidate(_safe_id(tenant_id, "tenant_id"), snapshot_id, remote_release_id)

    def propose_remote_tenant_candidate(self, tenant_id: str, snapshot_id: str, remote_release_id: str, *, operator_id: str, reason: str) -> Mapping[str, Any]:
        return self.store.propose_remote_tenant_candidate(_safe_id(tenant_id, "tenant_id"), snapshot_id, remote_release_id, operator_id=operator_id, reason=reason)

    def resolve_remote_tenant_candidate(self, candidate_id: str, *, operator_id: str, decision: str, reason: str) -> Mapping[str, Any]:
        return self.store.resolve_remote_tenant_candidate(candidate_id, operator_id=operator_id, decision=decision, reason=reason)

    def preview_tenant_adoption(self, tenant_id: str, release_id: str) -> Mapping[str, Any]:
        return self.store.preview_tenant_adoption(_safe_id(tenant_id, "tenant_id"), release_id)

    def adopt_registry_release(self, tenant_id: str, release_id: str) -> Mapping[str, Any]:
        return self.store.adopt_registry_release(_safe_id(tenant_id, "tenant_id"), release_id)

    def rollback_tenant_adoption(self, tenant_id: str, role: str) -> Mapping[str, Any]:
        return self.store.rollback_tenant_adoption(_safe_id(tenant_id, "tenant_id"), _safe_id(role, "role"))

    @staticmethod
    def network_gate_status() -> Mapping[str, Any]:
        return network_gate_status().to_dict()

    @staticmethod
    def preview_network_worker(tenant_id: str, release_id: str) -> Mapping[str, Any]:
        return preview_network_worker(_safe_id(tenant_id, "tenant_id"), release_id)

    @staticmethod
    def preview_capability_grant(tenant_id: str, release_id: str, job_id: str, capabilities: tuple[str, ...]) -> Mapping[str, Any]:
        return preview_capability_grant(_safe_id(tenant_id, "tenant_id"), release_id, _safe_id(job_id, "job_id"), capabilities).to_dict()

    @staticmethod
    def hosted_release_authorization_preview() -> Mapping[str, Any]:
        return hosted_release_authorization_preview()

    @staticmethod
    def operator_enrollment_preview(
        *, role: str, token_env: str, identity: str | None = None,
        authority: str = "INDIVIDUAL",
    ) -> Mapping[str, Any]:
        return operator_enrollment_preview(
            role=role,
            token_env=token_env,
            identity=identity,
            authority=authority,
        )

    def review_release_candidate(
        self, candidate_id: str, *, operator_id: str, decision: str, reason: str
    ) -> Mapping[str, Any]:
        return self.store.review_release_candidate(
            candidate_id, operator_id=operator_id, decision=decision, reason=reason
        )
