"""Safe, local-only fragments for Shared Evolution operator enrollment."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Mapping


_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_ROLES = frozenset({"contributor", "finalizer", "reviewer", "publisher"})
_AUTHORITIES = frozenset({"INDIVIDUAL", "ORGANIZATION_OWNER"})


def operator_enrollment_preview(
    *, role: str, token_env: str, identity: str | None = None,
    authority: str = "INDIVIDUAL",
) -> Mapping[str, object]:
    """Create one merge-only Worker allowlist fragment without exposing a token.

    The caller provisions the returned fragment through the private Worker
    secret workflow.  It is deliberately not a mutation, token issuer, or
    evidence that customer intake is authorized.
    """

    if role not in _ROLES:
        raise ValueError("Shared Evolution operator role is invalid")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", token_env):
        raise ValueError("Shared Evolution operator token environment variable is invalid")
    token = os.environ.get(token_env, "").strip()
    if not token or len(token) > 512 or "\r" in token or "\n" in token:
        raise ValueError("Shared Evolution operator token is unavailable")
    if role == "finalizer":
        if identity is not None:
            raise ValueError("Finalizer enrollment does not accept an identity")
        if authority != "INDIVIDUAL":
            raise ValueError("Finalizer enrollment does not accept an authority")
        secret_name = "FINALIZER_TOKEN_SHA256_ALLOWLIST"
        fragment: object = [hashlib.sha256(token.encode("utf-8")).hexdigest()]
    else:
        if not isinstance(identity, str) or not _SAFE_ID.fullmatch(identity):
            raise ValueError("Shared Evolution operator identity is invalid")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if role == "contributor":
            if authority not in _AUTHORITIES:
                raise ValueError("Shared Evolution contributor authority is invalid")
            secret_name = "CONTRIBUTOR_TOKEN_SHA256_ALLOWLIST"
            fragment = {token_hash: {"identity": identity, "authorities": [authority]}}
        elif role == "reviewer":
            if authority != "INDIVIDUAL":
                raise ValueError("Reviewer enrollment does not accept an authority")
            secret_name = "REVIEWER_TOKEN_SHA256_ALLOWLIST"
            fragment = {token_hash: identity}
        else:
            if authority != "INDIVIDUAL":
                raise ValueError("Publisher enrollment does not accept an authority")
            secret_name = "PUBLISHER_TOKEN_SHA256_ALLOWLIST"
            fragment = {token_hash: identity}
    return {
        "schema": "noruct.evolution-operator-enrollment-preview.v1",
        "role": role.upper(),
        "token_environment": token_env,
        "worker_secret_name": secret_name,
        "merge_only_allowlist_fragment": fragment,
        "network_requested": False,
        "worker_mutated": False,
        "local_state_written": False,
        "token_exposed": False,
        "customer_operation_authorized": False,
    }
