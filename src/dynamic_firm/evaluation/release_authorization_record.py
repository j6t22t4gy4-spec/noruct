"""Fail-closed decoding for live Release Authorization records."""

from __future__ import annotations

from dynamic_firm import __version__

def load_live_release_authorization_record(
    path: Path,
) -> LiveReleaseAuthorizationRecord:
    source = path.expanduser().resolve()
    if (
        not source.is_file()
        or source.is_symlink()
        or source.stat().st_size > 1_000_000
    ):
        raise ValueError(
            "Release-authorization live record must be a bounded regular file"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Release-authorization live record cannot be read") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != RELEASE_AUTHORIZATION_LIVE_RUN_SCHEMA
        or value.get("evidence_class") != INFORMATION_BOUNDARY_LIVE_EVIDENCE_CLASS
    ):
        raise ValueError("Release-authorization live record schema is incompatible")
    expected_keys = {
        "schema_version",
        "evidence_class",
        "evidence_id",
        "content_hash",
        "recorded_at",
        "noruct_version",
        "preflight_benchmark_id",
        "preflight_content_hash",
        "source_revision",
        "distribution_sha256",
        "provider_kind",
        "model_id",
        "authority_profile",
        "company_revision",
        "roster_revision",
        "playbook_revision",
        "memory_revision",
        "fixture_revision",
        "benchmark_revision",
        "strategy",
        "identity",
        "status",
        "task_success",
        "artifact",
        "safety",
        "admission",
        "cost",
        "trajectory",
        "validation",
        "provider_request_refs",
        "configured_model_call_limit",
        "configured_wall_time_ms",
        "elapsed_ms",
        "external_model_calls",
        "quota_confirmed",
    }
    if set(value) != expected_keys:
        raise ValueError("Release-authorization live record fields changed")
    projections = tuple(
        value[name]
        for name in (
            "identity",
            "artifact",
            "safety",
            "admission",
            "cost",
            "trajectory",
            "validation",
        )
    )
    if not all(isinstance(item, dict) for item in projections):
        raise ValueError("Release-authorization live projections are invalid")
    identity_value = value["identity"]
    artifact_value = value["artifact"]
    safety_value = value["safety"]
    admission_value = value["admission"]
    cost_value = value["cost"]
    trajectory_value = value["trajectory"]
    validation_value = value["validation"]
    checks_value = artifact_value.get("checks")
    validation_checks_value = validation_value.get("failed_checks")
    if (
        not isinstance(checks_value, list)
        or not isinstance(validation_checks_value, list)
    ):
        raise ValueError("Release-authorization live checks are invalid")
    artifact = InformationBoundaryArtifactProjection(
        **{
            key: item
            for key, item in artifact_value.items()
            if key not in {"checks", "changed_paths"}
        },
        changed_paths=tuple(str(item) for item in artifact_value["changed_paths"]),
        checks=tuple(InformationBoundaryCheck(**item) for item in checks_value),
    )
    identity = _evaluation_identity_from_payload(identity_value)
    record = LiveReleaseAuthorizationRecord(
        **{
            key: item
            for key, item in value.items()
            if key
            not in {
                "identity",
                "artifact",
                "safety",
                "admission",
                "cost",
                "trajectory",
                "validation",
                "provider_request_refs",
            }
        },
        identity=identity,
        artifact=artifact,
        safety=InformationBoundarySafetyProjection(**safety_value),
        admission=InformationBoundaryAdmissionProjection(
            **{
                **admission_value,
                "decision_reasons": tuple(admission_value["decision_reasons"]),
                "admitted_capabilities": tuple(admission_value["admitted_capabilities"]),
            }
        ),
        cost=InformationBoundaryCostProjection(**cost_value),
        trajectory=_trajectory_from_payload(trajectory_value),
        validation=ReleaseAuthorizationValidationProjection(
            **{
                **validation_value,
                "failed_checks": tuple(validation_checks_value),
            }
        ),
        provider_request_refs=tuple(str(item) for item in value["provider_request_refs"]),
    )
    expected_identity = release_authorization_live_identity(
        strategy=record.strategy,
        model_profile=record.model_id,
        company_revision=record.company_revision,
        roster_revision=record.roster_revision,
        playbook_revision=record.playbook_revision,
        max_total_model_calls=record.configured_model_call_limit,
        max_wall_time_ms=record.configured_wall_time_ms,
    )
    boolean_fields = (
        record.validation.disposition_match,
        record.validation.public_basis_match,
        record.validation.policy_basis_match,
        record.validation.required_action_match,
        record.validation.capability_signal_match,
        record.validation.no_memory_identifier_leak,
    )
    allowed_failed_checks = {
        "capability-signal",
        "disposition",
        "memory-identifier-leak",
        "policy-basis",
        "public-basis",
        "required-action",
        "task-contract",
        "unexpected-signal",
    }
    if (
        record.content_hash != content_digest(record.content_payload())
        or record.evidence_id
        != f"release-authorization-live-evidence-{record.content_hash[:24]}"
        or record.noruct_version != __version__
        or record.strategy not in INFORMATION_BOUNDARY_LIVE_STRATEGIES
        or record.identity.strategy != record.strategy
        or record.identity.workload_hash != expected_identity.workload_hash
        or record.identity.run_id != expected_identity.run_id
        or record.external_model_calls != record.cost.runtime_model_calls
        or record.validation.attempt_count != record.external_model_calls
        or record.external_model_calls > record.configured_model_call_limit
        or record.cost.tool_calls != 0
        or record.cost.total_tokens
        != record.cost.input_tokens + record.cost.output_tokens
        or min(
            record.cost.runtime_model_calls,
            record.cost.tool_calls,
            record.cost.input_tokens,
            record.cost.output_tokens,
            record.cost.total_tokens,
            record.elapsed_ms,
        )
        < 0
        or record.elapsed_ms > record.configured_wall_time_ms
        or not record.quota_confirmed
        or len(record.provider_request_refs) > record.configured_model_call_limit
        or len(set(record.provider_request_refs)) != len(record.provider_request_refs)
        or type(record.validation.passed) is not bool
        or type(record.validation.repair_used) is not bool
        or any(type(item) is not bool for item in boolean_fields)
        or not 1 <= record.validation.attempt_count <= record.configured_model_call_limit
        or len(set(record.validation.failed_checks))
        != len(record.validation.failed_checks)
        or any(
            check not in allowed_failed_checks
            for check in record.validation.failed_checks
        )
        or (
            record.validation.passed
            and not all(boolean_fields)
        )
        or any(
            not reference.startswith("provider-request-sha256:")
            or len(reference) != len("provider-request-sha256:") + 64
            or any(
                character not in "0123456789abcdef"
                for character in reference.removeprefix(
                    "provider-request-sha256:"
                )
            )
            for reference in record.provider_request_refs
        )
    ):
        raise ValueError("Release-authorization live record contract is invalid")
    return record
