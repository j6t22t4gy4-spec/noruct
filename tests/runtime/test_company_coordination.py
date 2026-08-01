from __future__ import annotations

import os
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from dynamic_firm.runtime.company_coordination import (
    CompanyCoordinationError,
    RemoteCompanyCoordinationClient,
    RemoteCompanyCoordinationConfig,
    company_coordination_authority_digest,
)


class CompanyCoordinationClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = "a" * 64
        self.request_hash = "b" * 64
        self.graph_hash = "c" * 64
        self.result_hash = "d" * 64
        self.continuation_id = "continuation-" + "e" * 32
        self.previous = os.environ.get("NORUCT_TEST_HANDOFF_TOKEN")
        os.environ["NORUCT_TEST_HANDOFF_TOKEN"] = "fixture-handoff-token"
        self.client = RemoteCompanyCoordinationClient(
            RemoteCompanyCoordinationConfig(
                endpoint="https://coordination.example.test",
                company_scope_digest=self.scope,
                device_id="device-laptop-a",
                token_env="NORUCT_TEST_HANDOFF_TOKEN",
            )
        )

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("NORUCT_TEST_HANDOFF_TOKEN", None)
        else:
            os.environ["NORUCT_TEST_HANDOFF_TOKEN"] = self.previous

    def test_authority_digest_canonicalizes_origin_and_excludes_device(self) -> None:
        first = RemoteCompanyCoordinationConfig(
            endpoint="https://Coordination.Example.Test:443",
            company_scope_digest=self.scope,
            device_id="device-laptop-a",
            token_env="NORUCT_TEST_HANDOFF_TOKEN",
        )
        second = RemoteCompanyCoordinationConfig(
            endpoint="https://coordination.example.test",
            company_scope_digest=self.scope,
            device_id="device-laptop-b",
            token_env="NORUCT_TEST_HANDOFF_TOKEN",
        )
        self.assertEqual(
            company_coordination_authority_digest(first),
            company_coordination_authority_digest(second),
        )
        changed_token_slot = RemoteCompanyCoordinationConfig(
            endpoint=second.endpoint,
            company_scope_digest=second.company_scope_digest,
            device_id=second.device_id,
            token_env="NORUCT_DIFFERENT_TOKEN_SLOT",
        )
        self.assertNotEqual(
            company_coordination_authority_digest(second),
            company_coordination_authority_digest(changed_token_slot),
        )
        self.assertRegex(company_coordination_authority_digest(None), r"^[0-9a-f]{64}$")
        invalid_posture = RemoteCompanyCoordinationConfig(
            endpoint="http://localhost:8787",
            company_scope_digest=self.scope,
            device_id="device-laptop-a",
            token_env="NORUCT_TEST_HANDOFF_TOKEN",
            allow_insecure_loopback=1,  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(CompanyCoordinationError, "boolean"):
            company_coordination_authority_digest(invalid_posture)

    def test_preclaim_handoff_uses_only_opaque_identity_and_one_target(self) -> None:
        receipt = {
            "schema": "noruct.company-coordination-partial-continuation-handoff.v1",
            "status": "TRANSFERRED",
            "job_id": "job-12345678",
            "continuation_id": self.continuation_id,
            "target_device_id": "device-laptop-b",
            "idempotent": False,
        }
        with patch(
            "dynamic_firm.runtime.company_coordination._request_json",
            return_value=receipt,
        ) as request:
            self.client.handoff_partial_continuation(
                job_id="job-12345678",
                continuation_id=self.continuation_id,
                request_snapshot_hash=self.request_hash,
                graph_digest=self.graph_hash,
                completed_attempt_ids=("attempt-one",),
                completed_results_digest=self.result_hash,
                target_device_id="device-laptop-b",
            )

        self.assertTrue(str(request.call_args.kwargs["endpoint"]).endswith("/handoff"))
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["target_device_id"], "device-laptop-b")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("result", payload)

    def test_handoff_rejects_self_target_before_network(self) -> None:
        with patch("dynamic_firm.runtime.company_coordination._request_json") as request:
            with self.assertRaisesRegex(CompanyCoordinationError, "target"):
                self.client.handoff_partial_continuation(
                    job_id="job-12345678",
                    continuation_id=self.continuation_id,
                    request_snapshot_hash=self.request_hash,
                    graph_digest=self.graph_hash,
                    completed_attempt_ids=("attempt-one",),
                    completed_results_digest=self.result_hash,
                    target_device_id="device-laptop-a",
                )
        request.assert_not_called()

    def test_preflight_preserves_non_worker_http_status_without_reflecting_body(self) -> None:
        rejected = HTTPError(
            "https://coordination.example.test/v1/company-coordination/identity",
            403,
            "Forbidden",
            hdrs=None,
            fp=BytesIO(b"error code: 1010\nprivate proxy detail"),
        )
        with patch(
            "dynamic_firm.runtime.company_coordination.build_opener"
        ) as opener:
            opener.return_value.open.side_effect = rejected
            with self.assertRaisesRegex(
                CompanyCoordinationError,
                r"HTTP 403 without a structured service error",
            ) as error:
                self.client.preflight_identity()
        self.assertEqual(
            opener.return_value.open.call_args.args[0].get_header("User-agent"),
            "Noruct/0.0.80 (company-coordination)",
        )
        self.assertNotIn("private proxy detail", str(error.exception))

    def test_preflight_preserves_safe_worker_rejection_code(self) -> None:
        rejected = HTTPError(
            "https://coordination.example.test/v1/company-coordination/identity",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"code":"COORDINATION_UNAUTHORIZED"}'),
        )
        with patch(
            "dynamic_firm.runtime.company_coordination.build_opener"
        ) as opener:
            opener.return_value.open.side_effect = rejected
            with self.assertRaisesRegex(
                CompanyCoordinationError,
                r"HTTP 401: COORDINATION_UNAUTHORIZED",
            ):
                self.client.preflight_identity()
