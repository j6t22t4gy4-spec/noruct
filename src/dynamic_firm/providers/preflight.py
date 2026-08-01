"""Explicit, bounded provider/authentication preflight.

This is deliberately not a model invocation.  It uses only documented model
listing metadata endpoints after an operator has confirmed the network action,
and returns no model names, credentials, response body, or provider-native
account data to the Company runtime.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from dynamic_firm import __version__
from dynamic_firm.providers.openai_compat import _is_loopback_host
from dynamic_firm.providers.profiles import provider_profile


_MAX_RESPONSE_BYTES = 128_000


@dataclass(frozen=True, slots=True)
class ProviderPreflightConfig:
    kind: str
    base_url: str
    model: str
    api_key_env: str | None
    no_auth: bool
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class ProviderPreflightResult:
    kind: str
    endpoint: str | None
    configured_model: str
    credential_name: str | None
    credential_present: bool
    network_attempted: bool
    outcome: str
    http_status: int | None
    model_count: int | None
    configured_model_seen: bool | None
    details: str
    model_invocation: bool = False
    credential_value_exposed: bool = False

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


def provider_preflight_status(config: ProviderPreflightConfig) -> ProviderPreflightResult:
    """Return deterministic local readiness without contacting a provider."""

    endpoint = _model_list_endpoint(config)
    credential_present = bool(config.no_auth or not config.api_key_env or os.environ.get(config.api_key_env))
    if config.kind == "openai_codex":
        return ProviderPreflightResult(
            kind=config.kind,
            endpoint=None,
            configured_model=config.model,
            credential_name=None,
            credential_present=False,
            network_attempted=False,
            outcome="EXTERNAL_CLI_LOGIN_CHECK_REQUIRED",
            http_status=None,
            model_count=None,
            configured_model_seen=None,
            details="Codex authentication is checked through its user-managed executable, not a provider HTTP probe.",
        )
    if config.kind == "external_exec":
        return ProviderPreflightResult(
            kind=config.kind,
            endpoint=None,
            configured_model=config.model,
            credential_name=None,
            credential_present=False,
            network_attempted=False,
            outcome="EXTERNAL_PROCESS_READINESS_CHECK_REQUIRED",
            http_status=None,
            model_count=None,
            configured_model_seen=None,
            details="This user-managed external process has no Noruct HTTP metadata endpoint; inspect its executable through `noruct provider status` and complete login in its own client.",
        )
    if endpoint is None:
        if _metadata_preflight_is_unsupported(config) and credential_present:
            return ProviderPreflightResult(
                kind=config.kind,
                endpoint=None,
                configured_model=config.model,
                credential_name=config.api_key_env,
                credential_present=True,
                network_attempted=False,
                outcome="METADATA_PREFLIGHT_UNSUPPORTED",
                http_status=None,
                model_count=None,
                configured_model_seen=None,
                details="This provider profile has no documented bounded model-list endpoint; no network request was made.",
            )
        return ProviderPreflightResult(
            kind=config.kind,
            endpoint=None,
            configured_model=config.model,
            credential_name=config.api_key_env,
            credential_present=credential_present,
            network_attempted=False,
            outcome="CONFIGURATION_INVALID",
            http_status=None,
            model_count=None,
            configured_model_seen=None,
            details="Provider base URL or model is not valid for a bounded metadata preflight.",
        )
    if not credential_present:
        return ProviderPreflightResult(
            kind=config.kind,
            endpoint=endpoint,
            configured_model=config.model,
            credential_name=config.api_key_env,
            credential_present=False,
            network_attempted=False,
            outcome="CREDENTIAL_MISSING",
            http_status=None,
            model_count=None,
            configured_model_seen=None,
            details="The named credential environment variable is not set.",
        )
    return ProviderPreflightResult(
        kind=config.kind,
        endpoint=endpoint,
        configured_model=config.model,
        credential_name=config.api_key_env,
        credential_present=True,
        network_attempted=False,
        outcome="READY_FOR_OPERATOR_CONFIRMED_METADATA_PROBE",
        http_status=None,
        model_count=None,
        configured_model_seen=None,
        details="No network request or model invocation has occurred.",
    )


def probe_provider_metadata(config: ProviderPreflightConfig) -> ProviderPreflightResult:
    """Perform one documented model-list probe with a bounded response.

    Callers must run :func:`provider_preflight_status` and obtain an explicit
    operator confirmation first.  Authentication values are read only long
    enough to construct the request header and never appear in the result.
    """

    status = provider_preflight_status(config)
    if status.outcome != "READY_FOR_OPERATOR_CONFIRMED_METADATA_PROBE":
        return status
    assert status.endpoint is not None
    headers = {"Accept": "application/json", "User-Agent": f"Noruct/{__version__} provider-preflight"}
    if not config.no_auth and config.api_key_env:
        credential = os.environ.get(config.api_key_env, "")
        if provider_profile(config.kind).transport == "anthropic-messages":
            headers["x-api-key"] = credential
            headers["anthropic-version"] = "2023-06-01"
        else:
            profile = provider_profile(config.kind)
            headers[profile.credential_header] = f"{profile.credential_prefix}{credential}"
    request = urllib.request.Request(status.endpoint, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                return _failure(status, "RESPONSE_TOO_LARGE", int(response.status), "Model metadata exceeded the response limit.")
            if not 200 <= int(response.status) < 300:
                return _failure(status, "ENDPOINT_REJECTED", int(response.status), "Provider metadata endpoint rejected the request.")
    except urllib.error.HTTPError as exc:
        outcome = "AUTHENTICATION_REJECTED" if exc.code in {401, 403} else "ENDPOINT_REJECTED"
        return _failure(status, outcome, exc.code, "Provider metadata request was rejected.")
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return _failure(status, "TRANSPORT_ERROR", None, "Provider metadata connection failed.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _failure(status, "RESPONSE_INVALID", 200, "Provider metadata response is not valid JSON.")
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, list) or len(data) > 1_000:
        return _failure(status, "RESPONSE_INVALID", 200, "Provider metadata response has no bounded model list.")
    seen: bool | None = None
    if config.model:
        seen = any(isinstance(item, dict) and item.get("id") == config.model for item in data)
    return ProviderPreflightResult(
        kind=status.kind,
        endpoint=status.endpoint,
        configured_model=status.configured_model,
        credential_name=status.credential_name,
        credential_present=True,
        network_attempted=True,
        outcome="METADATA_REACHABLE",
        http_status=200,
        model_count=len(data),
        configured_model_seen=seen,
        details="Model-list metadata was reachable; this is not a model capability or quota guarantee.",
    )


def _failure(
    status: ProviderPreflightResult, outcome: str, http_status: int | None, details: str
) -> ProviderPreflightResult:
    return ProviderPreflightResult(
        kind=status.kind,
        endpoint=status.endpoint,
        configured_model=status.configured_model,
        credential_name=status.credential_name,
        credential_present=status.credential_present,
        network_attempted=True,
        outcome=outcome,
        http_status=http_status,
        model_count=None,
        configured_model_seen=None,
        details=details,
    )


def _model_list_endpoint(config: ProviderPreflightConfig) -> str | None:
    if config.kind in {"openai_codex", "external_exec"} or not config.base_url.strip() or not config.model.strip():
        return None
    parsed = urlsplit(config.base_url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not _is_loopback_host(parsed.hostname))
    ):
        return None
    if config.timeout_seconds <= 0 or config.timeout_seconds > 30:
        return None
    profile = provider_profile(config.kind)
    if profile.model_list_url is not None:
        candidate = urlsplit(profile.model_list_url)
        if (
            candidate.scheme != "https"
            or not candidate.hostname
            or candidate.username
            or candidate.password
            or candidate.query
            or candidate.fragment
        ):
            return None
        return profile.model_list_url
    if profile.model_list_path is None:
        return None
    return config.base_url.rstrip("/") + profile.model_list_path


def _metadata_preflight_is_unsupported(config: ProviderPreflightConfig) -> bool:
    if config.kind == "openai_codex":
        return False
    profile = provider_profile(config.kind)
    return profile.model_list_path is None and profile.model_list_url is None
