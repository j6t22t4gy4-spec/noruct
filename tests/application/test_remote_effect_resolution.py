from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.application.job_cli import run_job_command
from dynamic_firm.runtime.company_coordination import (
    CompanyCoordinationError,
    RemoteCompanyCoordinationClient,
    RemoteCompanyCoordinationConfig,
)
from dynamic_firm.runtime.interruption import EffectInterruptionReason
from dynamic_firm.runtime.models import IdempotencyMode, ToolCall, ToolEffect
from dynamic_firm.runtime.store import RunStore
from tests.runtime.helpers import make_request


_SCOPE = "b" * 64
_DEVICE = "device-effect-owner"
_TOKEN_ENV = "NORUCT_TEST_REMOTE_EFFECT_TOKEN"
_ACTION_ID = "a" * 64
_JOB_ID = "job-12345678"
_RESOURCE_KEY = "workspace:remote-effect-recovery"
_RESOURCE_DIGEST = hashlib.sha256(
    f"noruct.effect-resource.v1|{_RESOURCE_KEY}".encode("utf-8")
).hexdigest()
_EVIDENCE = hashlib.sha256(b"operator-held remote effect evidence").hexdigest()
_RELEASE_SCHEMA = "noruct.company-coordination-resource-lease-release.v1"


def _settings(
    *,
    scope: str = _SCOPE,
    device: str = _DEVICE,
    endpoint: str = "https://Coordination.Example.Test:443",
):
    return {
        "company_coordination": {
            "enabled": True,
            "endpoint": endpoint,
            "company_scope_digest": scope,
            "device_id": device,
            "token_env": _TOKEN_ENV,
        }
    }


def _namespace(*, outcome: str = "confirmed-no-effect") -> Namespace:
    return Namespace(
        job_command="effect-resolve",
        job_id=_JOB_ID,
        action_id=_ACTION_ID,
        outcome=outcome,
        evidence_digest=None if outcome == "seal-unknown" else _EVIDENCE,
        operator_id="operator-remote-effect",
        reason="exact remote owner recovery evidence reviewed",
        confirm=True,
        json=True,
    )


def _stage_remote_claim(
    state_path: Path,
    *,
    handler_started: bool,
) -> str:
    store = RunStore(state_path)
    base_request = make_request(request_id=f"remote-effect-{handler_started}")
    request = replace(
        base_request,
        task=replace(base_request.task, job_id=_JOB_ID),
    )
    handle, created = store.create_run(request)
    if not created:
        raise AssertionError("remote effect fixture request must be unique")
    store.begin_run(handle.run_id)
    store.record_tool_intent(
        handle.run_id,
        _ACTION_ID,
        1,
        ToolCall("remote-effect-call", "workspace_write", {"secret": "redacted"}),
        hashlib.sha256(b"arguments").hexdigest(),
        _RESOURCE_KEY,
        effect=ToolEffect.WRITE,
        idempotency_mode=IdempotencyMode.NONE.value,
    )
    config = RemoteCompanyCoordinationConfig(
        endpoint="https://Coordination.Example.Test:443",
        company_scope_digest=_SCOPE,
        device_id=_DEVICE,
        token_env=_TOKEN_ENV,
    )
    client = RemoteCompanyCoordinationClient(config)
    store.prepare_remote_effect_resource_claim(
        _ACTION_ID,
        authority_digest=client.authority_digest,
        origin=client.origin,
        company_scope_digest=_SCOPE,
        device_id=_DEVICE,
        resource_digest=_RESOURCE_DIGEST,
        lease_id=f"coord-lease-{_ACTION_ID}",
    )
    if handler_started:
        if not store.acquire_effect_resource_lease(
            action_id=_ACTION_ID,
            run_id=handle.run_id,
            effect=ToolEffect.WRITE,
            resource_key=_RESOURCE_KEY,
        ):
            raise AssertionError("fixture local resource lease was not acquired")
        store.mark_tool_started(_ACTION_ID)
        store.mark_tool_effect_indeterminate(
            _ACTION_ID,
            cause=EffectInterruptionReason.PROCESS_OR_MACHINE_LOSS,
        )
    store.recover_interrupted_runs()
    store.close()
    return request.task.job_id


def _release_response(status: str) -> dict[str, object]:
    return {"schema": _RELEASE_SCHEMA, "status": status}


class RemoteEffectResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_token = os.environ.get(_TOKEN_ENV)
        os.environ[_TOKEN_ENV] = "remote-effect-fixture-token"

    def tearDown(self) -> None:
        if self.previous_token is None:
            os.environ.pop(_TOKEN_ENV, None)
        else:
            os.environ[_TOKEN_ENV] = self.previous_token

    def _run(
        self,
        state_path: Path,
        *,
        outcome: str = "confirmed-no-effect",
        settings=None,  # type: ignore[no-untyped-def]
    ) -> dict[str, object]:
        output = io.StringIO()
        code = run_job_command(
            _namespace(outcome=outcome),
            state_path=state_path,
            settings=_settings() if settings is None else settings,
            output=output,
        )
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_exact_remote_release_precedes_local_resolution_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            job_id = _stage_remote_claim(state_path, handler_started=True)
            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                return_value=_release_response("RELEASED"),
            ) as request:
                first = self._run(state_path)

            self.assertTrue(first["resource_released"])
            self.assertTrue(first["remote_resource_released"])
            self.assertEqual(first["remote_status"], "RELEASED")
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["company_scope_digest"], _SCOPE)
            self.assertEqual(payload["device_id"], _DEVICE)
            self.assertEqual(payload["resource_digest"], _RESOURCE_DIGEST)
            self.assertEqual(payload["lease_id"], f"coord-lease-{_ACTION_ID}")
            self.assertNotIn("resource_key", payload)
            self.assertNotIn("secret", json.dumps(payload))

            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                side_effect=AssertionError("durable retry must not repeat remote release"),
            ):
                repeated = self._run(state_path, settings={})
            self.assertEqual(repeated["resolved_at"], first["resolved_at"])
            reopened = RunStore(state_path)
            try:
                case = reopened.list_job_effect_recovery_cases(job_id)[0]
                remote = reopened.remote_effect_resource_claim(
                    job_id=job_id,
                    action_id=_ACTION_ID,
                )
            finally:
                reopened.close()
            self.assertEqual(case["case_status"], "RESOLVED")
            assert remote is not None
            self.assertTrue(remote["remote_closed"])

    def test_claim_response_outage_leaves_local_case_open_and_retry_accepts_exact_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            job_id = _stage_remote_claim(state_path, handler_started=True)
            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                side_effect=CompanyCoordinationError("fixture response lost"),
            ):
                with self.assertRaisesRegex(CompanyCoordinationError, "response lost"):
                    self._run(state_path)
            reopened = RunStore(state_path)
            try:
                case = reopened.list_job_effect_recovery_cases(job_id)[0]
                remote = reopened.remote_effect_resource_claim(
                    job_id=job_id,
                    action_id=_ACTION_ID,
                )
            finally:
                reopened.close()
            self.assertEqual(case["case_status"], "OPEN")
            self.assertTrue(case["lease_held"])
            assert remote is not None
            self.assertFalse(remote["remote_closed"])

            # The server may have released before its response was lost. An
            # exact-owner MISSING receipt is a safe idempotent closure.
            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                return_value=_release_response("MISSING"),
            ):
                recovered = self._run(state_path)
            self.assertEqual(recovered["remote_status"], "MISSING")
            self.assertTrue(recovered["resource_released"])

    def test_wrong_authority_scope_or_device_never_contacts_remote_or_releases_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            job_id = _stage_remote_claim(state_path, handler_started=True)
            for wrong in (
                _settings(scope="c" * 64),
                _settings(device="device-wrong-owner"),
                _settings(endpoint="https://different.example.test"),
            ):
                with self.subTest(settings=wrong):
                    with patch(
                        "dynamic_firm.runtime.company_coordination._request_json",
                        side_effect=AssertionError("wrong owner must fail before network"),
                    ):
                        with self.assertRaisesRegex(ValueError, "original .*owner"):
                            self._run(state_path, settings=wrong)
            reopened = RunStore(state_path)
            try:
                case = reopened.list_job_effect_recovery_cases(job_id)[0]
            finally:
                reopened.close()
            self.assertEqual(case["case_status"], "OPEN")
            self.assertTrue(case["lease_held"])

    def test_prehandler_crash_closes_both_possible_claim_outcomes_without_effect_case(self) -> None:
        for remote_status in ("RELEASED", "MISSING"):
            with self.subTest(remote_status=remote_status):
                with tempfile.TemporaryDirectory() as directory:
                    state_path = Path(directory) / "runtime.db"
                    job_id = _stage_remote_claim(state_path, handler_started=False)
                    with patch(
                        "dynamic_firm.runtime.company_coordination._request_json",
                        return_value=_release_response(remote_status),
                    ):
                        result = self._run(state_path)
                    self.assertTrue(result["resource_released"])
                    self.assertTrue(result["remote_resource_released"])
                    self.assertEqual(result["remote_status"], remote_status)
                    reopened = RunStore(state_path)
                    try:
                        self.assertEqual(
                            reopened.list_job_effect_recovery_cases(job_id),
                            (),
                        )
                        remote = reopened.remote_effect_resource_claim(
                            job_id=job_id,
                            action_id=_ACTION_ID,
                        )
                    finally:
                        reopened.close()
                    assert remote is not None
                    self.assertEqual(
                        remote["remote_resolution_outcome"],
                        "CONFIRMED_NO_EFFECT",
                    )
                    reopened = RunStore(state_path)
                    try:
                        projection = reopened.list_job_remote_effect_resource_claims(
                            job_id
                        )
                    finally:
                        reopened.close()
                    self.assertEqual(projection[0]["case_status"], "CLOSED")
                    self.assertEqual(projection[0]["next_action"], "NONE")

    def test_remote_only_projection_is_content_free_read_only_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            job_id = _stage_remote_claim(state_path, handler_started=False)
            store = RunStore(state_path)
            before_changes = store._conn.total_changes
            with patch.dict(os.environ, {}, clear=True), patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                side_effect=AssertionError("recovery projection must not use network"),
            ) as request:
                projected = store.list_job_remote_effect_resource_claims(job_id)
            after_changes = store._conn.total_changes
            store.close()

            self.assertEqual(before_changes, after_changes)
            request.assert_not_called()
            self.assertEqual(len(projected), 1)
            self.assertEqual(
                set(projected[0]),
                {
                    "job_id",
                    "action_id",
                    "effect",
                    "action_status",
                    "run_status",
                    "case_status",
                    "remote_status",
                    "resolution_outcome",
                    "resolved_at",
                    "next_action",
                },
            )
            self.assertEqual(projected[0]["action_id"], _ACTION_ID)
            self.assertEqual(
                projected[0]["next_action"],
                "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER",
            )
            serialized = json.dumps(projected)
            self.assertNotIn(_RESOURCE_KEY, serialized)
            self.assertNotIn(_RESOURCE_DIGEST, serialized)
            self.assertNotIn(_DEVICE, serialized)
            self.assertNotIn("Coordination.Example.Test", serialized)
            self.assertNotIn("token", serialized.lower())

            # Once a handler-started action has a richer effect case, the
            # remote-only projection must not create a duplicate operator case.
            duplicate_path = Path(directory) / "duplicate.db"
            duplicate_job_id = _stage_remote_claim(
                duplicate_path,
                handler_started=True,
            )
            duplicate_store = RunStore(duplicate_path)
            try:
                self.assertEqual(
                    duplicate_store.list_job_remote_effect_resource_claims(
                        duplicate_job_id
                    ),
                    (),
                )
                self.assertEqual(
                    len(duplicate_store.list_job_effect_recovery_cases(duplicate_job_id)),
                    1,
                )
            finally:
                duplicate_store.close()

    def test_sealed_unknown_never_reads_credential_or_releases_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "runtime.db"
            job_id = _stage_remote_claim(state_path, handler_started=True)
            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                side_effect=AssertionError("sealed unknown must not contact remote"),
            ):
                result = self._run(
                    state_path,
                    outcome="seal-unknown",
                    settings={},
                )
            self.assertFalse(result["resource_released"])
            self.assertFalse(result["remote_resource_released"])
            self.assertEqual(result["remote_status"], "SEALED_UNKNOWN")
            reopened = RunStore(state_path)
            try:
                case = reopened.list_job_effect_recovery_cases(job_id)[0]
                remote = reopened.remote_effect_resource_claim(
                    job_id=job_id,
                    action_id=_ACTION_ID,
                )
            finally:
                reopened.close()
            self.assertEqual(case["case_status"], "SEALED_UNKNOWN")
            assert remote is not None
            self.assertFalse(remote["remote_closed"])
            with patch(
                "dynamic_firm.runtime.company_coordination._request_json",
                side_effect=AssertionError(
                    "a final seal must reject release before network"
                ),
            ):
                with self.assertRaisesRegex(ValueError, "different resolution"):
                    self._run(state_path)


if __name__ == "__main__":
    unittest.main()
