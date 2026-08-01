"""Immutable first-release capability claims."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseScopeClaim:
    """A product-safe claim about one first-release capability."""

    capability: str
    enabled: bool
    reason_code: str


RELEASE_SCOPE_CLAIMS: tuple[ReleaseScopeClaim, ...] = (
    ReleaseScopeClaim(
        "unrestricted_in_process_plugin", False, "UNSUPPORTED_IN_PROCESS_PLUGIN"
    ),
    ReleaseScopeClaim(
        "silent_marketplace_update", False, "UNSUPPORTED_MARKETPLACE_UPDATE"
    ),
    ReleaseScopeClaim(
        "broad_autonomous_replanning", False, "UNSUPPORTED_AUTONOMOUS_REPLANNING"
    ),
    ReleaseScopeClaim(
        "customer_shared_automatic_evolution", False, "UNSUPPORTED_SHARED_EVOLUTION"
    ),
    ReleaseScopeClaim("silent_oauth_sync", False, "UNSUPPORTED_SILENT_OAUTH"),
    ReleaseScopeClaim(
        "hosted_multi_user_control_plane", False, "UNSUPPORTED_HOSTED_CONTROL_PLANE"
    ),
)

_CLAIMS_BY_CAPABILITY = {claim.capability: claim for claim in RELEASE_SCOPE_CLAIMS}
_UNKNOWN_CLAIM = ReleaseScopeClaim("unknown", False, "UNSUPPORTED_CAPABILITY")


def release_scope_claim(capability: object) -> ReleaseScopeClaim:
    """Return a first-release claim, failing closed without echoing input."""

    if not isinstance(capability, str):
        return _UNKNOWN_CLAIM
    return _CLAIMS_BY_CAPABILITY.get(capability, _UNKNOWN_CLAIM)
