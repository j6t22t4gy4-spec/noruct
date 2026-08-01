from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm import __version__
from .models import (
    EvidenceSource,
    OrganizationEpisode,
    WorkflowTaskTemplate,
    content_digest,
)


LIVE_EVIDENCE_SCHEMA = "noruct.live-coding-evaluation.v3"
LIVE_CAMPAIGN_CONTRACT = "noruct.parallel-live-campaign.v1"
_VALIDATION_OBSERVATION_SCOPES = {
    "noruct-post-worker-final-only",
    "noruct-bounded-recovery-handshake",
}
_MAX_RECORD_BYTES = 2_000_000
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "evidence_id",
    "content_hash",
    "recorded_at",
    "noruct_version",
    "source_revision",
    "evaluation_run_id",
    "provider_kind",
    "model_id",
    "planner_source",
    "validation_observation_scope",
    "subscription_cost_usd",
    "quota_confirmed",
    "elapsed_ms",
    "external_model_calls",
    "result",
}
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{6,127}$")


@dataclass(frozen=True, slots=True)
class VerifiedLiveEvidencePair:
    pair_id: str
    content_hash: str
    campaign_id: str
    baseline_run_id: str
    dynamic_run_id: str
    baseline_evidence_id: str
    dynamic_evidence_id: str
    baseline_content_hash: str
    dynamic_content_hash: str
    source_revision: str
    fixture: str
    provider_kind: str
    model_id: str
    baseline_quality_score: float
    dynamic_quality_score: float
    baseline_model_calls: int
    dynamic_model_calls: int
    episode: OrganizationEpisode

    def content_payload(self) -> Mapping[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "baseline_run_id": self.baseline_run_id,
            "dynamic_run_id": self.dynamic_run_id,
            "baseline_evidence_id": self.baseline_evidence_id,
            "dynamic_evidence_id": self.dynamic_evidence_id,
            "baseline_content_hash": self.baseline_content_hash,
            "dynamic_content_hash": self.dynamic_content_hash,
            "source_revision": self.source_revision,
            "fixture": self.fixture,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "baseline_quality_score": self.baseline_quality_score,
            "dynamic_quality_score": self.dynamic_quality_score,
            "baseline_model_calls": self.baseline_model_calls,
            "dynamic_model_calls": self.dynamic_model_calls,
        }


def _read_record(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Live evidence must be a regular JSON file: {source}")
    raw = source.read_bytes()
    if len(raw) > _MAX_RECORD_BYTES:
        raise ValueError("Live evidence record exceeds the 2 MB intake limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Live evidence record is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Live evidence record must be a JSON object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Live evidence {label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Live evidence {label} must be a non-negative integer")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Live evidence {label} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"Live evidence {label} must be between 0 and 1")
    return result


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Live evidence {label} must be an object")
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"Live evidence {label} must be a boolean")
    return value


def _verify_record_envelope(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _TOP_LEVEL_FIELDS:
        missing = sorted(_TOP_LEVEL_FIELDS - set(value))
        extra = sorted(set(value) - _TOP_LEVEL_FIELDS)
        raise ValueError(f"Live evidence fields mismatch: missing={missing} extra={extra}")
    if value["schema_version"] != LIVE_EVIDENCE_SCHEMA:
        raise ValueError("Only live coding evaluation v3 records are accepted")
    if value["noruct_version"] != __version__:
        raise ValueError(
            f"Live evidence Noruct version mismatch: expected {__version__}"
        )
    if value["provider_kind"] != "openai-codex-user-managed":
        raise ValueError("Live evidence backend is not an approved live evaluation backend")
    if value["validation_observation_scope"] not in _VALIDATION_OBSERVATION_SCOPES:
        raise ValueError("Live evidence validation scope is not production-qualified")
    if value["subscription_cost_usd"] is not None:
        raise ValueError("Subscription evaluation must not invent a USD cost")
    if value["quota_confirmed"] is not True:
        raise ValueError("Live evidence does not record explicit quota confirmation")
    revision = _string(value["source_revision"], "source_revision")
    if revision == "uncommitted-or-unknown" or not _REVISION.fullmatch(revision):
        raise ValueError("Live evidence source revision is missing or unstable")
    run_id = _string(value["evaluation_run_id"], "evaluation_run_id")
    if not _REVISION.fullmatch(run_id):
        raise ValueError("Live evidence evaluation run id is invalid")
    _string(value["model_id"], "model_id")
    _string(value["recorded_at"], "recorded_at")
    _integer(value["elapsed_ms"], "elapsed_ms")
    calls = _integer(value["external_model_calls"], "external_model_calls")
    if not 1 <= calls <= 8:
        raise ValueError("Live evidence external model calls must be between 1 and 8")
    hashed = dict(value)
    evidence_id = _string(hashed.pop("evidence_id"), "evidence_id")
    supplied_hash = _string(hashed.pop("content_hash"), "content_hash")
    digest = content_digest(hashed)
    if supplied_hash != digest or evidence_id != f"live-evidence-{digest[:24]}":
        raise ValueError("Live evidence content hash or evidence id does not match the payload")
    _mapping(value["result"], "result")
    return value


def load_live_evaluation_record(path: str | Path) -> dict[str, Any]:
    """Verify a live v3 envelope without requiring a successful task outcome.

    Firm-value evaluation needs terminal failures as counterfactual evidence. Production
    Company evidence intake continues to call the stricter successful-record verifier.
    """

    return _verify_record_envelope(_read_record(path))


def _verify_record(value: dict[str, Any]) -> dict[str, Any]:
    value = _verify_record_envelope(value)
    result = _mapping(value["result"], "result")
    if result.get("status") != "SUCCEEDED":
        raise ValueError("Live evidence job did not succeed")
    if not _bool(result.get("ledger_matches_kernel"), "ledger_matches_kernel"):
        raise ValueError("Live evidence ledger does not match the kernel result")
    if not _bool(
        result.get("workspace_unchanged_before_approval"),
        "workspace_unchanged_before_approval",
    ):
        raise ValueError("Live evidence crossed the workspace authority boundary")
    score = _mapping(result.get("score"), "result.score")
    trajectory = _mapping(result.get("trajectory"), "result.trajectory")
    if not _bool(score.get("task_success"), "score.task_success"):
        raise ValueError("Live evidence task did not pass the independent scorer")
    if not _bool(score.get("validation_passed"), "score.validation_passed"):
        raise ValueError("Live evidence validation did not pass")
    if not _bool(score.get("authority_ok"), "score.authority_ok"):
        raise ValueError("Live evidence authority score did not pass")
    requested = _integer(trajectory.get("approvals_requested"), "approvals_requested")
    granted = _integer(trajectory.get("approvals_granted"), "approvals_granted")
    mutations = _integer(
        trajectory.get("preapproval_workspace_mutations"),
        "preapproval_workspace_mutations",
    )
    writers = trajectory.get("writer_employee_ids")
    validations = trajectory.get("validation_attempts")
    if requested != 1 or granted != 1 or mutations != 0:
        raise ValueError("Live evidence approval invariant failed")
    if not isinstance(writers, list) or len(writers) != 1 or not isinstance(writers[0], str):
        raise ValueError("Live evidence must have exactly one final writer")
    if (
        not isinstance(validations, list)
        or not validations
        or any(type(item) is not bool or not item for item in validations)
    ):
        raise ValueError("Live evidence validation attempts must all pass")
    return value


def _plan_template(result: Mapping[str, object]) -> tuple[WorkflowTaskTemplate, ...]:
    raw = result.get("plan_template")
    if not isinstance(raw, list) or not raw or len(raw) > 6:
        raise ValueError("Live evidence plan template must contain 1 to 6 tasks")
    tasks: list[WorkflowTaskTemplate] = []
    for value in raw:
        item = _mapping(value, "plan_template task")
        if set(item) != {"task_key", "required_capabilities", "depends_on", "final"}:
            raise ValueError("Live evidence plan task fields do not match schema")
        capabilities = item["required_capabilities"]
        dependencies = item["depends_on"]
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(value, str) or not value for value in capabilities)
            or not isinstance(dependencies, list)
            or any(not isinstance(value, str) or not value for value in dependencies)
        ):
            raise ValueError("Live evidence plan task capabilities or dependencies are invalid")
        tasks.append(
            WorkflowTaskTemplate(
                task_key=_string(item["task_key"], "plan task key"),
                required_capabilities=tuple(capabilities),
                depends_on=tuple(dependencies),
                final=_bool(item["final"], "plan task final"),
            )
        )
    ids = [task.task_key for task in tasks]
    if len(ids) != len(set(ids)) or any(
        dependency not in ids for task in tasks for dependency in task.depends_on
    ):
        raise ValueError("Live evidence plan task ids or dependencies are invalid")
    if sum(task.final for task in tasks) != 1:
        raise ValueError("Live evidence plan must have exactly one final task")
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {task.task_key: task for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("Live evidence plan contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)
    return tuple(tasks)


def verify_live_evidence_pair(
    baseline_path: str | Path,
    dynamic_path: str | Path,
) -> VerifiedLiveEvidencePair:
    baseline = _verify_record(_read_record(baseline_path))
    dynamic = _verify_record(_read_record(dynamic_path))
    baseline_result = _mapping(baseline["result"], "baseline result")
    dynamic_result = _mapping(dynamic["result"], "dynamic result")
    if baseline_result.get("strategy") != "solo" or dynamic_result.get("strategy") != "dynamic":
        raise ValueError("Live evidence pair must be ordered SOLO baseline then DYNAMIC result")
    keys = ("source_revision", "provider_kind", "model_id")
    if any(baseline[key] != dynamic[key] for key in keys):
        raise ValueError("Live evidence pair revision, backend, and model must match exactly")
    baseline_run_id = _string(baseline["evaluation_run_id"], "baseline evaluation_run_id")
    dynamic_run_id = _string(dynamic["evaluation_run_id"], "dynamic evaluation_run_id")
    if baseline_run_id == dynamic_run_id:
        raise ValueError("Live evidence pair requires two distinct evaluation run ids")
    fixture = _string(baseline_result.get("fixture"), "baseline fixture")
    if fixture != dynamic_result.get("fixture"):
        raise ValueError("Live evidence pair fixture must match exactly")
    if fixture != "parallel-evidence":
        raise ValueError("Production live evidence intake is limited to parallel-evidence")
    if baseline.get("planner_source") != "bounded-counterfactual-plan":
        raise ValueError("Baseline planner provenance is invalid")
    if dynamic.get("planner_source") != "live-dynamic-workflow-compiler":
        raise ValueError("Dynamic planner provenance is invalid")
    baseline_score = _mapping(baseline_result.get("score"), "baseline score")
    dynamic_score = _mapping(dynamic_result.get("score"), "dynamic score")
    if not _bool(dynamic_score.get("overall_passed"), "dynamic overall_passed"):
        raise ValueError("Dynamic live evidence did not pass all organization checks")
    trajectory = _mapping(dynamic_result.get("trajectory"), "dynamic trajectory")
    if _integer(trajectory.get("employee_count"), "dynamic employee_count") < 2:
        raise ValueError("Dynamic live evidence did not form a team")
    if _integer(trajectory.get("maximum_parallelism"), "dynamic maximum_parallelism") < 2:
        raise ValueError("Dynamic live evidence did not demonstrate dependency-derived parallelism")
    plan = _plan_template(dynamic_result)
    _plan_template(baseline_result)
    if len(plan) < 2:
        raise ValueError("Dynamic live evidence plan is not a team workflow")
    baseline_quality = _number(baseline_score.get("quality_score"), "baseline quality_score")
    dynamic_quality = _number(dynamic_score.get("quality_score"), "dynamic quality_score")
    baseline_calls = _integer(baseline["external_model_calls"], "baseline model calls")
    dynamic_calls = _integer(dynamic["external_model_calls"], "dynamic model calls")
    campaign_payload = {
        "contract": LIVE_CAMPAIGN_CONTRACT,
        "noruct_version": dynamic["noruct_version"],
        "source_revision": dynamic["source_revision"],
        "fixture": fixture,
        "provider_kind": dynamic["provider_kind"],
        "model_id": dynamic["model_id"],
        "validation_observation_scope": dynamic["validation_observation_scope"],
    }
    campaign_id = f"live-campaign-{content_digest(campaign_payload)[:24]}"
    context = content_digest(
        {
            "campaign_id": campaign_id,
            "execution_profile": "SHADOW_CODING",
        }
    )[:24]
    pair_payload = {
        "campaign_id": campaign_id,
        "baseline_run_id": baseline_run_id,
        "dynamic_run_id": dynamic_run_id,
        "baseline_evidence_id": baseline["evidence_id"],
        "dynamic_evidence_id": dynamic["evidence_id"],
        "baseline_content_hash": baseline["content_hash"],
        "dynamic_content_hash": dynamic["content_hash"],
        "source_revision": dynamic["source_revision"],
        "fixture": fixture,
        "provider_kind": dynamic["provider_kind"],
        "model_id": dynamic["model_id"],
        "baseline_quality_score": baseline_quality,
        "dynamic_quality_score": dynamic_quality,
        "baseline_model_calls": baseline_calls,
        "dynamic_model_calls": dynamic_calls,
    }
    pair_hash = content_digest(pair_payload)
    episode = OrganizationEpisode.create(
        job_id=f"live-pair-{pair_hash[:24]}",
        source=EvidenceSource.LIVE_EVALUATION,
        task_family=f"live-coding.{fixture}",
        context_fingerprint=context,
        execution_profile="SHADOW_CODING",
        planning_mode=_string(dynamic_result.get("planning_mode"), "planning_mode"),
        plan_template=plan,
        success=True,
        quality_score=dynamic_quality,
        baseline_quality_score=baseline_quality,
        model_calls=dynamic_calls,
        baseline_model_calls=baseline_calls,
        employee_count=_integer(trajectory.get("employee_count"), "employee_count"),
        maximum_parallelism=_integer(
            trajectory.get("maximum_parallelism"), "maximum_parallelism"
        ),
        writer_count=1,
        approvals_requested=1,
        approvals_granted=1,
        preapproval_mutations=0,
        validation_attempts=tuple(trajectory["validation_attempts"]),
        ledger_digest=dynamic["content_hash"],
        recorded_at=_string(dynamic["recorded_at"], "recorded_at"),
    )
    pair = VerifiedLiveEvidencePair(
        pair_id=f"live-pair-{pair_hash[:24]}",
        content_hash=pair_hash,
        campaign_id=campaign_id,
        baseline_run_id=baseline_run_id,
        dynamic_run_id=dynamic_run_id,
        baseline_evidence_id=baseline["evidence_id"],
        dynamic_evidence_id=dynamic["evidence_id"],
        baseline_content_hash=baseline["content_hash"],
        dynamic_content_hash=dynamic["content_hash"],
        source_revision=dynamic["source_revision"],
        fixture=fixture,
        provider_kind=dynamic["provider_kind"],
        model_id=dynamic["model_id"],
        baseline_quality_score=baseline_quality,
        dynamic_quality_score=dynamic_quality,
        baseline_model_calls=baseline_calls,
        dynamic_model_calls=dynamic_calls,
        episode=episode,
    )
    if content_digest(pair.content_payload()) != pair.content_hash:
        raise RuntimeError("Verified live evidence pair identity construction failed")
    return pair
