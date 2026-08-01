from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company.models import content_digest
from dynamic_firm.evolution.artifact_bundle import (
    MAX_ARTIFACT_SIGNATURE_BYTES,
    fetch_artifact_registry_signature,
)
from dynamic_firm.evolution.hosted_transport import (
    HostedTransportError,
    authorize_artifact_registry_publication,
    list_operator_candidates,
    publish_artifact_registry,
    probe_public_service,
    record_candidate_evaluation,
    retire_artifact_registry,
    submit_capsule,
)
from dynamic_firm.evolution.score_contract import evolution_content_digest
from dynamic_firm.evolution.signing import (
    MAX_OPENSSH_SIGNATURE_BYTES,
    verify_openssh_signature,
    verify_openssh_signature_bytes,
)


REGISTRY_ID = "fixture_registry"
AUTHORIZATION_ID = "authorization-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _ssh_signature(size: int) -> bytes:
    prefix = b"-----BEGIN SSH SIGNATURE-----\n"
    suffix = b"\n-----END SSH SIGNATURE-----\n"
    if size < len(prefix) + len(suffix):
        raise ValueError("size cannot contain an SSH signature envelope")
    return prefix + (b"A" * (size - len(prefix) - len(suffix))) + suffix


class TransportRedirectTests(unittest.TestCase):
    def test_public_probe_uses_no_credential_and_checks_only_public_endpoints(self) -> None:
        class ProbeHandler(BaseHTTPRequestHandler):
            authorizations: list[str | None] = []

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self.__class__.authorizations.append(self.headers.get("authorization"))
                if self.path == "/health":
                    payload = {"service": "noruct-evolution-network", "status": "ok"}
                elif self.path == "/v1/artifact-registries":
                    payload = {
                        "schema": "noruct.public-evolution-artifact-registry-index.v1",
                        "registries": [],
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_public_service(
                f"http://127.0.0.1:{server.server_port}",
                allow_insecure_loopback=True,
            )
            self.assertEqual(result["worker_health"], "REACHABLE")
            self.assertEqual(result["public_registry_count"], 0)
            self.assertFalse(result["credential_sent"])
            self.assertEqual(ProbeHandler.authorizations, [None, None])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cross_origin_redirect_never_receives_bearer_authorization(self) -> None:
        class TargetHandler(BaseHTTPRequestHandler):
            requests: list[str | None] = []

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self.__class__.requests.append(self.headers.get("authorization"))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"schema":"unexpected"}')

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        target_url = f"http://127.0.0.1:{target.server_port}/credential-target"

        class RedirectHandler(BaseHTTPRequestHandler):
            requests: list[str | None] = []

            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self.__class__.requests.append(self.headers.get("authorization"))
                self.send_response(302)
                self.send_header("location", target_url)
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        endpoint = f"http://127.0.0.1:{redirect.server_port}"
        try:
            with self.assertRaisesRegex(HostedTransportError, "does not follow redirects"):
                list_operator_candidates(
                    endpoint=endpoint,
                    token="secret-reviewer-token",
                    allow_insecure_loopback=True,
                )
            with self.assertRaisesRegex(ValueError, "does not follow redirects"):
                fetch_artifact_registry_signature(
                    f"{endpoint}/registry.sig", allow_insecure_loopback=True
                )
            self.assertEqual(
                RedirectHandler.requests,
                ["Bearer secret-reviewer-token", None],
            )
            self.assertEqual(TargetHandler.requests, [])
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)


class ReceiptBindingTests(unittest.TestCase):
    def test_intake_receipt_uses_cross_runtime_score_digest_and_closed_shape(self) -> None:
        capsule_id = "capsule-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        capsule = {
            "schema": "fixture",
            "outcome": {"quality_score": 1, "safety_score": 1},
        }
        capsule_digest = evolution_content_digest(capsule)
        unsigned = {
            "schema": "noruct.evolution-network-intake-receipt.v1",
            "event_type": "ACCEPTED",
            "contribution_id": "contribution-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "capsule_id": capsule_id,
            "capsule_digest": capsule_digest,
            "recorded_at": "2026-07-21T00:00:00+00:00",
        }
        valid = {
            **unsigned,
            "expires_at": "2026-08-20T00:00:00+00:00",
            "receipt_digest": hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "withdrawal_capability": "b" * 64,
            "idempotent": False,
        }

        def submit(receipt: dict[str, object]):
            with patch(
                "dynamic_firm.evolution.hosted_transport._request_json",
                return_value=receipt,
            ):
                return submit_capsule(
                    endpoint="http://127.0.0.1:1",
                    token="fixture-token",
                    capsule_id=capsule_id,
                    capsule=capsule,
                    consent={
                        "purpose": "SHARED_EVOLUTION_IMPROVEMENT",
                        "allowed_reuse": "EVALUATE_AND_PROMOTE_VERSIONED_ARTIFACT",
                        "authority": "INDIVIDUAL",
                        "retention_days": 30,
                    },
                    withdrawal_capability="b" * 64,
                    allow_insecure_loopback=True,
                )

        self.assertEqual(submit(valid).capsule_digest, capsule_digest)
        for invalid in (
            {key: value for key, value in valid.items() if key != "idempotent"},
            {**valid, "idempotent": 0},
            {**valid, "unexpected": True},
        ):
            with self.assertRaisesRegex(HostedTransportError, "unsupported shape"):
                submit(invalid)

    def _authorization_response(self, payload: dict[str, object]) -> dict[str, object]:
        evidence = {
            "candidate_evidence_digests": payload["candidate_evidence_digests"],
            "evaluation_evidence_digests": payload["evaluation_evidence_digests"],
            "artifact_manifest_digests": payload["artifact_manifest_digests"],
        }
        return {
            "schema": "noruct.evolution-artifact-registry-publication-authorization-receipt.v1",
            "authorization_id": AUTHORIZATION_ID,
            "authorization_digest": content_digest(payload),
            "registry_id": REGISTRY_ID,
            "bundle_digest": "a" * 64,
            "evidence_digest": content_digest(evidence),
            "status": "PENDING",
            "authorized_at": "2026-07-21T00:00:00+00:00",
            "idempotent": False,
        }

    def _authorize(self) -> dict[str, object]:
        return dict(
            authorize_artifact_registry_publication(
                endpoint="http://127.0.0.1:1",
                token="reviewer-token",
                registry_id=REGISTRY_ID,
                bundle_digest="a" * 64,
                candidate_evidence_digests=("c" * 64, "b" * 64),
                evaluation_evidence_digests=("e" * 64, "d" * 64),
                artifact_manifest_digests=("f" * 64,),
                reviewer_id="reviewer_fixture",
                reason_code="accepted_evidence",
                allow_insecure_loopback=True,
            )
        )

    def test_authorization_receipt_is_closed_and_digest_bound_to_normalized_request(self) -> None:
        def valid_response(**kwargs: object) -> dict[str, object]:
            return self._authorization_response(dict(kwargs["payload"]))  # type: ignore[arg-type]

        with patch(
            "dynamic_firm.evolution.hosted_transport._request_json",
            side_effect=valid_response,
        ):
            response = self._authorize()
        self.assertFalse(response["idempotent"])

        def tampered(field: str, value: object) -> None:
            def response(**kwargs: object) -> dict[str, object]:
                result = self._authorization_response(dict(kwargs["payload"]))  # type: ignore[arg-type]
                result[field] = value
                return result

            with patch(
                "dynamic_firm.evolution.hosted_transport._request_json",
                side_effect=response,
            ):
                with self.assertRaisesRegex(HostedTransportError, "unsupported shape"):
                    self._authorize()

        tampered("authorization_digest", "f" * 64)
        tampered("evidence_digest", "f" * 64)
        tampered("idempotent", 0)
        tampered("unexpected", True)

    def test_evaluation_receipt_digest_is_bound_to_normalized_evaluation(self) -> None:
        evaluation = {
            "schema": "noruct.evolution-candidate-evaluation.v1",
            "suite_id": "public_fixture",
            "suite_version": "1.0.0",
            "suite_digest": "f" * 64,
            "evaluator_id": "offline_fixture",
            "fixture_scope": "PUBLIC",
            "quality_score": 0.9,
            "safety_score": 0.95,
            "cost_bucket": "LOW",
            "decision": "PASS",
        }
        valid = {
            "status": "OPERATOR_REVIEW_READY",
            "candidate_id": "candidate-" + "a" * 32,
            "evaluation_digest": content_digest(evaluation),
            "evaluated_at": "2026-07-21T00:01:00+00:00",
            "idempotent": False,
        }
        with patch(
            "dynamic_firm.evolution.hosted_transport._request_json", return_value=valid
        ):
            response = record_candidate_evaluation(
                endpoint="http://127.0.0.1:1",
                token="finalizer-token",
                candidate_id="candidate-" + "a" * 32,
                evaluation=evaluation,
                allow_insecure_loopback=True,
            )
        self.assertEqual(response["evaluation_digest"], content_digest(evaluation))

        with patch(
            "dynamic_firm.evolution.hosted_transport._request_json",
            return_value={**valid, "evaluation_digest": "0" * 64},
        ):
            with self.assertRaisesRegex(HostedTransportError, "unsupported shape"):
                record_candidate_evaluation(
                    endpoint="http://127.0.0.1:1",
                    token="finalizer-token",
                    candidate_id="candidate-" + "a" * 32,
                    evaluation=evaluation,
                    allow_insecure_loopback=True,
                )

    def test_publication_receipt_is_closed_and_bound_to_raw_signature(self) -> None:
        signature = _ssh_signature(512)
        bundle = {"registry_id": REGISTRY_ID, "bundle_digest": "a" * 64}
        valid = {
            "schema": "noruct.evolution-artifact-registry-publication-receipt.v1",
            "authorization_id": AUTHORIZATION_ID,
            "registry_id": REGISTRY_ID,
            "bundle_digest": "a" * 64,
            "signature_digest": hashlib.sha256(signature).hexdigest(),
            "published_at": "2026-07-21T00:02:00+00:00",
            "status": "ACTIVE",
            "idempotent": False,
        }

        def publish(receipt: dict[str, object]) -> dict[str, object]:
            with patch(
                "dynamic_firm.evolution.hosted_transport._request_json",
                return_value=receipt,
            ):
                return dict(
                    publish_artifact_registry(
                        endpoint="http://127.0.0.1:1",
                        token="publisher-token",
                        registry_id=REGISTRY_ID,
                        authorization_id=AUTHORIZATION_ID,
                        bundle=bundle,
                        signature=signature,
                        allow_insecure_loopback=True,
                    )
                )

        self.assertFalse(publish(valid)["idempotent"])
        with patch("dynamic_firm.evolution.hosted_transport._request_json") as request_json:
            with self.assertRaisesRegex(HostedTransportError, "32 KiB"):
                publish_artifact_registry(
                    endpoint="http://127.0.0.1:1",
                    token="publisher-token",
                    registry_id=REGISTRY_ID,
                    authorization_id=AUTHORIZATION_ID,
                    bundle=bundle,
                    signature=_ssh_signature(MAX_OPENSSH_SIGNATURE_BYTES) + b"x",
                    allow_insecure_loopback=True,
                )
            request_json.assert_not_called()
        for receipt in (
            {**valid, "signature_digest": "0" * 64},
            {key: value for key, value in valid.items() if key != "published_at"},
            {**valid, "idempotent": 0},
            {**valid, "unexpected": True},
        ):
            with self.subTest(receipt=receipt):
                with self.assertRaisesRegex(HostedTransportError, "unsupported shape"):
                    publish(receipt)

    def test_retirement_receipt_accepts_only_bound_fresh_or_idempotent_shape(self) -> None:
        fresh = {
            "schema": "noruct.evolution-artifact-registry-retirement.v1",
            "registry_id": REGISTRY_ID,
            "bundle_digest": "a" * 64,
            "status": "RETIRED",
            "retired_at": "2026-07-21T00:03:00+00:00",
            "reason_code": "operator_retired",
        }
        repeated = {
            "schema": "noruct.evolution-artifact-registry-retirement.v1",
            "registry_id": REGISTRY_ID,
            "bundle_digest": "a" * 64,
            "status": "RETIRED",
            "idempotent": True,
        }

        def retire(receipt: dict[str, object]) -> dict[str, object]:
            with patch(
                "dynamic_firm.evolution.hosted_transport._request_json",
                return_value=receipt,
            ):
                return dict(
                    retire_artifact_registry(
                        endpoint="http://127.0.0.1:1",
                        token="publisher-token",
                        registry_id=REGISTRY_ID,
                        reason_code="operator_retired",
                        allow_insecure_loopback=True,
                    )
                )

        self.assertEqual(retire(fresh)["reason_code"], "operator_retired")
        self.assertTrue(retire(repeated)["idempotent"])
        for receipt in (
            {**fresh, "bundle_digest": "not-a-digest"},
            {**fresh, "reason_code": "different_reason"},
            {**repeated, "idempotent": False},
            {**repeated, "unexpected": True},
        ):
            with self.subTest(receipt=receipt):
                with self.assertRaisesRegex(HostedTransportError, "unsupported shape"):
                    retire(receipt)


class SignatureBoundaryTests(unittest.TestCase):
    def test_detached_signature_verifiers_accept_32_kib_and_reject_one_byte_more(self) -> None:
        self.assertEqual(MAX_ARTIFACT_SIGNATURE_BYTES, 32 * 1024)
        self.assertEqual(MAX_OPENSSH_SIGNATURE_BYTES, 32 * 1024)
        signature = _ssh_signature(MAX_OPENSSH_SIGNATURE_BYTES)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = root / "ssh-keygen"
            command.write_text("fixture", encoding="utf-8")
            allowed_signers = root / "allowed-signers"
            allowed_signers.write_text("fixture signer", encoding="utf-8")
            signature_path = root / "release.sig"
            signature_path.write_bytes(signature)
            completed = subprocess.CompletedProcess(args=(), returncode=0, stdout=b"", stderr=b"")
            with patch("dynamic_firm.evolution.signing.subprocess.run", return_value=completed):
                verify_openssh_signature(
                    b'{"payload":"fixture"}',
                    signature_path=signature_path,
                    allowed_signers_path=allowed_signers,
                    principal="fixture_signer",
                    command=command,
                )
                verify_openssh_signature_bytes(
                    b'{"payload":"fixture"}',
                    signature=signature,
                    allowed_signers_path=allowed_signers,
                    principal="fixture_signer",
                    command=command,
                )

            signature_path.write_bytes(signature + b"x")
            with self.assertRaisesRegex(ValueError, "32 KiB"):
                verify_openssh_signature(
                    b'{"payload":"fixture"}',
                    signature_path=signature_path,
                    allowed_signers_path=allowed_signers,
                    principal="fixture_signer",
                    command=command,
                )
            with self.assertRaisesRegex(ValueError, "32 KiB"):
                verify_openssh_signature_bytes(
                    b'{"payload":"fixture"}',
                    signature=signature + b"x",
                    allowed_signers_path=allowed_signers,
                    principal="fixture_signer",
                    command=command,
                )

    def test_public_signature_fetch_has_the_same_32_kib_boundary(self) -> None:
        exact = _ssh_signature(MAX_ARTIFACT_SIGNATURE_BYTES)
        oversized = exact + b"x"

        class SignatureHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                value = exact if self.path == "/exact.sig" else oversized
                self.send_response(200)
                self.send_header("content-type", "application/ssh-signature")
                self.send_header("content-length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SignatureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            self.assertEqual(
                len(
                    fetch_artifact_registry_signature(
                        f"{origin}/exact.sig", allow_insecure_loopback=True
                    )
                ),
                MAX_ARTIFACT_SIGNATURE_BYTES,
            )
            with self.assertRaisesRegex(ValueError, "32 KiB"):
                fetch_artifact_registry_signature(
                    f"{origin}/oversized.sig", allow_insecure_loopback=True
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
