"""First-party employee credential boundary over the private runtime primitive."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Mapping

from dynamic_firm._vendor.runtime_safety import secret_scope as _scope


SecretScopeError = _scope.UnscopedSecretError


def require_employee_secret_scope(required: bool = True) -> None:
    """Enable fail-closed resolution for concurrent employee execution."""

    _scope.set_scope_required(required)


@contextmanager
def employee_secret_scope(secrets: Mapping[str, str]) -> Iterator[None]:
    """Make a secret mapping authoritative for the current async context."""

    token = _scope.set_secret_scope(secrets)
    try:
        yield
    finally:
        _scope.reset_secret_scope(token)


def resolve_secret(name: str, default: str | None = None) -> str | None:
    return _scope.get_secret(name, default)


def employee_secret_scope_required() -> bool:
    return _scope.is_scope_required()
