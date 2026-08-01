"""Explicit, credential-free-at-rest HTTPS transport for Evolution Capsules.

The local Evolution store remains authoritative for consent and keeps no server
credential.  This module performs one narrowly-scoped request only after the
caller has selected an HTTPS endpoint, supplied a token through its environment,
and confirmed the action.  It is deliberately not a background synchronizer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dynamic_firm.company.models import canonical_json

from .score_contract import canonical_evolution_json, evolution_content_digest
from .signing import MAX_OPENSSH_SIGNATURE_BYTES


INTAKE_REQUEST_SCHEMA = "noruct.evolution-network-intake-request.v1"
INTAKE_RECEIPT_SCHEMA = "noruct.evolution-network-intake-receipt.v1"
MAX_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PUBLICATION_BYTES = 1_089_536
_CONTRIBUTION_ID = re.compile(r"^contribution-[0-9a-f-]{16,80}$")
_CAPSULE_ID = re.compile(r"^capsule-[0-9a-f-]{16,80}$")
_AUTHORIZATION_ID = re.compile(r"^authorization-[0-9a-f-]{16,80}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HostedTransportError(ValueError):
    """A safe request/response contract failure with no secret disclosure."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects before urllib can forward a Bearer-bearing request."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _open_without_redirects(request: Request):
    return build_opener(_NoRedirectHandler()).open(request, timeout=10)


@dataclass(frozen=True, slots=True)
class HostedReceipt:
    contribution_id: str
    capsule_id: str
    capsule_digest: str
    status: str
    receipt_digest: str
    recorded_at: str
    expires_at: str | None
    endpoint_origin: str
    withdrawal_capability: str | None = None

    def to_dict(self) -> Mapping[str, str | None]:
        return {
            "contribution_id": self.contribution_id,
            "capsule_id": self.capsule_id,
            "capsule_digest": self.capsule_digest,
            "status": self.status,
            "receipt_digest": self.receipt_digest,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "endpoint_origin": self.endpoint_origin,
            "withdrawal_capability": self.withdrawal_capability,
        }


def token_from_environment(name: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
        raise HostedTransportError("Token environment variable name is invalid")
    token = os.environ.get(name, "").strip()
    if not token:
        raise HostedTransportError(f"Token environment variable is not set: {name}")
    if len(token) > 512 or "\r" in token or "\n" in token:
        raise HostedTransportError("Evolution Network token is invalid")
    return token


def submit_capsule(
    *,
    endpoint: str,
    token: str,
    capsule_id: str,
    capsule: Mapping[str, Any],
    consent: Mapping[str, Any],
    withdrawal_capability: str,
    allow_insecure_loopback: bool = False,
) -> HostedReceipt:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not _CAPSULE_ID.fullmatch(capsule_id):
        raise HostedTransportError("Local capsule identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", withdrawal_capability):
        raise HostedTransportError("Withdrawal capability must be 64 lower-case hex characters")
    payload = {
        "schema": INTAKE_REQUEST_SCHEMA,
        "capsule_id": capsule_id,
        "capsule": capsule,
        "consent": {
            "purpose": consent.get("purpose"),
            "allowed_reuse": consent.get("allowed_reuse"),
            "authority": consent.get("authority"),
            "retention_days": consent.get("retention_days"),
        },
    }
    response = _request_json(
        endpoint=f"{origin}/v1/contributions",
        method="POST",
        token=token,
        payload=payload,
        extra_headers={"X-Noruct-Withdrawal-Capability": withdrawal_capability},
    )
    receipt = _receipt(
        response,
        expected_capsule_id=capsule_id,
        expected_digest=evolution_content_digest(capsule),
        origin=origin,
        statuses={"ACCEPTED"},
    )
    if (
        receipt.withdrawal_capability is not None
        and receipt.withdrawal_capability != withdrawal_capability
    ):
        raise HostedTransportError("Evolution Network returned a different withdrawal capability")
    return receipt


def withdraw_capsule(
    *,
    endpoint: str,
    token: str,
    contribution_id: str,
    capsule_id: str,
    capsule_digest: str,
    allow_insecure_loopback: bool = False,
    withdrawal_capability: str | None = None,
) -> HostedReceipt:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not _CONTRIBUTION_ID.fullmatch(contribution_id) or not _CAPSULE_ID.fullmatch(capsule_id) or not _SHA256.fullmatch(capsule_digest):
        raise HostedTransportError("Hosted contribution receipt is invalid")
    response = _request_json(
        endpoint=f"{origin}/v1/contributions/{contribution_id}",
        method="DELETE",
        token=token,
        payload=None,
        extra_headers=({"X-Noruct-Withdrawal-Capability": withdrawal_capability} if withdrawal_capability else {}),
    )
    return _receipt(response, expected_capsule_id=capsule_id, expected_digest=capsule_digest, origin=origin, statuses={"WITHDRAWN"})


def list_operator_candidates(
    *, endpoint: str, token: str, cursor: str | None = None, limit: int = 25,
    allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not isinstance(limit, int) or not 1 <= limit <= 25:
        raise HostedTransportError("Evolution Network candidate page limit must be from 1 to 25")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 512):
        raise HostedTransportError("Evolution Network candidate cursor is invalid")
    query = f"?limit={limit}" + (f"&cursor={quote(cursor, safe='')}" if cursor else "")
    response = _request_json(endpoint=f"{origin}/v1/internal/candidates{query}", method="GET", token=token, payload=None)
    candidates = response.get("candidates")
    next_cursor = response.get("next_cursor")
    if (
        response.get("schema") != "noruct.evolution-candidate-list.v1"
        or not isinstance(candidates, list) or len(candidates) > limit
        or (next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 512))
    ):
        raise HostedTransportError("Evolution Network candidate list has an unsupported shape")
    return response


def record_candidate_evaluation(
    *, endpoint: str, token: str, candidate_id: str, evaluation: Mapping[str, Any],
    allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not re.fullmatch(r"candidate-[0-9a-f]{32}", candidate_id):
        raise HostedTransportError("Evolution Network candidate identity is invalid")
    response = _request_json(
        endpoint=f"{origin}/v1/internal/candidates/{candidate_id}/evaluations",
        method="POST", token=token, payload=evaluation,
    )
    if (
        set(response)
        != {"status", "candidate_id", "evaluation_digest", "evaluated_at", "idempotent"}
        or response.get("candidate_id") != candidate_id
        or response.get("status")
        not in {"OPERATOR_REVIEW_READY", "EVALUATION_REJECTED", "EVALUATION_RECORDED"}
        or response.get("evaluation_digest") != evolution_content_digest(evaluation)
        or not isinstance(response.get("evaluated_at"), str)
        or not response.get("evaluated_at")
        or not isinstance(response.get("idempotent"), bool)
    ):
        raise HostedTransportError("Evolution Network evaluation receipt has an unsupported shape")
    return response


def expire_pending_contributions(
    *, endpoint: str, token: str, allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    response = _request_json(
        endpoint=f"{origin}/v1/internal/pending-contributions/expire",
        method="POST", token=token, payload=None,
    )
    if (
        response.get("schema") != "noruct.evolution-pending-expiry.v1"
        or not isinstance(response.get("expired_count"), int)
        or not isinstance(response.get("more_may_remain"), bool)
    ):
        raise HostedTransportError("Evolution Network expiry receipt has an unsupported shape")
    return response


def finalize_pending_contribution(
    *, endpoint: str, token: str, contribution_id: str, allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not _CONTRIBUTION_ID.fullmatch(contribution_id):
        raise HostedTransportError("Hosted contribution identity is invalid")
    response = _request_json(
        endpoint=f"{origin}/v1/internal/pending-contributions/{contribution_id}/finalize",
        method="POST", token=token, payload=None,
    )
    if response.get("contribution_id") != contribution_id or response.get("status") != "FINALIZED_SIGNAL":
        raise HostedTransportError("Evolution Network finalization receipt has an unsupported shape")
    return response


def assemble_candidates(
    *, endpoint: str, token: str, allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    response = _request_json(
        endpoint=f"{origin}/v1/internal/candidates/assemble",
        method="POST", token=token, payload=None,
    )
    if (
        response.get("schema") != "noruct.evolution-candidate-assembly.v1"
        or not isinstance(response.get("finalized_proposal_groups"), int)
        or not isinstance(response.get("evaluation_ready"), list)
    ):
        raise HostedTransportError("Evolution Network candidate assembly receipt has an unsupported shape")
    return response


def authorize_artifact_registry_publication(
    *,
    endpoint: str,
    token: str,
    registry_id: str,
    bundle_digest: str,
    candidate_evidence_digests: tuple[str, ...],
    evaluation_evidence_digests: tuple[str, ...],
    artifact_manifest_digests: tuple[str, ...],
    reviewer_id: str,
    reason_code: str,
    allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    """Authorize one exact registry digest from already-accepted evidence.

    This call cannot publish a bundle.  The server independently verifies that
    every supplied digest belongs to an operator-review-ready Candidate or its
    accepted public/synthetic PASS evaluation.
    """

    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not _SAFE_ID.fullmatch(registry_id) or not _SAFE_ID.fullmatch(reviewer_id) or not _SAFE_ID.fullmatch(reason_code):
        raise HostedTransportError("Evolution Network publication authorization identity is invalid")
    if not _SHA256.fullmatch(bundle_digest):
        raise HostedTransportError("Evolution Network publication bundle digest is invalid")
    candidate_digests = _bounded_digest_list(candidate_evidence_digests, "Candidate")
    evaluation_digests = _bounded_digest_list(evaluation_evidence_digests, "Evaluation")
    artifact_digests = _bounded_digest_list(artifact_manifest_digests, "Artifact manifest")
    if not candidate_digests or not evaluation_digests or not artifact_digests:
        raise HostedTransportError("Evolution Network publication authorization requires Candidate, evaluation, and Artifact evidence")
    if set(candidate_digests) & set(evaluation_digests):
        raise HostedTransportError("Evolution Network publication evidence digests must be disjoint")
    request_payload = {
        "schema": "noruct.evolution-artifact-registry-publication-authorization.v1",
        "registry_id": registry_id,
        "bundle_digest": bundle_digest,
        "candidate_evidence_digests": candidate_digests,
        "evaluation_evidence_digests": evaluation_digests,
        "artifact_manifest_digests": artifact_digests,
        "reviewer_id": reviewer_id,
        "reason_code": reason_code,
    }
    response = _request_json(
        endpoint=f"{origin}/v1/internal/artifact-registries/{registry_id}/authorizations",
        method="POST",
        token=token,
        payload=request_payload,
    )
    authorization_id = response.get("authorization_id")
    expected_keys = {
        "schema",
        "authorization_id",
        "authorization_digest",
        "registry_id",
        "bundle_digest",
        "evidence_digest",
        "status",
        "authorized_at",
        "idempotent",
    }
    expected_authorization_digest = _canonical_sha256(request_payload)
    expected_evidence_digest = _canonical_sha256(
        {
            "candidate_evidence_digests": candidate_digests,
            "evaluation_evidence_digests": evaluation_digests,
            "artifact_manifest_digests": artifact_digests,
        }
    )
    if (
        set(response) != expected_keys
        or response.get("schema") != "noruct.evolution-artifact-registry-publication-authorization-receipt.v1"
        or not isinstance(authorization_id, str)
        or not _AUTHORIZATION_ID.fullmatch(authorization_id)
        or response.get("registry_id") != registry_id
        or response.get("bundle_digest") != bundle_digest
        or response.get("status") not in {"PENDING", "CONSUMED"}
        or response.get("authorization_digest") != expected_authorization_digest
        or response.get("evidence_digest") != expected_evidence_digest
        or not isinstance(response.get("authorized_at"), str)
        or not response.get("authorized_at")
        or not isinstance(response.get("idempotent"), bool)
        or (response.get("idempotent") is False and response.get("status") != "PENDING")
    ):
        raise HostedTransportError("Evolution Network publication authorization receipt has an unsupported shape")
    return response


def publish_artifact_registry(
    *, endpoint: str, token: str, registry_id: str, authorization_id: str,
    bundle: Mapping[str, Any], signature: bytes, allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not _SAFE_ID.fullmatch(registry_id):
        raise HostedTransportError("Evolution Network registry identity is invalid")
    if not _AUTHORIZATION_ID.fullmatch(authorization_id):
        raise HostedTransportError("Evolution Network publication authorization identity is invalid")
    bundle_digest = bundle.get("bundle_digest")
    if not isinstance(bundle_digest, str) or not _SHA256.fullmatch(bundle_digest):
        raise HostedTransportError("Evolution Network publication bundle digest is invalid")
    if not signature or len(signature) > MAX_OPENSSH_SIGNATURE_BYTES:
        raise HostedTransportError("Artifact registry signature must contain up to 32 KiB")
    try:
        signature_text = signature.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostedTransportError("Artifact registry signature must be UTF-8") from exc
    if (
        not signature_text.startswith("-----BEGIN SSH SIGNATURE-----")
        or not signature_text.rstrip().endswith("-----END SSH SIGNATURE-----")
    ):
        raise HostedTransportError("Artifact registry signature has an unsupported format")
    response = _request_json(
        endpoint=f"{origin}/v1/internal/artifact-registries/{registry_id}/publish",
        method="POST", token=token,
        payload={
            "schema": "noruct.evolution-artifact-registry-publication.v1",
            "registry_id": registry_id,
            "bundle": bundle,
            "signature": signature_text,
            "authorization_id": authorization_id,
        },
        maximum_request_bytes=MAX_PUBLICATION_BYTES,
    )
    expected_keys = {
        "schema",
        "authorization_id",
        "registry_id",
        "bundle_digest",
        "signature_digest",
        "published_at",
        "status",
        "idempotent",
    }
    if (
        set(response) != expected_keys
        or response.get("schema") != "noruct.evolution-artifact-registry-publication-receipt.v1"
        or response.get("authorization_id") != authorization_id
        or response.get("registry_id") != registry_id
        or response.get("bundle_digest") != bundle_digest
        or response.get("signature_digest") != hashlib.sha256(signature).hexdigest()
        or not isinstance(response.get("published_at"), str)
        or not response.get("published_at")
        or response.get("status") != "ACTIVE"
        or not isinstance(response.get("idempotent"), bool)
    ):
        raise HostedTransportError("Evolution Network publication receipt has an unsupported shape")
    return response


def retire_artifact_registry(
    *, endpoint: str, token: str, registry_id: str, reason_code: str,
    allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    origin = _endpoint_origin(endpoint, allow_insecure_loopback=allow_insecure_loopback)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", registry_id) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", reason_code):
        raise HostedTransportError("Evolution Network registry retirement identity is invalid")
    response = _request_json(
        endpoint=f"{origin}/v1/internal/artifact-registries/{registry_id}/retire",
        method="POST", token=token,
        payload={"schema": "noruct.evolution-artifact-registry-retirement.v1", "registry_id": registry_id, "reason_code": reason_code},
    )
    base_keys = {"schema", "registry_id", "bundle_digest", "status"}
    fresh_keys = base_keys | {"retired_at", "reason_code"}
    idempotent_keys = base_keys | {"idempotent"}
    keys = set(response)
    fresh_shape = keys == fresh_keys
    idempotent_shape = keys == idempotent_keys
    if (
        not (fresh_shape or idempotent_shape)
        or response.get("schema") != "noruct.evolution-artifact-registry-retirement.v1"
        or response.get("registry_id") != registry_id
        or not isinstance(response.get("bundle_digest"), str)
        or not _SHA256.fullmatch(str(response.get("bundle_digest")))
        or response.get("status") != "RETIRED"
        or (
            fresh_shape
            and (
                response.get("reason_code") != reason_code
                or not isinstance(response.get("retired_at"), str)
                or not response.get("retired_at")
            )
        )
        or (idempotent_shape and response.get("idempotent") is not True)
    ):
        raise HostedTransportError("Evolution Network retirement receipt has an unsupported shape")
    return response


def _bounded_digest_list(values: tuple[str, ...], label: str) -> list[str]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise HostedTransportError(f"{label} evidence digest list is invalid")
    if any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in values):
        raise HostedTransportError(f"{label} evidence digest list is invalid")
    if len(set(values)) != len(values):
        raise HostedTransportError(f"{label} evidence digest list contains duplicates")
    return sorted(values)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return evolution_content_digest(value)


def _endpoint_origin(endpoint: str, *, allow_insecure_loopback: bool) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_insecure_loopback
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        pass
    else:
        raise HostedTransportError("Evolution Network endpoint must use HTTPS; HTTP is test-only loopback")
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise HostedTransportError("Evolution Network endpoint must be an origin without credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise HostedTransportError("Evolution Network endpoint must not include a path")
    return f"{parsed.scheme}://{parsed.netloc}"


def endpoint_origin(endpoint: str, *, allow_insecure_loopback: bool = False) -> str:
    """Validate and normalize an Evolution Network origin without I/O."""

    return _endpoint_origin(
        endpoint,
        allow_insecure_loopback=allow_insecure_loopback,
    )


def probe_public_service(
    endpoint: str,
    *,
    allow_insecure_loopback: bool = False,
) -> Mapping[str, Any]:
    """Read only the public Worker health and registry index without a token.

    This deliberately proves deployment reachability, not contributor access,
    consent, artifact publication, or remote Employee execution.  It performs
    no local persistence and never sends an Authorization header.
    """

    origin = _endpoint_origin(
        endpoint, allow_insecure_loopback=allow_insecure_loopback
    )
    health = _public_request_json(f"{origin}/health")
    registry = _public_request_json(f"{origin}/v1/artifact-registries")
    if health != {"service": "noruct-evolution-network", "status": "ok"}:
        raise HostedTransportError("Evolution Network health response is invalid")
    if (
        registry.get("schema")
        != "noruct.public-evolution-artifact-registry-index.v1"
        or not isinstance(registry.get("registries"), list)
        or len(registry["registries"]) > 512
    ):
        raise HostedTransportError("Evolution Network public registry response is invalid")
    return {
        "schema": "noruct.evolution-network-public-probe.v1",
        "endpoint_origin": origin,
        "worker_health": "REACHABLE",
        "public_registry_count": len(registry["registries"]),
        "credential_sent": False,
        "consent_required": False,
        "local_state_written": False,
        "remote_execution_enabled": False,
    }


def _public_request_json(endpoint: str) -> Mapping[str, Any]:
    request = Request(
        endpoint,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "noruct-evolution-probe/1"},
    )
    try:
        with _open_without_redirects(request) as response:  # nosec B310: validated origin
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise HostedTransportError("Evolution Network public probe does not follow redirects") from None
        raise HostedTransportError(f"Evolution Network public probe rejected ({exc.code})") from None
    except URLError:
        raise HostedTransportError("Evolution Network public probe failed") from None
    except Exception:
        raise HostedTransportError("Evolution Network public probe failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HostedTransportError("Evolution Network public probe response exceeds the limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HostedTransportError("Evolution Network public probe response is not valid JSON") from None
    if not isinstance(value, Mapping):
        raise HostedTransportError("Evolution Network public probe response must be an object")
    return value


def _request_json(
    *, endpoint: str,
    method: str,
    token: str,
    payload: Mapping[str, Any] | None,
    extra_headers: Mapping[str, str] | None = None,
    maximum_request_bytes: int = MAX_REQUEST_BYTES,
) -> Mapping[str, Any]:
    if not token or len(token) > 512 or "\r" in token or "\n" in token:
        raise HostedTransportError("Evolution Network token is invalid")
    # The Worker and Python client share this exact Evolution score-aware
    # canonicalizer.  In particular, integer boundary scores (0/1) are sent as
    # 0.0/1.0 so the immutable receipt digest cannot diverge by runtime.
    body = None if payload is None else canonical_evolution_json(payload).encode("utf-8")
    if body is not None and len(body) > maximum_request_bytes:
        raise HostedTransportError("Evolution Network request exceeds its bounded payload size limit")
    request = Request(
        endpoint,
        method=method,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
            "User-Agent": "noruct-evolution-client/1",
            **(extra_headers or {}),
        },
    )
    try:
        with _open_without_redirects(request) as response:  # nosec B310: endpoint policy above
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HostedTransportError:
        raise
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise HostedTransportError("Evolution Network transport does not follow redirects") from None
        if exc.code in {400, 401, 403, 404, 409, 413}:
            raise HostedTransportError(f"Evolution Network request rejected ({exc.code})") from None
        raise HostedTransportError("Evolution Network server error") from None
    except URLError:
        raise HostedTransportError("Evolution Network request failed") from None
    except Exception:
        raise HostedTransportError("Evolution Network transport failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HostedTransportError("Evolution Network response exceeds the limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HostedTransportError("Evolution Network response is not valid JSON") from None
    if not isinstance(value, Mapping):
        raise HostedTransportError("Evolution Network response must be an object")
    return value


def _receipt(
    value: Mapping[str, Any], *, expected_capsule_id: str, expected_digest: str,
    origin: str, statuses: set[str],
) -> HostedReceipt:
    base_keys = {
        "schema", "event_type", "contribution_id", "capsule_id", "capsule_digest",
        "recorded_at", "receipt_digest", "idempotent",
    }
    if value.get("schema") != INTAKE_RECEIPT_SCHEMA or value.get("event_type") not in statuses:
        raise HostedTransportError("Evolution Network receipt has an unexpected status")
    event_type = str(value["event_type"])
    idempotent = value.get("idempotent")
    accepted_keys = base_keys | {"expires_at"}
    if event_type == "ACCEPTED" and idempotent is False:
        accepted_keys |= {"withdrawal_capability"}
    expected_keys = accepted_keys if event_type == "ACCEPTED" else base_keys
    if set(value) != expected_keys or not isinstance(idempotent, bool):
        raise HostedTransportError("Evolution Network receipt has an unsupported shape")
    contribution_id = value.get("contribution_id")
    capsule_id = value.get("capsule_id")
    capsule_digest = value.get("capsule_digest")
    receipt_digest = value.get("receipt_digest")
    recorded_at = value.get("recorded_at")
    expires_at = value.get("expires_at")
    withdrawal_capability = value.get("withdrawal_capability")
    if (
        not isinstance(contribution_id, str) or not _CONTRIBUTION_ID.fullmatch(contribution_id)
        or capsule_id != expected_capsule_id or capsule_digest != expected_digest
        or not isinstance(receipt_digest, str) or not _SHA256.fullmatch(receipt_digest)
        or not isinstance(recorded_at, str) or not recorded_at
        or (
            event_type == "ACCEPTED"
            and (not isinstance(expires_at, str) or not expires_at)
        )
        or (event_type != "ACCEPTED" and expires_at is not None)
        or (withdrawal_capability is not None and (not isinstance(withdrawal_capability, str) or not re.fullmatch(r"[0-9a-f]{64}", withdrawal_capability)))
    ):
        raise HostedTransportError("Evolution Network receipt identity is invalid")
    unsigned = {
        "schema": INTAKE_RECEIPT_SCHEMA,
        "event_type": value["event_type"],
        "contribution_id": contribution_id,
        "capsule_id": capsule_id,
        "capsule_digest": capsule_digest,
        "recorded_at": recorded_at,
    }
    # The server derives its digest with SHA-256 canonical JSON. Recheck it so
    # a TLS-terminating proxy cannot silently substitute the receipt fields.
    server_digest = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if server_digest != receipt_digest:
        raise HostedTransportError("Evolution Network receipt digest does not match")
    return HostedReceipt(
        contribution_id=contribution_id,
        capsule_id=capsule_id,
        capsule_digest=capsule_digest,
        status=str(value["event_type"]),
        receipt_digest=receipt_digest,
        recorded_at=recorded_at,
        expires_at=expires_at if isinstance(expires_at, str) else None,
        endpoint_origin=origin,
        withdrawal_capability=withdrawal_capability if isinstance(withdrawal_capability, str) else None,
    )
