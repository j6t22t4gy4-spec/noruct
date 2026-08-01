"""Surface-neutral orchestration for exact remote/local effect resolution."""

from __future__ import annotations

from typing import Mapping

from dynamic_firm.product.company_coordination_settings import (
    company_coordination_config_from_settings,
)
from dynamic_firm.runtime.company_coordination import (
    RemoteCompanyCoordinationClient,
)
from dynamic_firm.runtime.interruption import EffectRecoveryOutcome
from dynamic_firm.runtime.store import RunStore


def resolve_effect_recovery(
    store: RunStore,
    *,
    settings: Mapping[str, object],
    job_id: str,
    action_id: str,
    outcome: EffectRecoveryOutcome,
    evidence_digest: str | None,
    resolved_by: str,
    reason: str,
) -> dict[str, object]:
    """Release an exact remote owner before appending local release authority.

    A prepared remote claim is treated as possibly successful even when no
    response receipt exists.  `RELEASED` and exact-owner `MISSING` are both
    closed states; an outage or authority mismatch leaves the local case
    untouched so the same command can be retried safely.
    """

    remote_claim = store.remote_effect_resource_claim(
        job_id=job_id,
        action_id=action_id,
    )
    cases = {
        str(item["action_id"]): item
        for item in store.list_job_effect_recovery_cases(job_id)
    }
    has_effect_case = action_id in cases

    if outcome.releases_resource and remote_claim is not None:
        if not has_effect_case:
            store.validate_terminal_remote_effect_resolution(
                job_id=job_id,
                action_id=action_id,
                outcome=outcome,
                evidence_digest=evidence_digest,
                resolved_by=resolved_by,
                reason=reason,
            )
        if not bool(remote_claim["remote_closed"]):
            config = company_coordination_config_from_settings(settings)
            if config is None:
                raise ValueError(
                    "Remote effect resolution requires the original enabled Company coordination authority"
                )
            client = RemoteCompanyCoordinationClient(config)
            mismatches = []
            if client.authority_digest != remote_claim["authority_digest"]:
                mismatches.append("authority")
            if client.origin != remote_claim["origin"]:
                mismatches.append("origin")
            if config.company_scope_digest != remote_claim["company_scope_digest"]:
                mismatches.append("scope")
            if config.device_id != remote_claim["device_id"]:
                mismatches.append("device")
            if mismatches:
                raise ValueError(
                    "Remote effect resolution does not match the original "
                    + "/".join(mismatches)
                    + " owner"
                )
            if has_effect_case:
                store.prepare_remote_effect_resolution(
                    job_id=job_id,
                    action_id=action_id,
                    outcome=outcome,
                    evidence_digest=evidence_digest,
                    resolved_by=resolved_by,
                    reason=reason,
                )
            released = client.release_resource_lease(
                job_id=str(remote_claim["job_id"]),
                resource_digest=str(remote_claim["resource_digest"]),
                lease_id=str(remote_claim["lease_id"]),
            )
            store.record_remote_effect_resource_release(
                job_id=job_id,
                action_id=action_id,
                remote_status="RELEASED" if released else "MISSING",
                release_reason="OPERATOR_EFFECT_RESOLUTION",
            )
            remote_claim = store.remote_effect_resource_claim(
                job_id=job_id,
                action_id=action_id,
            )
            assert remote_claim is not None

    if has_effect_case:
        local = store.resolve_effect_recovery_case(
            job_id=job_id,
            action_id=action_id,
            outcome=outcome,
            evidence_digest=evidence_digest,
            resolved_by=resolved_by,
            reason=reason,
        )
    else:
        if remote_claim is None:
            raise KeyError(f"Unknown effect recovery case: {action_id}")
        if not outcome.releases_resource:
            raise ValueError(
                "SEALED_UNKNOWN requires an indeterminate handler recovery case"
            )
        local = store.resolve_terminal_remote_effect_resource(
            job_id=job_id,
            action_id=action_id,
            outcome=outcome,
            evidence_digest=evidence_digest,
            resolved_by=resolved_by,
            reason=reason,
        )

    result: dict[str, object] = dict(local)
    if remote_claim is None:
        result.update(
            {
                "remote_claim_prepared": False,
                "remote_resource_released": None,
                "remote_status": "LOCAL_ONLY",
            }
        )
    elif outcome is EffectRecoveryOutcome.SEALED_UNKNOWN:
        result.update(
            {
                "remote_claim_prepared": True,
                "remote_resource_released": False,
                "remote_status": "SEALED_UNKNOWN",
            }
        )
    else:
        result.update(
            {
                "remote_claim_prepared": True,
                "remote_resource_released": bool(remote_claim["remote_closed"]),
                "remote_status": str(remote_claim["remote_status"]),
            }
        )
    return result
