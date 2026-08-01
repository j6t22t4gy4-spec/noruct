"""Build a qualified Community Blueprint Passport from public evidence only.

Community releases deliberately cannot derive a quality claim from a private
Work Order, transcript, Knowledge Asset, or local run record.  This module is
the narrow alternative: an operator supplies a bounded public/synthetic
evaluation result set, the producer validates it, and derives only aggregate
metrics plus an immutable evidence digest.  It never contacts a network or
mutates a Blueprint registry.
"""

from __future__ import annotations

import math
from statistics import mean
from typing import Any, Mapping

from .community_blueprints import BlueprintPassport
from .graph_blueprint_models import digest, identifier


COMMUNITY_PASSPORT_OBSERVATIONS_SCHEMA = "noruct.community-blueprint-passport-observations.v1"
_ALLOWED_STATUS = frozenset({"SUCCEEDED", "FAILED", "PARTIAL"})


def _mapping(value: object, label: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} has an unsupported shape")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an identifier")
    return identifier(value, label)


def _semver(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or any(
        not item.isdigit() for item in value.split(".")
    ) or len(value.split(".")) != 3:
        raise ValueError(f"{label} must be a semantic version")
    return value


def _score(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite score")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{label} must be between zero and one")
    return parsed


def _non_negative(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be non-negative")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _p10(values: tuple[float, ...]) -> float:
    """Return the conservative nearest-rank lower decile for a nonempty set."""

    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.10) - 1)]


def passport_observations_from_payload(value: object) -> Mapping[str, Any]:
    """Strictly parse a content-free public/synthetic evidence envelope."""

    root = _mapping(
        value,
        "Community Passport observations",
        {"schema", "evaluator_revision", "runtime_contract", "suite", "limitations", "observations"},
    )
    if root["schema"] != COMMUNITY_PASSPORT_OBSERVATIONS_SCHEMA:
        raise ValueError("Community Passport observations schema is invalid")
    suite = _mapping(root["suite"], "Community Passport suite", {"suite_id", "version", "digest", "fixture_scope"})
    if suite["fixture_scope"] not in {"PUBLIC", "SYNTHETIC"}:
        raise ValueError("Community Passport suite must use PUBLIC or SYNTHETIC fixtures")
    suite_digest = suite["digest"]
    if not isinstance(suite_digest, str) or len(suite_digest) != 64 or any(
        char not in "0123456789abcdef" for char in suite_digest
    ):
        raise ValueError("Community Passport suite digest is invalid")
    if not isinstance(root["limitations"], list) or not root["limitations"]:
        raise ValueError("Community Passport observations require one or more limitation codes")
    limitations = tuple(_identifier(item, "Community Passport limitation") for item in root["limitations"])
    if len(set(limitations)) != len(limitations):
        raise ValueError("Community Passport limitation codes must be unique")
    if not isinstance(root["observations"], list) or not 10 <= len(root["observations"]) <= 512:
        raise ValueError("Community Passport requires 10 to 512 public/synthetic observations")
    observations: list[dict[str, object]] = []
    seen_cases: set[str] = set()
    for raw in root["observations"]:
        item = _mapping(
            raw,
            "Community Passport observation",
            {"case_id", "status", "quality_score", "safety_passed", "model_calls", "elapsed_ms", "mutation_count"},
        )
        case_id = _identifier(item["case_id"], "Community Passport case_id")
        if case_id in seen_cases:
            raise ValueError("Community Passport case_id values must be unique")
        seen_cases.add(case_id)
        if item["status"] not in _ALLOWED_STATUS:
            raise ValueError("Community Passport observation status is invalid")
        if not isinstance(item["safety_passed"], bool):
            raise ValueError("Community Passport safety_passed must be boolean")
        model_calls = _non_negative(item["model_calls"], "Community Passport model_calls")
        elapsed_ms = _non_negative(item["elapsed_ms"], "Community Passport elapsed_ms")
        mutation_count = _non_negative(item["mutation_count"], "Community Passport mutation_count")
        if not model_calls.is_integer() or not mutation_count.is_integer():
            raise ValueError("Community Passport model_calls and mutation_count must be integers")
        observations.append(
            {
                "case_id": case_id,
                "status": str(item["status"]),
                "quality_score": _score(item["quality_score"], "Community Passport quality_score"),
                "safety_passed": item["safety_passed"],
                "model_calls": int(model_calls),
                "elapsed_ms": elapsed_ms,
                "mutation_count": int(mutation_count),
            }
        )
    return {
        "schema": COMMUNITY_PASSPORT_OBSERVATIONS_SCHEMA,
        "evaluator_revision": _identifier(root["evaluator_revision"], "Community Passport evaluator_revision"),
        "runtime_contract": _identifier(root["runtime_contract"], "Community Passport runtime_contract"),
        "suite": {
            "suite_id": _identifier(suite["suite_id"], "Community Passport suite_id"),
            "version": _semver(suite["version"], "Community Passport suite version"),
            "digest": suite_digest,
            "fixture_scope": suite["fixture_scope"],
        },
        "limitations": limitations,
        "observations": tuple(observations),
    }


def build_qualified_blueprint_passport(value: object) -> BlueprintPassport:
    """Produce the immutable aggregate Passport for a shareable Blueprint.

    The source envelope is intentionally not stored in the release.  Its
    digest binds every numeric claim to a precise evaluator/suite/result set,
    while the published Passport retains only aggregates and limitation codes.
    """

    parsed = passport_observations_from_payload(value)
    observations = tuple(parsed["observations"])
    quality = tuple(float(item["quality_score"]) for item in observations)
    safety_failures = sum(not bool(item["safety_passed"]) for item in observations)
    complete_failures = sum(item["status"] == "FAILED" for item in observations)
    evidence_digest = digest(parsed)
    limitations = tuple(sorted(set((*parsed["limitations"], "public_or_synthetic_evidence_only"))))
    return BlueprintPassport(
        runtime_contract=str(parsed["runtime_contract"]),
        evaluator_revision=str(parsed["evaluator_revision"]),
        sample_count=len(observations),
        p10_quality=round(_p10(quality), 6),
        complete_failure_rate=round(complete_failures / len(observations), 6),
        safety_failure_rate=round(safety_failures / len(observations), 6),
        mean_model_calls=round(mean(int(item["model_calls"]) for item in observations), 6),
        mean_elapsed_ms=round(mean(float(item["elapsed_ms"]) for item in observations), 6),
        mutation_frequency=round(mean(int(item["mutation_count"]) for item in observations), 6),
        known_limitations=limitations,
        evidence_digest=evidence_digest,
    )
