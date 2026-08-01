"""Secret-free runtime bindings for same-Job continuation.

The original Job owns its provider and granted tool surface.  Restart may
reconstruct those private runtime objects, but it must not silently replace
their execution contract or remote coordination authority with whatever
happens to be configured now.  This module creates and checks content-free
SHA-256 bindings before any provider, tool, or remote claim can run.  It reads
no credential value and performs no network or tool operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Callable

from dynamic_firm.runtime.company_coordination import (
    RemoteCompanyCoordinationClient,
    RemoteCompanyCoordinationConfig,
    company_coordination_authority_digest,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    IdempotencyMode,
    ToolEffect,
    ToolRisk,
)
from dynamic_firm.runtime.tool_contracts import ToolRegistry
from dynamic_firm.kernel.models import CompanyRunRequest


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_BINDING_SCHEMA = "noruct.runtime-provider-binding.v1"
_TOOL_CONTRACT_SCHEMA = "noruct.runtime-tool-contract-binding.v1"
_MAX_CANONICAL_DEPTH = 32
_MAX_CONTAINER_ITEMS = 4_096


class ContinuationRuntimePreflightCode(StrEnum):
    """Stable, content-free reasons why continuation cannot use the runtime."""

    PROVIDER_CONFIG_INVALID = "PROVIDER_CONFIG_INVALID"
    PROVIDER_BINDING_MISSING = "PROVIDER_BINDING_MISSING"
    PROVIDER_BINDING_MISMATCH = "PROVIDER_BINDING_MISMATCH"
    TOOL_CONTRACT_INVALID = "TOOL_CONTRACT_INVALID"
    TOOL_CONTRACT_MISSING = "TOOL_CONTRACT_MISSING"
    TOOL_CONTRACT_MISMATCH = "TOOL_CONTRACT_MISMATCH"
    CAPABILITY_MANIFEST_MISMATCH = "CAPABILITY_MANIFEST_MISMATCH"
    COMPANY_COORDINATION_CONFIG_INVALID = "COMPANY_COORDINATION_CONFIG_INVALID"
    COMPANY_COORDINATION_BINDING_MISSING = "COMPANY_COORDINATION_BINDING_MISSING"
    COMPANY_COORDINATION_BINDING_MISMATCH = "COMPANY_COORDINATION_BINDING_MISMATCH"


class ContinuationRuntimePreflightError(RuntimeError):
    """Safe refusal raised before a continued Job can dispatch work."""

    def __init__(self, code: ContinuationRuntimePreflightCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ContinuationRuntimeBinding:
    """Content-free runtime contract frozen into a new Job request."""

    provider_binding_digest: str
    tool_contract_digest: str
    company_coordination_digest: str
    tool_count: int


@dataclass(frozen=True, slots=True)
class ContinuationRuntimePreflightResult:
    """Proof that reconstructed private runtime objects match the old Job."""

    provider_binding_digest: str
    tool_contract_digest: str
    company_coordination_digest: str
    tool_count: int


class _CanonicalizationError(ValueError):
    pass


def provider_binding_digest(provider_config: object) -> str:
    """Hash a deterministic provider projection without credential values.

    Provider configuration is expected to be a dataclass or another bounded
    JSON-like structure.  Dataclass and enum type identities are retained so
    two transports with coincidentally equal field values cannot collide.
    A field/key named ``workspace`` is deliberately omitted because workspace
    identity is frozen by the Company request's separate workspace contract.
    Raw-secret-looking fields fail closed; environment-variable names and
    broker references remain safe metadata and are retained.
    """

    try:
        if provider_config is None:
            raise _CanonicalizationError("provider config is absent")
        projection = {
            "schema": _PROVIDER_BINDING_SCHEMA,
            "config": _canonicalize(
                provider_config,
                reject_secret_fields=True,
                depth=0,
                active=set(),
            ),
        }
        return _sha256(projection)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.PROVIDER_CONFIG_INVALID
        ) from exc


def company_coordination_binding_digest(
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None,
) -> str:
    """Hash the remote authority domain without token values or device id.

    ``DISABLED`` is an explicit binding so enabling coordination after Job
    admission cannot silently add a remote lease authority.  Device identity
    is deliberately absent: the existing append-only handoff receipt is the
    only authority for an eligible cross-device read-only continuation.
    """

    try:
        if isinstance(company_coordination, RemoteCompanyCoordinationClient):
            config = company_coordination.config
        else:
            config = company_coordination
        if config is not None and not isinstance(
            config, RemoteCompanyCoordinationConfig
        ):
            raise _CanonicalizationError("unsupported coordination config")
        return company_coordination_authority_digest(config)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.COMPANY_COORDINATION_CONFIG_INVALID
        ) from exc


def granted_tool_contract_digest(
    registry: ToolRegistry,
    policy: ActionPolicy,
) -> tuple[str, int]:
    """Hash only exact definitions exposed by the frozen ActionPolicy.

    ``audit_projection`` is intentionally the first registry operation.  A
    dangling grant or effect mismatch is a configuration/programming error,
    not a digest mismatch, and is refused before a continuation can construct
    an Employee runtime.
    """

    try:
        audit = registry.audit_projection(policy)
    except Exception as exc:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID
        ) from exc
    if not audit.valid:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID
        )

    try:
        grant_names = tuple(grant.tool_name for grant in policy.tool_grants)
        if len(grant_names) != len(set(grant_names)):
            raise _CanonicalizationError("duplicate tool grant")
        tools: list[Mapping[str, Any]] = []
        for name in audit.exposed_tool_names:
            definition = registry.get(name)
            if definition is None or definition.name != name:
                raise _CanonicalizationError("audited definition disappeared")
            if (
                not isinstance(definition.name, str)
                or not definition.name.strip()
                or not isinstance(definition.description, str)
                or not isinstance(definition.effect, ToolEffect)
                or not isinstance(definition.risk, ToolRisk)
                or not isinstance(definition.idempotency_mode, IdempotencyMode)
                or not isinstance(definition.timeout_ms, int)
                or isinstance(definition.timeout_ms, bool)
                or definition.timeout_ms <= 0
                or not isinstance(definition.output_limit_bytes, int)
                or isinstance(definition.output_limit_bytes, bool)
                or definition.output_limit_bytes <= 0
                or not isinstance(definition.requires_approval, bool)
                or not isinstance(definition.allow_session_approval, bool)
                or not isinstance(definition.parallel_safe, bool)
                or not isinstance(definition.input_schema, Mapping)
            ):
                raise _CanonicalizationError("invalid tool definition")
            tools.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": _canonicalize(
                        definition.input_schema,
                        reject_secret_fields=False,
                        depth=0,
                        active=set(),
                    ),
                    "effect": definition.effect.value,
                    "risk": definition.risk.value,
                    "idempotency_mode": definition.idempotency_mode.value,
                    "timeout_ms": definition.timeout_ms,
                    "output_limit_bytes": definition.output_limit_bytes,
                    "requires_approval": definition.requires_approval,
                    "has_approval_preview": definition.approval_preview is not None,
                    "allow_session_approval": definition.allow_session_approval,
                    "parallel_safe": definition.parallel_safe,
                }
            )
        digest = _sha256(
            {
                "schema": _TOOL_CONTRACT_SCHEMA,
                "tools": tools,
            }
        )
    except ContinuationRuntimePreflightError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID
        ) from exc
    return digest, len(tools)


def bind_continuation_runtime(
    *,
    provider_config: object,
    registry: ToolRegistry,
    policy: ActionPolicy,
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None = None,
) -> ContinuationRuntimeBinding:
    """Create exact content-free bindings for a newly admitted Job."""

    provider_digest = provider_binding_digest(provider_config)
    tool_digest, tool_count = granted_tool_contract_digest(registry, policy)
    coordination_digest = company_coordination_binding_digest(company_coordination)
    return ContinuationRuntimeBinding(
        provider_binding_digest=provider_digest,
        tool_contract_digest=tool_digest,
        company_coordination_digest=coordination_digest,
        tool_count=tool_count,
    )


def bind_company_run_request_runtime(
    request: CompanyRunRequest,
    firm_admission_digest: str,
    provider_config: object,
    registry: ToolRegistry,
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None,
) -> CompanyRunRequest:
    """Freeze production runtime bindings with the final admission receipt."""

    binding = bind_continuation_runtime(
        provider_config=provider_config,
        registry=registry,
        policy=request.action_policy,
        company_coordination=company_coordination,
    )
    return replace(
        request,
        firm_admission_digest=firm_admission_digest,
        runtime_provider_binding_digest=binding.provider_binding_digest,
        runtime_tool_contract_digest=binding.tool_contract_digest,
        runtime_company_coordination_digest=binding.company_coordination_digest,
    )


def validate_continuation_runtime(
    *,
    expected_provider_digest: str,
    expected_tool_digest: str,
    expected_company_coordination_digest: str,
    provider_config: object,
    registry: ToolRegistry,
    policy: ActionPolicy,
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None,
) -> ContinuationRuntimePreflightResult:
    """Fail closed unless the restarted runtime matches all frozen hashes."""

    require_continuation_runtime_bindings(
        expected_provider_digest=expected_provider_digest,
        expected_tool_digest=expected_tool_digest,
        expected_company_coordination_digest=expected_company_coordination_digest,
    )
    coordination_digest = company_coordination_binding_digest(company_coordination)
    if not _constant_time_equal(
        expected_company_coordination_digest,
        coordination_digest,
    ):
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISMATCH
        )
    provider_digest = provider_binding_digest(provider_config)
    if not _constant_time_equal(expected_provider_digest, provider_digest):
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.PROVIDER_BINDING_MISMATCH
        )
    tool_digest, tool_count = granted_tool_contract_digest(registry, policy)
    if not _constant_time_equal(expected_tool_digest, tool_digest):
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_MISMATCH
        )
    return ContinuationRuntimePreflightResult(
        provider_binding_digest=provider_digest,
        tool_contract_digest=tool_digest,
        company_coordination_digest=coordination_digest,
        tool_count=tool_count,
    )


def require_continuation_runtime_bindings(
    *,
    expected_provider_digest: str,
    expected_tool_digest: str,
    expected_company_coordination_digest: str,
) -> None:
    """Reject absent or malformed historical bindings before assembly work."""

    if not expected_provider_digest:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.PROVIDER_BINDING_MISSING
        )
    if not expected_tool_digest:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_MISSING
        )
    if not expected_company_coordination_digest:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISSING
        )
    if _SHA256.fullmatch(expected_provider_digest) is None:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.PROVIDER_BINDING_MISMATCH
        )
    if _SHA256.fullmatch(expected_tool_digest) is None:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_MISMATCH
        )
    if _SHA256.fullmatch(expected_company_coordination_digest) is None:
        raise ContinuationRuntimePreflightError(
            ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISMATCH
        )


def require_company_run_request_runtime_bindings(
    request: CompanyRunRequest,
) -> None:
    """Apply the assembly-free gate to one retained Company request."""

    require_continuation_runtime_bindings(
        expected_provider_digest=request.runtime_provider_binding_digest,
        expected_tool_digest=request.runtime_tool_contract_digest,
        expected_company_coordination_digest=(
            request.runtime_company_coordination_digest
        ),
    )


def build_validated_continuation_provider(
    *,
    expected_provider_digest: str,
    expected_tool_digest: str,
    expected_company_coordination_digest: str,
    provider_config: object,
    registry: ToolRegistry,
    policy: ActionPolicy,
    provider_factory: Callable[[object], object],
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None,
) -> object:
    """Construct a provider only after every frozen runtime binding passes.

    Provider constructors may inspect an executable, environment metadata, or
    other user-managed local state even when they make no model request.  Keep
    that construction behind the same fail-closed boundary as actual provider
    calls so historical requests without bindings and drifted tool surfaces
    cannot trigger it.
    """

    validate_continuation_runtime(
        expected_provider_digest=expected_provider_digest,
        expected_tool_digest=expected_tool_digest,
        expected_company_coordination_digest=expected_company_coordination_digest,
        provider_config=provider_config,
        registry=registry,
        policy=policy,
        company_coordination=company_coordination,
    )
    return provider_factory(provider_config)


def build_validated_company_run_request_provider(
    *,
    request: CompanyRunRequest,
    provider_config: object,
    registry: ToolRegistry,
    provider_factory: Callable[[object], object],
    company_coordination: RemoteCompanyCoordinationClient
    | RemoteCompanyCoordinationConfig
    | None,
) -> object:
    """Validate every retained runtime digest before provider construction."""

    return build_validated_continuation_provider(
        expected_provider_digest=request.runtime_provider_binding_digest,
        expected_tool_digest=request.runtime_tool_contract_digest,
        expected_company_coordination_digest=(
            request.runtime_company_coordination_digest
        ),
        provider_config=provider_config,
        registry=registry,
        policy=request.action_policy,
        provider_factory=provider_factory,
        company_coordination=company_coordination,
    )


def _constant_time_equal(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected.encode("ascii"), actual.encode("ascii"))


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _qualified_type(value: object) -> str:
    target = value if isinstance(value, type) else type(value)
    return f"{target.__module__}.{target.__qualname__}"


def _canonicalize(
    value: object,
    *,
    reject_secret_fields: bool,
    depth: int,
    active: set[int],
) -> object:
    if depth > _MAX_CANONICAL_DEPTH:
        raise _CanonicalizationError("canonical projection is too deep")
    if isinstance(value, Enum):
        return {
            "$enum": _qualified_type(value),
            "value": _canonicalize(
                value.value,
                reject_secret_fields=reject_secret_fields,
                depth=depth + 1,
                active=active,
            ),
        }
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _CanonicalizationError("non-finite float")
        return value
    if isinstance(value, Path):
        return {"$path": value.as_posix()}
    if isinstance(value, type):
        return {"$type": _qualified_type(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _CanonicalizationError("binary provider material is forbidden")

    track_identity = is_dataclass(value) or isinstance(value, (Mapping, Sequence))
    identity = id(value)
    if track_identity:
        if identity in active:
            raise _CanonicalizationError("cyclic provider configuration")
        active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            value_fields = fields(value)
            if len(value_fields) > _MAX_CONTAINER_ITEMS:
                raise _CanonicalizationError("too many provider fields")
            projection: dict[str, object] = {}
            for item in value_fields:
                if item.name == "workspace":
                    continue
                if reject_secret_fields and _looks_like_secret_value_name(item.name):
                    raise _CanonicalizationError("raw secret field is forbidden")
                projection[item.name] = _canonicalize(
                    getattr(value, item.name),
                    reject_secret_fields=reject_secret_fields,
                    depth=depth + 1,
                    active=active,
                )
            return {"$dataclass": _qualified_type(value), "fields": projection}
        if isinstance(value, Mapping):
            if len(value) > _MAX_CONTAINER_ITEMS:
                raise _CanonicalizationError("mapping is too large")
            items: list[tuple[object, object]] = []
            for key, item_value in value.items():
                if key == "workspace":
                    continue
                if (
                    reject_secret_fields
                    and isinstance(key, str)
                    and _looks_like_secret_value_name(key)
                ):
                    raise _CanonicalizationError("raw secret key is forbidden")
                canonical_key = _canonicalize(
                    key,
                    reject_secret_fields=False,
                    depth=depth + 1,
                    active=active,
                )
                canonical_value = _canonicalize(
                    item_value,
                    reject_secret_fields=reject_secret_fields,
                    depth=depth + 1,
                    active=active,
                )
                items.append((canonical_key, canonical_value))
            items.sort(key=lambda item: _canonical_sort_key(item[0]))
            return {"$mapping": [[key, item] for key, item in items]}
        if isinstance(value, Sequence):
            if len(value) > _MAX_CONTAINER_ITEMS:
                raise _CanonicalizationError("sequence is too large")
            return {
                "$sequence": [
                    _canonicalize(
                        item,
                        reject_secret_fields=reject_secret_fields,
                        depth=depth + 1,
                        active=active,
                    )
                    for item in value
                ]
            }
    finally:
        if track_identity:
            active.remove(identity)
    raise _CanonicalizationError(f"unsupported provider value: {_qualified_type(value)}")


def _canonical_sort_key(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _looks_like_secret_value_name(name: str) -> bool:
    normalized = name.strip().lower().replace("-", "_")
    safe_metadata_suffixes = (
        "_env",
        "_env_var",
        "_environment_variable",
        "_name",
        "_ref",
        "_reference",
        "_header",
        "_prefix",
    )
    if normalized.endswith(safe_metadata_suffixes):
        return False
    if normalized in {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }:
        return True
    return normalized.endswith(
        ("_api_key", "_password", "_passwd", "_secret", "_access_token", "_refresh_token")
    )
