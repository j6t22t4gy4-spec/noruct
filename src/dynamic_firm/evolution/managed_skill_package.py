"""Receipt-bound semantic packages for source-managed local SKILL.md trees.

Raw instruction files can be useful local context, but they are not evidence
that a Company should retain their text as organizational learning.  This
adapter therefore accepts only a user-reviewed bounded procedure and links it
to the opaque content-tree digest of one separately source-managed skill.  It
never embeds the SKILL.md body, local path, linked files, or scanner findings
in an Evolution Artifact.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


MANAGED_SKILL_RECEIPT_SCHEMA = "noruct.managed-skill-receipt.v1"


def build_managed_skill_artifact(
    *,
    artifact_id: str,
    version: str,
    skill_key: str,
    applies_to: Sequence[str],
    steps: Sequence[str],
    required_capabilities: Sequence[str],
    receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Build an EXPERIMENTAL `SKILL_PACKAGE` from reviewed semantic fields.

    Schema validation remains the authority for identifier, step and digest
    bounds.  The receipt is intentionally reduced to its SHA-256 digest before
    registration, so a local filesystem layout cannot become catalog content.
    """

    tree_sha256 = receipt.get("tree_sha256")
    if not isinstance(tree_sha256, str):
        raise ValueError("Managed skill receipt requires a content-tree digest")
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": artifact_id,
        "version": version,
        "kind": "SKILL_PACKAGE",
        "release_channel": "EXPERIMENTAL",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": list(required_capabilities),
        },
        "content": {
            "skill_key": skill_key,
            "applies_to": list(applies_to),
            "steps": list(steps),
            "required_capabilities": list(required_capabilities),
            "source_receipt_digest": tree_sha256,
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "local_managed_skill_receipt",
                "version": "1.0.0",
                "digest": tree_sha256,
            },
            "metrics": {
                "quality_score": 0.0,
                "safety_score": 0.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": ["local_user_managed_skill", "unbenchmarked_procedure"],
        },
    }
