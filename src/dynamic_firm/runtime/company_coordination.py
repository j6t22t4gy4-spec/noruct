"""Opt-in, privacy-bounded remote Company coordination transport.

The Company coordination service is intentionally distinct from Shared
Evolution.  It never transports a Work Order body, a prompt, tool arguments,
tool output, Employee memory, or dependency result content.  It only gives
multiple user-authorized devices a common authority for opaque resource leases
and one-shot receipt-bound continuation claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^device-[a-z0-9_-]{2,80}$")
_JOB_ID = re.compile(r"^job-[a-z0-9-]{8,120}$")
_LEASE_ID = re.compile(r"^coord-lease-[0-9a-f-]{16,120}$")
_CONTINUATION_ID = re.compile(r"^continuation-[0-9a-f-]{16,120}$")
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SCHEMA_LEASE_CLAIM = "noruct.company-coordination-resource-lease-claim.v1"
_SCHEMA_LEASE_RELEASE = "noruct.company-coordination-resource-lease-release.v1"
_SCHEMA_CONTINUATION = "noruct.company-coordination-partial-continuation.v1"
_SCHEMA_CONTINUATION_HANDOFF = "noruct.company-coordination-partial-continuation-handoff.v1"
_SCHEMA_GRAPH_PROPOSAL = "noruct.company-coordination-graph-proposal-continuation.v1"
_MAX_RESPONSE_BYTES = 16 * 1024
_SAFE_REMOTE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_USER_AGENT = "Noruct/0.0.80 (company-coordination)"
_AUTHORITY_DIGEST_SCHEMA = "noruct.company-coordination-authority.v1"


class CompanyCoordinationError(ValueError):
    """Safe coordination failure; it never contains a bearer token."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


def _origin(endpoint: str, *, allow_insecure_loopback: bool = False) -> str:
    if not isinstance(allow_insecure_loopback, bool):
        raise CompanyCoordinationError(
            "Company coordination loopback posture must be boolean"
        )
    parsed = urlparse(endpoint.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise CompanyCoordinationError("Company coordination endpoint must be an absolute URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise CompanyCoordinationError("Company coordination endpoint must be an origin URL")
    hostname = (parsed.hostname or "").lower()
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_insecure_loopback and loopback):
        raise CompanyCoordinationError("Company coordination requires HTTPS outside explicit loopback development")
    try:
        port = parsed.port
    except ValueError as error:
        raise CompanyCoordinationError("Company coordination endpoint port is invalid") from error
    default_port = 443 if parsed.scheme == "https" else 80
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _token_from_environment(name: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
        raise CompanyCoordinationError("Company coordination token environment variable is invalid")
    value = os.environ.get(name, "").strip()
    if not value or len(value) > 512 or "\r" in value or "\n" in value:
        raise CompanyCoordinationError("Company coordination token is unavailable")
    return value


def company_coordination_authority_digest(
    config: RemoteCompanyCoordinationConfig | None,
) -> str:
    """Hash the secret-free remote authority, excluding device and token.

    A device id is an exact lease-owner dimension and is persisted separately.
    The bearer value is never read here; only its environment-variable name is
    part of the frozen authority posture.
    """

    if config is None:
        payload: Mapping[str, object] = {
            "schema": _AUTHORITY_DIGEST_SCHEMA,
            "enabled": False,
        }
    else:
        origin = _origin(
            config.endpoint,
            allow_insecure_loopback=config.allow_insecure_loopback,
        )
        if not _SHA256.fullmatch(config.company_scope_digest):
            raise CompanyCoordinationError(
                "Company coordination scope must be an opaque SHA-256 digest"
            )
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", config.token_env):
            raise CompanyCoordinationError(
                "Company coordination token environment variable is invalid"
            )
        payload = {
            "schema": _AUTHORITY_DIGEST_SCHEMA,
            "enabled": True,
            "origin": origin,
            "company_scope_digest": config.company_scope_digest,
            "token_env": config.token_env,
            "allow_insecure_loopback": config.allow_insecure_loopback,
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_rejection_detail(error: HTTPError) -> str:
    """Return bounded operator diagnostics without reflecting remote content.

    A proxy, WAF, or access policy can reject a coordination request before the
    Worker has a chance to return Noruct's structured error body.  Preserve the
    HTTP status in that case so an operator can distinguish deployment routing
    from device enrollment, but never surface arbitrary response text.
    """

    code: object | None = None
    try:
        body = error.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) <= _MAX_RESPONSE_BYTES:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, Mapping):
                candidate = parsed.get("code")
                if isinstance(candidate, str) and _SAFE_REMOTE_CODE.fullmatch(candidate):
                    code = candidate
    except Exception:
        pass
    if isinstance(code, str):
        return f"HTTP {error.code}: {code}"
    return f"HTTP {error.code} without a structured service error"


def _request_json(*, endpoint: str, token: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 12 * 1024:
        raise CompanyCoordinationError("Company coordination request exceeds the bounded contract")
    request = Request(
        endpoint,
        data=encoded,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=10) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise CompanyCoordinationError(
            f"Company coordination request was rejected: {_safe_rejection_detail(error)}"
        ) from None
    except (OSError, URLError) as error:
        raise CompanyCoordinationError(f"Company coordination transport failed: {type(error).__name__}") from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CompanyCoordinationError("Company coordination response exceeds the bounded contract")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompanyCoordinationError("Company coordination response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise CompanyCoordinationError("Company coordination response has an invalid shape")
    return value


def _authenticated_get_json(*, endpoint: str, token: str) -> Mapping[str, Any]:
    request = Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="GET",
    )
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=10) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise CompanyCoordinationError(
            "Company coordination identity preflight was rejected: "
            + _safe_rejection_detail(error)
        ) from None
    except (OSError, URLError) as error:
        raise CompanyCoordinationError(
            f"Company coordination identity preflight failed: {type(error).__name__}"
        ) from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CompanyCoordinationError("Company coordination identity response exceeds the bounded contract")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompanyCoordinationError("Company coordination identity response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise CompanyCoordinationError("Company coordination identity response has an invalid shape")
    return value


@dataclass(frozen=True, slots=True)
class RemoteCompanyCoordinationConfig:
    endpoint: str
    company_scope_digest: str
    device_id: str
    token_env: str = "NORUCT_COMPANY_COORDINATION_TOKEN"
    allow_insecure_loopback: bool = False

    def validate(self) -> None:
        _origin(self.endpoint, allow_insecure_loopback=self.allow_insecure_loopback)
        if not _SHA256.fullmatch(self.company_scope_digest):
            raise CompanyCoordinationError("Company coordination scope must be an opaque SHA-256 digest")
        if not _DEVICE_ID.fullmatch(self.device_id):
            raise CompanyCoordinationError("Company coordination device identity is invalid")
        _token_from_environment(self.token_env)


@dataclass(frozen=True, slots=True)
class RemoteResourceLease:
    """Opaque owner receipt whose deadline permits refresh, never takeover.

    The coordination authority cannot observe handler entry or an external
    outcome.  ``expires_at`` is therefore an advisory renewal deadline for the
    exact owner, not evidence that another device may claim the resource.
    """

    resource_digest: str
    lease_id: str
    expires_at: str
    idempotent: bool


class RemoteCompanyCoordinationClient:
    """HTTPS client used only at effect and same-Job continuation boundaries."""

    def __init__(self, config: RemoteCompanyCoordinationConfig) -> None:
        config.validate()
        self.config = config
        self.origin = _origin(config.endpoint, allow_insecure_loopback=config.allow_insecure_loopback)
        self.authority_digest = company_coordination_authority_digest(config)

    def preflight_identity(self) -> Mapping[str, object]:
        """Prove the opaque device enrollment without creating any remote state."""

        value = _authenticated_get_json(
            endpoint=f"{self.origin}/v1/company-coordination/identity?"
            + urlencode(
                {
                    "company_scope_digest": self.config.company_scope_digest,
                    "device_id": self.config.device_id,
                }
            ),
            token=_token_from_environment(self.config.token_env),
        )
        if (
            value.get("schema") != "noruct.company-coordination-identity.v1"
            or value.get("status") != "AUTHORIZED"
            or value.get("company_scope_digest") != self.config.company_scope_digest
            or value.get("device_id") != self.config.device_id
            or value.get("server_mutated") is not False
            or value.get("remote_execution_enabled") is not False
        ):
            raise CompanyCoordinationError("Company coordination identity receipt is invalid")
        return dict(value)

    def claim_resource_lease(
        self,
        *,
        job_id: str,
        resource_digest: str,
        lease_id: str,
        ttl_seconds: int = 300,
    ) -> RemoteResourceLease | None:
        if not _JOB_ID.fullmatch(job_id) or not _SHA256.fullmatch(resource_digest) or not _LEASE_ID.fullmatch(lease_id):
            raise CompanyCoordinationError("Company coordination resource identity is invalid")
        if not isinstance(ttl_seconds, int) or not 30 <= ttl_seconds <= 900:
            raise CompanyCoordinationError("Company coordination lease TTL must be between 30 and 900 seconds")
        value = _request_json(
            endpoint=f"{self.origin}/v1/company-coordination/resource-leases/claim",
            token=_token_from_environment(self.config.token_env),
            payload={
                "schema": _SCHEMA_LEASE_CLAIM,
                "company_scope_digest": self.config.company_scope_digest,
                "job_id": job_id,
                "device_id": self.config.device_id,
                "resource_digest": resource_digest,
                "lease_id": lease_id,
                "ttl_seconds": ttl_seconds,
            },
        )
        if value.get("status") == "BUSY":
            return None
        if (
            value.get("schema") != _SCHEMA_LEASE_CLAIM
            or value.get("status") != "CLAIMED"
            or value.get("resource_digest") != resource_digest
            or value.get("lease_id") != lease_id
            or not isinstance(value.get("expires_at"), str)
            or not isinstance(value.get("idempotent"), bool)
        ):
            raise CompanyCoordinationError("Company coordination resource receipt is invalid")
        return RemoteResourceLease(resource_digest, lease_id, str(value["expires_at"]), bool(value["idempotent"]))

    def release_resource_lease(self, *, job_id: str, resource_digest: str, lease_id: str) -> bool:
        if not _JOB_ID.fullmatch(job_id) or not _SHA256.fullmatch(resource_digest) or not _LEASE_ID.fullmatch(lease_id):
            raise CompanyCoordinationError("Company coordination resource identity is invalid")
        value = _request_json(
            endpoint=f"{self.origin}/v1/company-coordination/resource-leases/release",
            token=_token_from_environment(self.config.token_env),
            payload={
                "schema": _SCHEMA_LEASE_RELEASE,
                "company_scope_digest": self.config.company_scope_digest,
                "job_id": job_id,
                "device_id": self.config.device_id,
                "resource_digest": resource_digest,
                "lease_id": lease_id,
            },
        )
        if value.get("schema") != _SCHEMA_LEASE_RELEASE or value.get("status") not in {"RELEASED", "MISSING"}:
            raise CompanyCoordinationError("Company coordination release receipt is invalid")
        return value.get("status") == "RELEASED"

    def authorize_partial_continuation(
        self,
        *,
        job_id: str,
        continuation_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_attempt_ids: tuple[str, ...],
        completed_results_digest: str,
    ) -> None:
        self._continuation_request(
            "admit", job_id=job_id, continuation_id=continuation_id,
            request_snapshot_hash=request_snapshot_hash, graph_digest=graph_digest,
            completed_attempt_ids=completed_attempt_ids,
            completed_results_digest=completed_results_digest,
        )

    def claim_partial_continuation(
        self,
        *,
        job_id: str,
        continuation_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_attempt_ids: tuple[str, ...],
        completed_results_digest: str,
    ) -> bool:
        value = self._continuation_request(
            "claim", job_id=job_id, continuation_id=continuation_id,
            request_snapshot_hash=request_snapshot_hash, graph_digest=graph_digest,
            completed_attempt_ids=completed_attempt_ids,
            completed_results_digest=completed_results_digest,
        )
        if value.get("status") == "CLAIMED":
            return True
        if value.get("status") == "ALREADY_CLAIMED":
            return False
        raise CompanyCoordinationError("Company coordination continuation claim is invalid")

    def handoff_partial_continuation(
        self,
        *,
        job_id: str,
        continuation_id: str,
        request_snapshot_hash: str,
        graph_digest: str,
        completed_attempt_ids: tuple[str, ...],
        completed_results_digest: str,
        target_device_id: str,
    ) -> None:
        """Transfer only an unclaimed read-only continuation to one device.

        The recipient must already have the exact local Work Order and result
        receipts.  This is authority handoff, never remote job/result sync or
        a transfer of an in-flight/effectful execution.
        """

        if not _DEVICE_ID.fullmatch(target_device_id) or target_device_id == self.config.device_id:
            raise CompanyCoordinationError("Company coordination handoff target is invalid")
        value = self._continuation_request(
            "handoff",
            job_id=job_id,
            continuation_id=continuation_id,
            request_snapshot_hash=request_snapshot_hash,
            graph_digest=graph_digest,
            completed_attempt_ids=completed_attempt_ids,
            completed_results_digest=completed_results_digest,
            target_device_id=target_device_id,
        )
        if (
            value.get("schema") != _SCHEMA_CONTINUATION_HANDOFF
            or value.get("status") != "TRANSFERRED"
            or value.get("target_device_id") != target_device_id
        ):
            raise CompanyCoordinationError("Company coordination handoff receipt is invalid")

    def authorize_graph_proposal_continuation(
        self,
        *,
        job_id: str,
        continuation_id: str,
        proposal_id: str,
        request_snapshot_hash: str,
        before_graph_digest: str,
        after_graph_digest: str,
        mutation_lease_digest: str,
        completed_results_digest: str,
    ) -> None:
        self._graph_proposal_request(
            "admit", job_id=job_id, continuation_id=continuation_id, proposal_id=proposal_id,
            request_snapshot_hash=request_snapshot_hash, before_graph_digest=before_graph_digest,
            after_graph_digest=after_graph_digest, mutation_lease_digest=mutation_lease_digest,
            completed_results_digest=completed_results_digest,
        )

    def claim_graph_proposal_continuation(self, **payload: Any) -> bool:
        value = self._graph_proposal_request("claim", **payload)
        if value.get("status") == "CLAIMED":
            return True
        if value.get("status") == "ALREADY_CLAIMED":
            return False
        raise CompanyCoordinationError("Company coordination Graph proposal claim is invalid")

    def resolve_graph_proposal_continuation(self, **payload: Any) -> None:
        value = self._graph_proposal_request("resolve", **payload)
        if value.get("status") != payload.get("decision"):
            raise CompanyCoordinationError("Company coordination Graph proposal decision is invalid")

    def _graph_proposal_request(self, operation: str, **payload: Any) -> Mapping[str, Any]:
        if operation not in {"admit", "resolve", "claim"}:
            raise ValueError("Unsupported Graph proposal coordination operation")
        values = (
            payload.get("request_snapshot_hash"), payload.get("before_graph_digest"),
            payload.get("after_graph_digest"), payload.get("mutation_lease_digest"),
            payload.get("completed_results_digest"),
        )
        if (
            not _JOB_ID.fullmatch(str(payload.get("job_id", "")))
            or not _CONTINUATION_ID.fullmatch(str(payload.get("continuation_id", "")))
            or not re.fullmatch(r"graph-proposal-[0-9a-f]{24}", str(payload.get("proposal_id", "")))
            or not all(_SHA256.fullmatch(str(item)) for item in values)
        ):
            raise CompanyCoordinationError("Company coordination Graph proposal identity is invalid")
        if operation in {"resolve", "claim"} and payload.get("decision") not in {
            "APPROVED",
            "REJECTED",
        }:
            raise CompanyCoordinationError("Company coordination Graph proposal decision is invalid")
        value = _request_json(
            endpoint=f"{self.origin}/v1/company-coordination/graph-proposals/{operation}",
            token=_token_from_environment(self.config.token_env),
            payload={"schema": _SCHEMA_GRAPH_PROPOSAL, "company_scope_digest": self.config.company_scope_digest, "device_id": self.config.device_id, **payload},
        )
        if value.get("schema") != _SCHEMA_GRAPH_PROPOSAL or value.get("job_id") != payload["job_id"] or value.get("proposal_id") != payload["proposal_id"]:
            raise CompanyCoordinationError("Company coordination Graph proposal receipt is invalid")
        return value

    def _continuation_request(self, operation: str, **payload: Any) -> Mapping[str, Any]:
        if operation not in {"admit", "claim", "handoff"}:
            raise ValueError("Unsupported company coordination continuation operation")
        job_id = payload["job_id"]
        continuation_id = payload["continuation_id"]
        hashes = (payload["request_snapshot_hash"], payload["graph_digest"], payload["completed_results_digest"])
        attempt_ids = tuple(payload["completed_attempt_ids"])
        if (
            not _JOB_ID.fullmatch(job_id) or not _CONTINUATION_ID.fullmatch(continuation_id)
            or not all(_SHA256.fullmatch(item) for item in hashes)
            or not attempt_ids or len(set(attempt_ids)) != len(attempt_ids)
            or any(not _ATTEMPT_ID.fullmatch(item) for item in attempt_ids)
        ):
            raise CompanyCoordinationError("Company coordination continuation identity is invalid")
        target_device_id = payload.get("target_device_id")
        if operation == "handoff" and (
            not isinstance(target_device_id, str)
            or not _DEVICE_ID.fullmatch(target_device_id)
            or target_device_id == self.config.device_id
        ):
            raise CompanyCoordinationError("Company coordination handoff target is invalid")
        endpoint_operation = "handoff" if operation == "handoff" else operation
        value = _request_json(
            endpoint=f"{self.origin}/v1/company-coordination/partial-continuations/{endpoint_operation}",
            token=_token_from_environment(self.config.token_env),
            payload={
                "schema": _SCHEMA_CONTINUATION_HANDOFF if operation == "handoff" else _SCHEMA_CONTINUATION,
                "company_scope_digest": self.config.company_scope_digest,
                "device_id": self.config.device_id,
                **payload,
            },
        )
        expected_schema = _SCHEMA_CONTINUATION_HANDOFF if operation == "handoff" else _SCHEMA_CONTINUATION
        if value.get("schema") != expected_schema or value.get("job_id") != job_id or value.get("continuation_id") != continuation_id:
            raise CompanyCoordinationError("Company coordination continuation receipt is invalid")
        return value
