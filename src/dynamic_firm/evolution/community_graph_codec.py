"""Data-only bridge between Community Blueprint releases and Evolution artifacts.

Evolution owns the transport envelope and generic artifact validation. Company
owns the privacy-critical Community release grammar. This codec composes those
two narrow contracts without making Company import Evolution services.
"""

from __future__ import annotations

from typing import Mapping

from dynamic_firm.company.community_blueprints import (
    COMMUNITY_GRAPH_ARTIFACT_KIND,
    COMMUNITY_RUNTIME_CONTRACT,
    EVOLUTION_ARTIFACT_SCHEMA,
    WORKFORCE_PASSPORT_SCHEMA,
    CommunityGraphBlueprintRelease,
    community_release_from_payload,
    digest,
)

from .service import validate_evolution_artifact


def community_release_to_evolution_artifact(
    release: CommunityGraphBlueprintRelease,
    *,
    release_channel: str = "EXPERIMENTAL",
) -> dict[str, object]:
    """Encode one public Community release for the signed artifact rail."""

    release.verify()
    passport = release.artifact.passport
    if release_channel not in {"EXPERIMENTAL", "STABLE"}:
        raise ValueError("community release channel must be EXPERIMENTAL or STABLE")
    if release_channel == "STABLE" and (
        passport.sample_count < 10
        or passport.p10_quality is None
        or passport.safety_failure_rate is None
        or passport.evidence_digest is None
        or passport.p10_quality < 0.5
        or passport.safety_failure_rate > 0.1
    ):
        raise ValueError("STABLE Community Blueprint requires a qualified safe Passport")
    quality = 0.0 if passport.p10_quality is None else passport.p10_quality
    safety = 1.0 if passport.safety_failure_rate is None else 1.0 - passport.safety_failure_rate
    limitations = tuple(
        sorted(
            set(passport.known_limitations)
            | {
                "local_stage_required",
                "no_remote_activation",
                *(("unqualified_passport",) if passport.sample_count == 0 else ()),
            }
        )
    )
    required_capabilities = tuple(
        sorted(
            {
                capability
                for task in release.artifact.tasks
                for capability in task.required_capabilities
            }
        )
    )
    benchmark_digest = digest(
        {
            "schema": "noruct.community-blueprint-passport-benchmark.v1",
            "evaluator_revision": passport.evaluator_revision,
            "runtime_contract": passport.runtime_contract,
        }
    )
    return {
        "schema": EVOLUTION_ARTIFACT_SCHEMA,
        "artifact_id": release.artifact.artifact_id,
        "version": f"0.0.{release.artifact.revision}",
        "kind": COMMUNITY_GRAPH_ARTIFACT_KIND,
        "release_channel": release_channel,
        "compatibility": {
            "runtime_contract": COMMUNITY_RUNTIME_CONTRACT,
            "required_capabilities": list(required_capabilities),
        },
        "content": {"release": release.public_payload()},
        "passport": {
            "schema": WORKFORCE_PASSPORT_SCHEMA,
            "benchmark": {
                "suite_id": "community_graph_passport",
                "version": "1.0.0",
                "digest": benchmark_digest,
            },
            "metrics": {
                "quality_score": quality,
                "safety_score": safety,
                "cost_bucket": "LOW" if passport.mean_model_calls in {None, 0} else "MEDIUM",
                "latency_bucket": "LOW" if passport.mean_elapsed_ms in {None, 0} else "MEDIUM",
            },
            "limitations": list(limitations),
        },
    }


def community_release_from_evolution_artifact(payload: object) -> CommunityGraphBlueprintRelease:
    """Decode a validated signed-registry manifest without granting authority."""

    if not isinstance(payload, Mapping):
        raise ValueError("Community Graph artifact must be an object")
    artifact = validate_evolution_artifact(payload)
    if artifact["kind"] != COMMUNITY_GRAPH_ARTIFACT_KIND:
        raise ValueError("Evolution Artifact is not a Community Graph Blueprint")
    content = artifact.get("content")
    if not isinstance(content, Mapping) or set(content) != {"release"}:
        raise ValueError("Community Graph Artifact content is invalid")
    release = community_release_from_payload(content["release"])
    if (
        artifact["artifact_id"] != release.artifact.artifact_id
        or artifact["version"] != f"0.0.{release.artifact.revision}"
        or artifact["compatibility"]["runtime_contract"] != COMMUNITY_RUNTIME_CONTRACT
    ):
        raise ValueError("Community Graph Artifact identity or runtime contract is invalid")
    expected_capabilities = tuple(
        sorted(
            {
                capability
                for task in release.artifact.tasks
                for capability in task.required_capabilities
            }
        )
    )
    if tuple(artifact["compatibility"]["required_capabilities"]) != expected_capabilities:
        raise ValueError("Community Graph Artifact capability profile is invalid")
    if artifact["release_channel"] == "STABLE" and release.artifact.passport.sample_count < 10:
        raise ValueError("Stable Community Graph Artifact has no qualified Blueprint Passport")
    return release
