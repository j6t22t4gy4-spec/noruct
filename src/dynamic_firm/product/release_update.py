"""Local immutable-release activation and rollback controls.

The public installer may add a new verified version, but it must never mutate
Company state while doing so.  This module owns the remaining local operation:
inspect Noruct-owned immutable versions and atomically select one already
installed version.  It has no network, package-index, provider, or credential
path, so selecting a rollback cannot become an implicit update mechanism.
"""

from __future__ import annotations

import os
import json
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_WINDOWS_WRAPPER_MARKER = ":: Noruct managed command wrapper"


@dataclass(frozen=True, slots=True)
class ReleaseInstallationStatus:
    install_root: str
    command_path: str
    platform: str
    installed_versions: tuple[str, ...]
    active_version: str | None
    command_state: str
    install_root_state: str
    active_receipt_state: str
    verified_receipt_versions: tuple[str, ...]
    local_state_touched: bool = False
    network_accessed: bool = False

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


def default_release_paths() -> tuple[Path, Path]:
    """Return only the installer-owned executable locations, never NORUCT_HOME."""

    if os.name == "nt":
        root = Path(os.environ.get("NORUCT_INSTALL_ROOT", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Noruct"))
        bin_dir = Path(os.environ.get("NORUCT_BIN_DIR", root / "bin"))
    else:
        root = Path(os.environ.get("NORUCT_INSTALL_ROOT", Path.home() / ".local" / "share" / "noruct"))
        bin_dir = Path(os.environ.get("NORUCT_BIN_DIR", Path.home() / ".local" / "bin"))
    return root.expanduser().resolve(), bin_dir.expanduser().resolve()


def _binary_for(root: Path, version: str) -> Path:
    if os.name == "nt":
        return root / "versions" / version / "venv" / "Scripts" / "noruct.exe"
    return root / "versions" / version / "venv" / "bin" / "noruct"


def _command_path(bin_dir: Path) -> Path:
    return bin_dir / ("noruct.cmd" if os.name == "nt" else "noruct")


def _managed_versions(root: Path) -> tuple[str, ...]:
    versions_dir = root / "versions"
    if not versions_dir.is_dir():
        return ()
    values = [
        child.name
        for child in versions_dir.iterdir()
        if child.is_dir() and _VERSION.fullmatch(child.name) and _binary_for(root, child.name).is_file()
    ]
    return tuple(sorted(values, key=_version_key))


def _receipt_state(root: Path, version: str) -> str:
    """Validate only the local immutable installer receipt shape.

    The receipt is evidence that the rendered installer bound the version to a
    manifest/artifact digest at install time.  It is intentionally not a
    substitute for publisher signing or a remote update trust root.
    """

    path = root / "versions" / version / "release-receipt.json"
    if not path.is_file() or path.is_symlink():
        return "MISSING"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "INVALID"
    if not isinstance(value, dict) or set(value) != {
        "schema", "version", "target", "manifest_url", "manifest_sha256", "wheel_sha256", "employee_runtime_sha256"
    }:
        return "INVALID"
    if value.get("schema") != "noruct.release-installation-receipt.v1" or value.get("version") != version:
        return "INVALID"
    if not isinstance(value.get("target"), str) or not isinstance(value.get("manifest_url"), str) or not value["manifest_url"].startswith("https://"):
        return "INVALID"
    for field in ("manifest_sha256", "wheel_sha256", "employee_runtime_sha256"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            return "INVALID"
    return "VALID"


def _install_root_state(root: Path) -> str:
    """Expose installer-root removal eligibility without performing deletion."""

    if not root.is_dir():
        return "NOT_INSTALLED"
    marker = root / ".noruct-install-root-v1"
    if not marker.exists():
        return "MISSING_MANAGED_MARKER"
    if marker.is_symlink() or not marker.is_file():
        return "INVALID_MANAGED_MARKER"
    try:
        value = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "INVALID_MANAGED_MARKER"
    return "MANAGED_MARKER_VALID" if value == "noruct-install-root-v1\n" else "INVALID_MANAGED_MARKER"


def _version_key(value: str) -> tuple[tuple[tuple[int, int | str], ...], str]:
    # Avoid a third-party semver parser in the shipped product.  The original
    # string stays as a deterministic tiebreaker for pre-release notation.
    return tuple(
        (0, int(item)) if item.isdecimal() else (1, item)
        for item in re.split(r"[.+-]", value)
    ), value


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _active_unix(root: Path, command: Path, versions: tuple[str, ...]) -> tuple[str | None, str]:
    if not command.exists() and not command.is_symlink():
        return None, "NOT_INSTALLED"
    if not command.is_symlink():
        return None, "UNMANAGED_COMMAND"
    try:
        target = command.resolve(strict=True)
    except OSError:
        return None, "INVALID_COMMAND_LINK"
    if not _is_inside(target, root):
        return None, "UNMANAGED_COMMAND"
    for version in versions:
        if target == _binary_for(root, version).resolve():
            return version, "READY"
    return None, "INVALID_COMMAND_LINK"


def _active_windows(root: Path, command: Path, versions: tuple[str, ...]) -> tuple[str | None, str]:
    if not command.is_file():
        return None, "NOT_INSTALLED"
    try:
        text = command.read_text(encoding="ascii")
    except OSError:
        return None, "UNMANAGED_COMMAND"
    if _WINDOWS_WRAPPER_MARKER not in text:
        return None, "UNMANAGED_COMMAND"
    for version in versions:
        binary = str(_binary_for(root, version))
        if binary in text:
            return version, "READY"
    return None, "INVALID_COMMAND_LINK"


def release_installation_status(
    *, install_root: Path | None = None, bin_dir: Path | None = None
) -> ReleaseInstallationStatus:
    default_root, default_bin = default_release_paths()
    root = (install_root or default_root).expanduser().resolve()
    selected_bin = (bin_dir or default_bin).expanduser().resolve()
    versions = _managed_versions(root)
    command = _command_path(selected_bin)
    active, state = (
        _active_windows(root, command, versions)
        if os.name == "nt"
        else _active_unix(root, command, versions)
    )
    receipt_versions = tuple(version for version in versions if _receipt_state(root, version) == "VALID")
    return ReleaseInstallationStatus(
        install_root=str(root),
        command_path=str(command),
        platform="windows" if os.name == "nt" else "unix",
        installed_versions=versions,
        active_version=active,
        command_state=state,
        install_root_state=_install_root_state(root),
        active_receipt_state=_receipt_state(root, active) if active else "NOT_APPLICABLE",
        verified_receipt_versions=receipt_versions,
    )


def activate_installed_release(
    version: str,
    *,
    install_root: Path | None = None,
    bin_dir: Path | None = None,
) -> ReleaseInstallationStatus:
    """Atomically select an already-installed Noruct version.

    This accepts no URL, archive, executable, or state path.  It refuses to
    overwrite an unmanaged command and only rewrites the command link/wrapper
    after the requested release binary is inside the Noruct install root.
    """

    if not _VERSION.fullmatch(version):
        raise ValueError("Release version must use the installed Noruct version format")
    status = release_installation_status(install_root=install_root, bin_dir=bin_dir)
    root = Path(status.install_root)
    command = Path(status.command_path)
    binary = _binary_for(root, version)
    if version not in status.installed_versions or not binary.is_file() or not _is_inside(binary, root):
        raise ValueError("Requested Noruct version is not an installed managed release")
    if status.command_state == "UNMANAGED_COMMAND":
        raise ValueError("Refusing to replace a command not managed by Noruct")
    command.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        wrapper = f"{_WINDOWS_WRAPPER_MARKER}\r\n@echo off\r\n\"{binary}\" %*\r\n"
        _atomic_file_replace(command, wrapper)
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=".noruct-activate-", dir=command.parent)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            temporary_path.unlink()
            os.symlink(binary, temporary_path)
            os.replace(temporary_path, command)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return release_installation_status(install_root=root, bin_dir=command.parent)


def _atomic_file_replace(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-activate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\r\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
