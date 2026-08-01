"""Context-local secret resolution derived from Hermes Agent.

Upstream: NousResearch/hermes-agent
Commit: 7fe1cb384e4f99aae3243c4c578904ac8c114b25
Path: agent/secret_scope.py
Upstream SHA-256: e4fc76b51e360b96a773927b19e2e1ce592e9295d8ecd747dcf383e7668b48f2
Copyright (c) 2025 Nous Research
SPDX-License-Identifier: MIT

Modified for Dynamic Firm: removed Hermes profile paths, .env parsing and
process-global variable allowlists; renamed the deployment mode to scoped
execution and retained authoritative nested ContextVar scopes.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Mapping


_SCOPE_REQUIRED = False
_SECRET_SCOPE: ContextVar[Mapping[str, str] | None] = ContextVar(
    "_NORUCT_SECRET_SCOPE",
    default=None,
)


class UnscopedSecretError(RuntimeError):
    """Raised when scoped execution reads a credential without a scope."""


def set_scope_required(required: bool) -> None:
    """Require all credential reads to occur inside a context-local scope."""

    global _SCOPE_REQUIRED
    _SCOPE_REQUIRED = bool(required)


def is_scope_required() -> bool:
    return _SCOPE_REQUIRED


def set_secret_scope(secrets: Mapping[str, str] | None) -> Token:
    """Install an authoritative secret mapping for the current context."""

    return _SECRET_SCOPE.set(secrets)


def reset_secret_scope(token: Token) -> None:
    _SECRET_SCOPE.reset(token)


def current_secret_scope() -> Mapping[str, str] | None:
    return _SECRET_SCOPE.get()


def get_secret(name: str, default: str | None = None) -> str | None:
    """Resolve from the current scope, environment fallback, or fail closed."""

    scope = _SECRET_SCOPE.get()
    if scope is not None:
        value = scope.get(name)
        return value if value is not None else default
    if _SCOPE_REQUIRED:
        raise UnscopedSecretError(
            f"Credential {name!r} was read without an employee secret scope "
            "while scoped execution is required."
        )
    value = os.environ.get(name)
    return value if value is not None else default
