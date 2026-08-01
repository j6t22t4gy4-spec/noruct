"""Secret-free persistence for the local approved-route reuse preference.

This boundary deliberately stores only immutable, already-approved metadata.  It
does not resolve providers, credentials, or egress grants, and a missing table
is an explicit first-run state rather than an approval.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)


_TABLE_NAME = "model_routing"
_TABLE_PATTERN = re.compile(
    rf"(?ms)^\[{re.escape(_TABLE_NAME)}\]\s*\n.*?(?=^\[[^\n]+\]\s*\n|\Z)"
)


@dataclass(frozen=True, slots=True)
class LocalRoutingSettings:
    """One local reuse choice and its immutable approved-route registry."""

    policy: UserRoutingPolicy
    approved_routes: ApprovedRouteRegistry

    def __post_init__(self) -> None:
        if not isinstance(self.policy, UserRoutingPolicy):
            raise TypeError("policy must be a UserRoutingPolicy")
        if not isinstance(self.approved_routes, ApprovedRouteRegistry):
            raise TypeError("approved_routes must be an ApprovedRouteRegistry")

    def render(self) -> str:
        quote = lambda value: json.dumps(value, ensure_ascii=False)
        return "\n".join((
            f"[{_TABLE_NAME}]",
            f"policy = {quote(self.policy.canonical_json())}",
            f"approved_routes = {quote(self.approved_routes.canonical_json())}",
            "",
        ))


def first_run_local_routing_settings() -> LocalRoutingSettings:
    """Return a deterministic policy with no route approval implied."""

    return LocalRoutingSettings(
        policy=UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
        approved_routes=ApprovedRouteRegistry(()),
    )


def _strict_json_object(raw: object, field: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a JSON string")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{field} contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _policy_from_canonical_json(raw: object) -> UserRoutingPolicy:
    payload = _strict_json_object(raw, "policy")
    if set(payload) != {"mode"}:
        raise ValueError("policy has unknown or missing fields")
    try:
        policy = UserRoutingPolicy(**payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("policy is invalid") from exc
    if raw != policy.canonical_json():
        raise ValueError("policy is not canonical")
    return policy


def _registry_from_canonical_json(raw: object) -> ApprovedRouteRegistry:
    payload = _strict_json_object(raw, "approved_routes")
    routes = payload.get("routes")
    if set(payload) != {"routes"} or not isinstance(routes, list):
        raise ValueError("approved_routes has unknown or missing fields")
    try:
        registry = ApprovedRouteRegistry(
            tuple(ApprovedRouteMetadata(**route) for route in routes)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("approved_routes is invalid") from exc
    if raw != registry.canonical_json():
        raise ValueError("approved_routes is not canonical")
    return registry


def load_local_routing_settings(path: Path) -> LocalRoutingSettings:
    """Read the single local table strictly, defaulting only when it is absent."""

    target = path.expanduser().resolve()
    if not target.is_file():
        return first_run_local_routing_settings()
    try:
        document = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("local routing settings cannot be read") from exc
    table = document.get(_TABLE_NAME)
    if table is None:
        return first_run_local_routing_settings()
    if not isinstance(table, Mapping) or set(table) != {"policy", "approved_routes"}:
        raise ValueError("local routing settings table is unknown or incomplete")
    return LocalRoutingSettings(
        policy=_policy_from_canonical_json(table["policy"]),
        approved_routes=_registry_from_canonical_json(table["approved_routes"]),
    )


def write_local_routing_settings(path: Path, settings: LocalRoutingSettings) -> Path:
    """Atomically replace only `[model_routing]`, retaining unrelated TOML."""

    if not isinstance(settings, LocalRoutingSettings):
        raise TypeError("settings must be LocalRoutingSettings")
    target = path.expanduser().resolve()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    retained = _TABLE_PATTERN.sub("", existing).strip()
    content = settings.render() + ("\n" + retained + "\n" if retained else "")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-local-routing-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
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
