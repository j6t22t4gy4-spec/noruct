"""One-slot live execution for the Firm Value v2 campaign."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Awaitable, Callable

from dynamic_firm.runtime.models import utc_now
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled

from .firm_value_campaign import CampaignEventKind, CampaignState
from .firm_value_v2 import LiveFirmValueV2Config, LiveFirmValueV2Record, firm_value_v2_to_json, run_live_firm_value_v2_evaluation
from .firm_value_campaign_v2 import (
    CAMPAIGN_V2_FAILURE_SCHEMA,
    FirmValueCampaignV2RunResult,
    FirmValueCampaignV2Store,
    _campaign_artifacts,
    _sha256_file,
    _validate_live_record,
    _verify_runtime_inputs,
    _write_private,
    campaign_v2_status,
)

async def run_next_campaign_v2_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    confirm_evaluator_risk: bool,
    provider_factory=None,
    coding_worker_factory=None,
    live_runner: Callable[..., Awaitable[LiveFirmValueV2Record]] | None = None,
) -> FirmValueCampaignV2RunResult:
    status = campaign_v2_status(directory)
    if status.state != CampaignState.READY or not status.next_fixture or not status.next_strategy:
        raise ValueError(f"Firm-value campaign v2 cannot run while state={status.state.value}")
    if not confirm_live_quota:
        raise ValueError("Firm-value campaign v2 requires --confirm-live-quota for one slot")
    if not confirm_evaluator_risk:
        raise ValueError(
            "Firm-value campaign v2 requires --confirm-evaluator-risk for candidate execution"
        )
    with FirmValueCampaignV2Store(directory) as store:
        metadata, _, manifest, _ = _campaign_artifacts(store)
        _verify_runtime_inputs(metadata, manifest)
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=status.next_fixture,
            strategy=status.next_strategy,
            payload={
                "attempt": 1,
                "pid": os.getpid(),
                "quota_confirmed": True,
                "evaluator_risk_confirmed": True,
                "evaluator_profile": manifest.evaluator_profile,
                "max_model_calls": manifest.max_total_model_calls,
                "max_wall_time_ms": manifest.max_wall_time_ms,
            },
        )
    config = LiveFirmValueV2Config(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_total_model_calls,
        max_wall_time_ms=manifest.max_wall_time_ms,
        quota_confirmed=True,
        evaluator_risk_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
    )
    runner = live_runner or run_live_firm_value_v2_evaluation
    try:
        record = await runner(
            config,
            status.next_fixture,
            status.next_strategy,
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
        )
        relative = Path("records-v2") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        record_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            firm_value_v2_to_json(record),
        )
        qualified = _validate_live_record(
            record_path,
            manifest,
            expected_fixture=status.next_fixture,
            expected_strategy=status.next_strategy,
        )
        with FirmValueCampaignV2Store(directory) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_fixture,
                strategy=status.next_strategy,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": qualified.content_hash,
                    "evaluation_run_id": qualified.evaluation_run_id,
                    "status": qualified.result.status,
                    "task_success": qualified.result.task_success,
                    "external_model_calls": qualified.external_model_calls,
                    "evaluator_risk_confirmed": True,
                },
            )
        return FirmValueCampaignV2RunResult(
            event=event,
            status=campaign_v2_status(directory),
            record_path=str(record_path),
            task_success=qualified.result.task_success,
        )
    except BaseException as exc:
        interrupted = isinstance(
            exc,
            (OperationCancelled, asyncio.CancelledError, KeyboardInterrupt),
        )
        kind = CampaignEventKind.RUN_INTERRUPTED if interrupted else CampaignEventKind.RUN_FAILED
        relative = Path("failures-v2") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        code = exc.code if isinstance(exc, ModelProviderError) else type(exc).__name__
        failure_payload = {
            "schema_version": CAMPAIGN_V2_FAILURE_SCHEMA,
            "benchmark_id": status.benchmark_id,
            "fixture": status.next_fixture,
            "strategy": status.next_strategy,
            "recorded_at": utc_now().isoformat(),
            "failure_code": str(code),
            "interrupted": interrupted,
            "quota_confirmed": True,
            "evaluator_risk_confirmed": True,
            "partial_result_promoted": False,
        }
        failure_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2),
        )
        with FirmValueCampaignV2Store(directory) as store:
            event = store.append(
                kind,
                fixture=status.next_fixture,
                strategy=status.next_strategy,
                payload={
                    "failure_path": relative.as_posix(),
                    "failure_file_sha256": _sha256_file(failure_path),
                    "failure_code": str(code),
                    "partial_result_promoted": False,
                },
            )
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        return FirmValueCampaignV2RunResult(
            event=event,
            status=campaign_v2_status(directory),
            record_path=None,
            task_success=False,
        )
