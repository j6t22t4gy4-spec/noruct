"""Secret-free settings and TOML adapter for multi-device Company coordination.

The coordination bearer token is deliberately never read or stored here.  The
only credential-shaped field that may appear in the settings file or Settings
Center is the *environment variable name* from which the runtime client reads
the token at the moment it makes a bounded coordination request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.company_coordination import (
    CompanyCoordinationError,
    RemoteCompanyCoordinationClient,
    RemoteCompanyCoordinationConfig,
)


COMPANY_COORDINATION_SETTINGS_SCHEMA = "noruct.company-coordination-settings.v1"
COMPANY_COORDINATION_ENROLLMENT_PREVIEW_SCHEMA = (
    "noruct.company-coordination-enrollment-preview.v1"
)
_TABLE = "company_coordination"
_HEADER = re.compile(r"(?m)^\[([A-Za-z0-9_.-]+)\][ \t]*(?:\r?\n|$)")


@dataclass(frozen=True, slots=True)
class CompanyCoordinationSettings:
    """One non-secret, future-Job coordination configuration.

    ``enabled=False`` retains no network capability and is useful for an
    operator who wants an explicit local off posture in a shared config file.
    ``enabled=True`` validates the token's availability before the file is
    committed, avoiding a setting that breaks every later Job preparation.
    """

    enabled: bool
    endpoint: str = ""
    company_scope_digest: str = ""
    device_id: str = ""
    token_env: str = "NORUCT_COMPANY_COORDINATION_TOKEN"
    allow_insecure_loopback: bool = False

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "company_scope_digest": self.company_scope_digest,
            "device_id": self.device_id,
            "token_env": self.token_env,
            "allow_insecure_loopback": self.allow_insecure_loopback,
        }

    def validated_runtime_config(self) -> RemoteCompanyCoordinationConfig | None:
        return company_coordination_config_from_settings(
            {"company_coordination": self.to_mapping()}
        )


def _table_range(text: str) -> tuple[int, int] | None:
    headers = tuple(_HEADER.finditer(text))
    start_index = next(
        (index for index, item in enumerate(headers) if item.group(1) == _TABLE),
        None,
    )
    if start_index is None:
        return None
    start = headers[start_index].start()
    end = len(text)
    for item in headers[start_index + 1 :]:
        name = item.group(1)
        if name != _TABLE and not name.startswith(f"{_TABLE}."):
            end = item.start()
            break
    return start, end


def _without_table(text: str) -> str:
    section = _table_range(text)
    if section is None:
        return text.strip()
    start, end = section
    return (text[:start] + text[end:]).strip()


def _atomic_write(path: Path, value: str) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-company-coordination-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return target


def company_coordination_table_text(settings: CompanyCoordinationSettings) -> str:
    """Render one strictly bounded table after validating an enabled profile."""

    if settings.enabled:
        settings.validated_runtime_config()
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    fields = [
        "[company_coordination]",
        f"enabled = {'true' if settings.enabled else 'false'}",
    ]
    if settings.enabled:
        fields.extend(
            (
                f"endpoint = {quote(settings.endpoint.strip())}",
                f"company_scope_digest = {quote(settings.company_scope_digest.strip())}",
                f"device_id = {quote(settings.device_id.strip())}",
                f"token_env = {quote(settings.token_env.strip())}",
                "allow_insecure_loopback = " + ("true" if settings.allow_insecure_loopback else "false"),
            )
        )
    return "\n".join(fields) + "\n"


def company_coordination_enrollment_preview(
    settings: CompanyCoordinationSettings,
) -> Mapping[str, object]:
    """Derive one Worker allowlist entry without exposing or transmitting a token.

    This is an operator aid, not enrollment: it makes no network request and
    neither writes local configuration nor mutates the Worker.  The current
    device token is read only long enough to calculate its SHA-256 identity.
    """

    if not settings.enabled:
        raise ValueError("Company coordination enrollment preview requires enabled=true")
    config = settings.validated_runtime_config()
    assert config is not None
    token = os.environ.get(config.token_env, "").strip()
    # ``validated_runtime_config`` has already applied the strict token shape
    # contract.  Keep this defensive check so a future validation change
    # cannot turn this helper into an accidental empty-token enrollment aid.
    if not token:
        raise ValueError("Company coordination token is unavailable")
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return {
        "schema": COMPANY_COORDINATION_ENROLLMENT_PREVIEW_SCHEMA,
        "endpoint": config.endpoint,
        "company_scope_digest": config.company_scope_digest,
        "device_id": config.device_id,
        "token_env": config.token_env,
        "worker_allowlist_entry": {
            token_sha256: {
                "scopes": [config.company_scope_digest],
                "devices": [config.device_id],
            }
        },
        "credential_value_exposed": False,
        "server_mutated": False,
        "network_requested": False,
        "local_state_written": False,
        "activation_scope": "receipt_bound_read_only_and_graph_decision_only",
    }


def company_coordination_preflight(
    settings: CompanyCoordinationSettings,
) -> Mapping[str, object]:
    """Run the sole credentialed no-mutation enrollment preflight."""

    if not settings.enabled:
        raise ValueError("Company coordination preflight requires enabled=true")
    config = settings.validated_runtime_config()
    assert config is not None
    return RemoteCompanyCoordinationClient(config).preflight_identity()


def write_company_coordination_settings(
    path: str | Path,
    settings: CompanyCoordinationSettings,
) -> Path:
    """Atomically replace only the coordination table, preserving all others."""

    target = Path(path).expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    remainder = _without_table(existing)
    return _atomic_write(
        target,
        (remainder + "\n\n" if remainder else "")
        + company_coordination_table_text(settings),
    )


def company_coordination_config_from_settings(
    settings: Mapping[str, Any],
) -> RemoteCompanyCoordinationConfig | None:
    raw = settings.get("company_coordination")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("company_coordination settings must be a table")
    allowed = {
        "enabled", "endpoint", "company_scope_digest", "device_id",
        "token_env", "allow_insecure_loopback",
    }
    if set(raw) - allowed:
        raise ValueError("company_coordination contains an unsupported setting")
    if raw.get("enabled", False) is not True:
        return None
    config = RemoteCompanyCoordinationConfig(
        endpoint=str(raw.get("endpoint", "")).strip(),
        company_scope_digest=str(raw.get("company_scope_digest", "")).strip(),
        device_id=str(raw.get("device_id", "")).strip(),
        token_env=str(raw.get("token_env", "NORUCT_COMPANY_COORDINATION_TOKEN")).strip(),
        allow_insecure_loopback=raw.get("allow_insecure_loopback", False) is True,
    )
    try:
        config.validate()
    except CompanyCoordinationError as error:
        raise ValueError(str(error)) from error
    return config
