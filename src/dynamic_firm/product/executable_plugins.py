"""Approved local executable-plugin lifecycle behind the Noruct tool boundary.

This is intentionally not an in-process Python plugin loader.  A plugin is a
user-installed directory with a small JSON manifest and one executable host
process.  Its declared tools are made visible to the employee only after an
explicit enable action; each call remains a HIGH, individually approved native
ToolIntent.  The host process receives one bounded JSON request on stdin and
returns one bounded JSON response on stdout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from dynamic_firm.product.plugin_lifecycle_receipts import (
    append_selected_plugin_receipt,
    lifecycle_receipts,
)
from dynamic_firm.product.executable_plugin_protocol import (
    bounded_exchange,
    reject_json_constant,
    terminate_process,
)
from dynamic_firm.product.plugin_registry import (
    read_plugin_registry,
    write_plugin_registry,
)
from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled
from dynamic_firm.runtime.tools import ToolDefinition, ToolExecutionError, ToolValidationError


PLUGIN_SCHEMA = "noruct.executable-plugin.v1"
REQUEST_SCHEMA = "noruct.executable-plugin-request.v1"
RESPONSE_SCHEMA = "noruct.executable-plugin-response.v1"
REGISTRY_SCHEMA = "noruct.executable-plugin-registry.v1"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_TOOL_IDENTIFIER = re.compile(r"plugin_[a-z][a-z0-9_]{0,62}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_MAX_PACKAGE_FILES = 128
_MAX_PACKAGE_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 32_000
_MAX_RESPONSE_BYTES = 64_000
_MAX_REQUEST_BYTES = 64_000
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_REF = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,239}\Z")
_DEPENDENCY_LOCK = "requirements.lock"
_MAX_LOCK_BYTES = 256_000


class PluginLifecycleError(ValueError):
    """A safe plugin lifecycle error."""


@dataclass(frozen=True, slots=True)
class PluginGitUpdateReview:
    """An immutable candidate discovered from an already-installed Git receipt.

    The object deliberately contains neither a checkout nor a package diff.
    Reviewing an update may use the network, but it must not change the local
    plugin registry, install a package, or make a plugin runnable.
    """

    plugin_id: str
    installed_version: str
    installed_commit: str
    repository_url: str
    subdirectory: str
    ref: str
    candidate_commit: str

    @property
    def update_available(self) -> bool:
        return self.installed_commit != self.candidate_commit


@dataclass(frozen=True, slots=True)
class ExecutablePluginTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExecutablePlugin:
    plugin_id: str
    version: str
    description: str
    package_path: Path
    command: tuple[str, ...]
    environment_names: tuple[str, ...]
    timeout_seconds: float
    tools: tuple[ExecutablePluginTool, ...]
    package_digest: str
    enabled: bool
    dependency_lock: str | None = None
    dependency_lock_digest: str | None = None
    dependency_environment: Path | None = None

    def validate(self) -> None:
        if not _IDENTIFIER.fullmatch(self.plugin_id):
            raise PluginLifecycleError("Plugin id is invalid")
        if not isinstance(self.version, str) or not self.version or len(self.version.encode()) > 64:
            raise PluginLifecycleError("Plugin version is invalid")
        if not isinstance(self.description, str) or len(self.description.encode()) > 1_000:
            raise PluginLifecycleError("Plugin description is invalid")
        if not isinstance(self.package_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.package_digest):
            raise PluginLifecycleError("Plugin package digest is invalid")
        root = self.package_path.expanduser().resolve()
        if not root.is_dir():
            raise PluginLifecycleError("Plugin package is not installed")
        if _package_tree_digest(root) != self.package_digest:
            raise PluginLifecycleError("Plugin package contents no longer match the installed digest")
        if (self.dependency_lock is None) != (self.dependency_lock_digest is None):
            raise PluginLifecycleError("Plugin dependency lock metadata is invalid")
        if self.dependency_lock is not None:
            if self.dependency_lock != _DEPENDENCY_LOCK:
                raise PluginLifecycleError("Plugin dependency lock must be requirements.lock")
            lock = root / self.dependency_lock
            if lock.is_symlink() or not lock.is_file() or lock.stat().st_size > _MAX_LOCK_BYTES:
                raise PluginLifecycleError("Plugin dependency lock is invalid")
            if hashlib.sha256(lock.read_bytes()).hexdigest() != self.dependency_lock_digest:
                raise PluginLifecycleError("Plugin dependency lock no longer matches the installed receipt")
        if not 1 <= len(self.command) <= 8 or not all(isinstance(item, str) and item and "\x00" not in item and len(item.encode()) <= 512 for item in self.command):
            raise PluginLifecycleError("Plugin command is invalid")
        command_path = (root / self.command[0]).resolve()
        try:
            command_path.relative_to(root)
        except ValueError as exc:
            raise PluginLifecycleError("Plugin command escapes its package") from exc
        if command_path.is_symlink() or not command_path.is_file() or not os.access(command_path, os.X_OK):
            raise PluginLifecycleError("Plugin command must be an executable regular package file")
        if len(self.environment_names) > 16 or len(set(self.environment_names)) != len(self.environment_names) or any(not _ENVIRONMENT_NAME.fullmatch(item) for item in self.environment_names):
            raise PluginLifecycleError("Plugin environment declaration is invalid")
        if not 1 <= self.timeout_seconds <= 120:
            raise PluginLifecycleError("Plugin timeout must be between 1 and 120 seconds")
        if not 1 <= len(self.tools) <= 16 or len({item.name for item in self.tools}) != len(self.tools):
            raise PluginLifecycleError("Plugin must declare one to sixteen unique tools")
        for tool in self.tools:
            if not _TOOL_IDENTIFIER.fullmatch(tool.name) or not tool.name.startswith(f"plugin_{self.plugin_id}_"):
                raise PluginLifecycleError("Plugin tool name must begin with plugin_<plugin-id>_")
            if not isinstance(tool.description, str) or not tool.description.strip() or len(tool.description.encode()) > 2_000:
                raise PluginLifecycleError("Plugin tool description is invalid")
            _validate_input_schema(tool.input_schema)

    @property
    def command_path(self) -> Path:
        return (self.package_path.expanduser().resolve() / self.command[0]).resolve()

    @property
    def dependency_environment_ready(self) -> bool:
        if self.dependency_lock is None:
            return True
        if self.dependency_environment is None:
            return False
        return _environment_python(self.dependency_environment) is not None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        self.validate()
        return tuple(self._definition(tool) for tool in self.tools)

    def _definition(self, tool: ExecutablePluginTool) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not isinstance(arguments, Mapping):
                raise ToolValidationError("Plugin tool arguments must be an object")
            _validate_value(arguments, tool.input_schema)
            return dict(arguments)

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            return await self._invoke(tool.name, arguments, cancellation)

        return ToolDefinition(
            name=tool.name,
            description=f"[{self.plugin_id}@{self.version}] {tool.description}",
            input_schema=tool.input_schema,
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda _: f"plugin:{self.plugin_id}:{self.version}:{tool.name}",
            handler=handle,
            timeout_ms=int(self.timeout_seconds * 1000),
            output_limit_bytes=_MAX_RESPONSE_BYTES,
            requires_approval=True,
            approval_preview=lambda _: f"Run {self.plugin_id}@{self.version} / {tool.name}",
            allow_session_approval=False,
            parallel_safe=False,
        )

    async def _invoke(self, tool_name: str, arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
        try:
            self.validate()
        except (PluginLifecycleError, OSError) as exc:
            raise ToolExecutionError(
                "Plugin exact installed package is unavailable or changed"
            ) from exc
        environment = {name: os.environ[name] for name in self.environment_names if name in os.environ}
        missing = [name for name in self.environment_names if name not in environment]
        if missing:
            raise ToolExecutionError("Plugin is missing a declared environment variable")
        if not self.dependency_environment_ready:
            raise ToolExecutionError("Plugin dependency environment is not built for this exact installed version")
        executable_path = os.environ.get("PATH", "")
        if self.dependency_environment is not None:
            executable = _environment_python(self.dependency_environment)
            assert executable is not None
            executable_path = str(executable.parent) + os.pathsep + executable_path
        try:
            request = json.dumps(
                {
                    "schema": REQUEST_SCHEMA,
                    "plugin_id": self.plugin_id,
                    "version": self.version,
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("Plugin request is not bounded JSON") from exc
        if len(request) > _MAX_REQUEST_BYTES:
            raise ToolExecutionError("Plugin request exceeds the input limit")
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.command_path), *self.command[1:],
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=str(self.package_path),
                env={"PATH": executable_path, "HOME": os.environ.get("HOME", ""), "LANG": os.environ.get("LANG", "C"), **environment},
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ToolExecutionError("Plugin process could not start") from exc
        exchange = asyncio.create_task(
            bounded_exchange(process, request, max_response_bytes=_MAX_RESPONSE_BYTES)
        )
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({exchange, cancelled}, timeout=self.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                await terminate_process(process)
                raise OperationCancelled(cancellation.reason or "Plugin call cancelled")
            if exchange not in done:
                await terminate_process(process)
                raise ToolExecutionError("Plugin call timed out")
            stdout = exchange.result()
        finally:
            cancelled.cancel()
            if process.returncode is None:
                await terminate_process(process)
            if not exchange.done():
                exchange.cancel()
            await asyncio.gather(exchange, cancelled, return_exceptions=True)
        if process.returncode != 0:
            raise ToolExecutionError("Plugin process failed")
        try:
            response = json.loads(
                stdout.decode("utf-8"),
                parse_constant=reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ToolExecutionError("Plugin response is not valid JSON") from exc
        if not isinstance(response, Mapping) or response.get("schema") != RESPONSE_SCHEMA or response.get("ok") is not True or set(response) != {"schema", "ok", "result"}:
            raise ToolExecutionError("Plugin response does not match the Noruct plugin protocol")
        try:
            return json.dumps(
                {
                    "plugin_id": self.plugin_id,
                    "version": self.version,
                    "tool_name": tool_name,
                    "result": response["result"],
                },
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ToolExecutionError("Plugin result is not bounded JSON") from exc


def _validate_input_schema(schema: object) -> None:
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise PluginLifecycleError("Plugin tool schema must be an object schema")
    if set(schema) - {"type", "properties", "required", "additionalProperties"}:
        raise PluginLifecycleError("Plugin tool schema uses unsupported keywords")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list) or schema.get("additionalProperties") is not False:
        raise PluginLifecycleError("Plugin tool schema must have fixed object properties")
    if len(properties) > 24 or len(required) > len(properties) or not all(isinstance(key, str) and _IDENTIFIER.fullmatch(key) for key in properties) or not all(isinstance(key, str) and key in properties for key in required):
        raise PluginLifecycleError("Plugin tool schema properties are invalid")
    for value in properties.values():
        if not isinstance(value, Mapping) or value.get("type") not in {"string", "integer", "number", "boolean", "array"} or set(value) - {"type", "enum", "maxLength", "items", "maxItems"}:
            raise PluginLifecycleError("Plugin tool property schema is invalid")
        if value.get("type") == "array":
            items = value.get("items")
            if not isinstance(items, Mapping) or items.get("type") not in {"string", "integer", "number", "boolean"} or set(items) - {"type", "enum", "maxLength"}:
                raise PluginLifecycleError("Plugin array schema is invalid")


def _validate_value(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if set(arguments) - set(properties) or any(item not in arguments for item in required):
        raise ToolValidationError("Plugin arguments do not match the declared schema")
    for name, value in arguments.items():
        _validate_property(value, properties[name])


def _validate_property(value: object, schema: Mapping[str, Any]) -> None:
    kind = schema["type"]
    valid = {
        "string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        ),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
    }[kind]
    if not valid:
        raise ToolValidationError("Plugin argument type does not match the declared schema")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError("Plugin argument is outside the declared enum")
    if isinstance(value, str) and (len(value.encode("utf-8")) > int(schema.get("maxLength", 4_000))):
        raise ToolValidationError("Plugin text argument exceeds its limit")
    if isinstance(value, list):
        if len(value) > int(schema.get("maxItems", 32)):
            raise ToolValidationError("Plugin array argument exceeds its limit")
        for item in value:
            _validate_property(item, schema["items"])


class ExecutablePluginStore:
    """Local stage/install/enable/disable/remove lifecycle for plugin packages."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.package_root = self.root / "packages"
        self.environment_root = self.root / "environments"
        self.registry_path = self.root / "registry.json"

    def list(self) -> tuple[ExecutablePlugin, ...]:
        raw = self._read_registry()
        return tuple(
            _plugin_from_record(item, dependency_environment=_recorded_environment(raw, item, self.environment_root))
            for item in raw["plugins"]
        )

    def active(self) -> tuple[ExecutablePlugin, ...]:
        """Return only versions safe to expose as executable Company tools.

        A dependency-bearing version remains visibly enabled in its receipt,
        but is intentionally absent from the runtime until its exact locked
        environment exists. This prevents a Company Job from receiving a tool
        that can only fail later because an operator has not completed setup.
        """
        return tuple(item for item in self.list() if item.enabled and item.dependency_environment_ready)

    def install(self, source: Path) -> ExecutablePlugin:
        return self._install(source, receipt={"kind": "local_directory"})

    def install_git(
        self,
        repository_url: str,
        commit: str,
        *,
        subdirectory: str = "",
        catalog_provenance: Mapping[str, str] | None = None,
    ) -> ExecutablePlugin:
        """Stage one exact HTTPS Git commit; it remains disabled after install."""
        if not isinstance(repository_url, str) or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?/[A-Za-z0-9._~:/-]+(?:\.git)?", repository_url):
            raise PluginLifecycleError("Plugin Git source must be an HTTPS repository URL without credentials, query, or fragment")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise PluginLifecycleError("Plugin Git install requires an exact 40-character commit")
        if not isinstance(subdirectory, str) or len(subdirectory.encode()) > 240 or "\x00" in subdirectory:
            raise PluginLifecycleError("Plugin Git subdirectory is invalid")
        relative = Path(subdirectory or ".")
        if relative.is_absolute() or ".." in relative.parts:
            raise PluginLifecycleError("Plugin Git subdirectory must remain inside the repository")
        provenance = _catalog_provenance(catalog_provenance)
        git = shutil.which("git")
        if not git:
            raise PluginLifecycleError("Git executable was not found")
        with tempfile.TemporaryDirectory(prefix="noruct-plugin-git-") as temporary:
            checkout = Path(temporary) / "checkout"
            self._git((git, "clone", "--no-checkout", "--filter=blob:none", repository_url, str(checkout)))
            self._git((git, "-C", str(checkout), "checkout", "--detach", commit))
            resolved = self._git((git, "-C", str(checkout), "rev-parse", "HEAD")).strip().lower()
            if resolved != commit.lower():
                raise PluginLifecycleError("Git checkout did not resolve to the requested commit")
            candidate = checkout / relative
            if candidate.is_symlink():
                raise PluginLifecycleError("Plugin Git subdirectory may not be a symbolic link")
            source = candidate.resolve()
            try:
                source.relative_to(checkout.resolve())
            except ValueError as exc:
                raise PluginLifecycleError("Plugin Git subdirectory escaped the checkout") from exc
            return self._install(
                source,
                receipt={
                    "kind": "git", "repository_url": repository_url, "commit": resolved,
                    "subdirectory": relative.as_posix(), **provenance,
                },
            )

    def source_receipt(self, plugin_id: str, *, version: str) -> Mapping[str, str]:
        """Return one safe, local source-provenance projection for an installed version."""

        plugin_id = _checked_id(plugin_id)
        if not isinstance(version, str) or not version or len(version.encode("utf-8")) > 64:
            raise PluginLifecycleError("Plugin version is invalid")
        registry = self._read_registry()
        if not any(item["plugin_id"] == plugin_id and item["version"] == version for item in registry["plugins"]):
            raise PluginLifecycleError("Requested plugin version is not installed")
        receipt = registry["receipts"].get(f"{plugin_id}@{version}")
        if receipt is None:
            # Registries created before source receipts were introduced remain
            # listable. They cannot be treated as Git-update candidates.
            return {"kind": "legacy_unrecorded"}
        return _safe_source_receipt(receipt)

    def review_git_update(self, plugin_id: str, *, ref: str, version: str | None = None) -> PluginGitUpdateReview:
        """Resolve one allowed remote ref for an installed Git-backed plugin.

        A branch or tag is only a discovery pointer here.  The returned SHA is
        the immutable value an operator must separately pass to ``install-git``.
        No checkout is made and no mutable ref is saved as an installed source.
        """
        plugin_id = _checked_id(plugin_id)
        if not isinstance(ref, str) or not _GIT_REF.fullmatch(ref) or ".." in ref or "//" in ref:
            raise PluginLifecycleError("Plugin update ref must be one refs/heads/* or refs/tags/* name")
        registry = self._read_registry()
        matching = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if not matching:
            raise PluginLifecycleError("Plugin is not installed")
        selected = (
            next((item for item in matching if item["version"] == version), None)
            if version is not None
            else next((item for item in matching if item["enabled"]), matching[-1])
        )
        if selected is None:
            raise PluginLifecycleError("Requested plugin version is not installed")
        receipt = registry["receipts"].get(f"{plugin_id}@{selected['version']}")
        if not isinstance(receipt, Mapping) or receipt.get("kind") != "git":
            raise PluginLifecycleError("Plugin version was not installed from an exact Git receipt")
        _safe_source_receipt(receipt)
        repository_url = receipt.get("repository_url")
        installed_commit = receipt.get("commit")
        subdirectory = receipt.get("subdirectory")
        if not isinstance(repository_url, str) or not isinstance(installed_commit, str) or not _COMMIT.fullmatch(installed_commit) or not isinstance(subdirectory, str):
            raise PluginLifecycleError("Plugin Git receipt is malformed")
        git = shutil.which("git")
        if not git:
            raise PluginLifecycleError("Git executable was not found")
        output = self._git((git, "ls-remote", "--refs", repository_url, ref))
        lines = [line.split("\t", 1) for line in output.splitlines() if line.strip()]
        if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref or not _COMMIT.fullmatch(lines[0][0]):
            raise PluginLifecycleError("Git remote did not resolve the requested exact ref")
        return PluginGitUpdateReview(
            plugin_id=plugin_id,
            installed_version=str(selected["version"]),
            installed_commit=installed_commit.lower(),
            repository_url=repository_url,
            subdirectory=subdirectory,
            ref=ref,
            candidate_commit=lines[0][0].lower(),
        )

    def build_dependency_environment(self, plugin_id: str, *, version: str | None = None, python_command: str | None = None) -> ExecutablePlugin:
        """Build one isolated, hash-locked dependency environment explicitly.

        This operation is intentionally separate from install/enable. It does
        not run plugin code and only accepts the package's immutable
        ``requirements.lock`` receipt through pip's ``--require-hashes`` mode.
        """
        plugin_id = _checked_id(plugin_id)
        registry = self._read_registry()
        matching = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if not matching:
            raise PluginLifecycleError("Plugin is not installed")
        selected = matching[-1] if version is None else next((item for item in matching if item["version"] == version), None)
        if selected is None:
            raise PluginLifecycleError("Requested plugin version is not installed")
        plugin = _plugin_from_record(selected)
        if plugin.dependency_lock is None or plugin.dependency_lock_digest is None:
            raise PluginLifecycleError("Plugin has no hash-locked dependency environment")
        command = str(python_command or sys.executable).strip()
        if not command or os.path.sep not in command and shutil.which(command) is None:
            raise PluginLifecycleError("Configured Python executable was not found")
        target = self.environment_root / plugin.plugin_id / plugin.version
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="noruct-plugin-venv-", dir=target.parent) as temporary:
            candidate = Path(temporary) / "environment"
            self._run_dependency_command((command, "-m", "venv", str(candidate)), timeout=90)
            environment_python = _environment_python(candidate)
            if environment_python is None:
                raise PluginLifecycleError("Plugin dependency environment could not be created")
            self._run_dependency_command((str(environment_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--only-binary=:all:", "--require-hashes", "-r", str(plugin.package_path / plugin.dependency_lock)), timeout=300)
            if target.exists():
                shutil.rmtree(target)
            os.replace(candidate, target)
        registry["environments"][f"{plugin.plugin_id}@{plugin.version}"] = {
            "lock_sha256": plugin.dependency_lock_digest,
            "python": str(_environment_python(target)),
        }
        self._append_receipt(
            registry,
            action="DEPENDENCY_ENVIRONMENT_BUILT",
            plugin_id=plugin.plugin_id,
            selected=(selected,),
        )
        self._write_registry(registry)
        return _plugin_from_record(selected, dependency_environment=target)

    @staticmethod
    def _run_dependency_command(command: Sequence[str], *, timeout: float) -> None:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False, env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": os.environ.get("LANG", "C")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PluginLifecycleError("Plugin dependency environment operation failed") from exc
        if result.returncode != 0:
            raise PluginLifecycleError("Plugin dependency environment operation failed")

    @staticmethod
    def _git(command: Sequence[str]) -> str:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PluginLifecycleError("Git plugin source operation failed") from exc
        if result.returncode != 0:
            raise PluginLifecycleError("Git plugin source operation failed")
        return result.stdout

    def _install(self, source: Path, *, receipt: Mapping[str, str]) -> ExecutablePlugin:
        candidate = source.expanduser()
        if candidate.is_symlink():
            raise PluginLifecycleError("Plugin source may not be a symbolic link")
        source = candidate.resolve()
        manifest, tree_digest = _read_source_manifest(source)
        plugin_id = str(manifest["plugin_id"])
        version = str(manifest["version"])
        destination = self.package_root / plugin_id / version
        if destination.exists():
            # ``remove`` deliberately withdraws a version only from the
            # future-Job registry.  Its immutable package bytes remain here
            # for any already assembled Job.  The exact same reviewed bytes
            # may be explicitly re-intaken; different bytes must never reuse
            # the old version path.
            if _package_tree_digest(destination) != tree_digest:
                raise PluginLifecycleError("That plugin version identity is retained with different package bytes")
            plugin = _plugin_from_manifest(manifest, destination, tree_digest, enabled=False)
            registry = self._read_registry()
            if any(
                item["plugin_id"] == plugin_id and item["version"] == version
                for item in registry["plugins"]
            ):
                raise PluginLifecycleError("That plugin version is already installed")
            registry["plugins"].append(_plugin_record(plugin))
            registry["receipts"][f"{plugin_id}@{version}"] = dict(receipt)
            self._append_receipt(
                registry,
                action="INSTALLED_INACTIVE",
                plugin_id=plugin_id,
                selected=(_plugin_record(plugin),),
            )
            self._write_registry(registry)
            return plugin
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            plugin = _plugin_from_manifest(manifest, destination, tree_digest, enabled=False)
            plugin.validate()
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            raise
        registry = self._read_registry()
        registry["plugins"] = [item for item in registry["plugins"] if not (item["plugin_id"] == plugin_id and item["version"] == version)] + [_plugin_record(plugin)]
        registry["receipts"][f"{plugin_id}@{version}"] = dict(receipt)
        self._append_receipt(
            registry,
            action="INSTALLED_INACTIVE",
            plugin_id=plugin_id,
            selected=(_plugin_record(plugin),),
        )
        self._write_registry(registry)
        return plugin

    def set_enabled(self, plugin_id: str, enabled: bool) -> ExecutablePlugin:
        return self.activate(plugin_id, version=None) if enabled else self._set_disabled(plugin_id)

    def activate(
        self,
        plugin_id: str,
        *,
        version: str | None,
        receipt_action: str = "ACTIVATED_FUTURE_JOB",
    ) -> ExecutablePlugin:
        """Activate one reviewed installed version and disable its siblings."""
        plugin_id = _checked_id(plugin_id)
        registry = self._read_registry()
        matching = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if not matching:
            raise PluginLifecycleError("Plugin is not installed")
        selected = matching[-1] if version is None else next((item for item in matching if item["version"] == version), None)
        if selected is None:
            raise PluginLifecycleError("Requested plugin version is not installed")
        for item in registry["plugins"]:
            if item["plugin_id"] == plugin_id:
                item["enabled"] = item is selected
        self._append_receipt(
            registry,
            action=receipt_action,
            plugin_id=plugin_id,
            selected=(selected,),
        )
        self._write_registry(registry)
        return _plugin_from_record(selected | {"enabled": True})

    def rollback(self, plugin_id: str) -> ExecutablePlugin:
        """Move the active version to its immediately preceding installed receipt."""
        plugin_id = _checked_id(plugin_id)
        registry = self._read_registry()
        matching = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if len(matching) < 2:
            raise PluginLifecycleError("Plugin rollback requires at least two installed versions")
        active_index = next((index for index, item in enumerate(matching) if item["enabled"]), len(matching) - 1)
        if active_index == 0:
            raise PluginLifecycleError("Plugin has no earlier installed version to roll back to")
        return self.activate(
            plugin_id,
            version=str(matching[active_index - 1]["version"]),
            receipt_action="ROLLED_BACK_FUTURE_JOB",
        )

    def _set_disabled(self, plugin_id: str) -> ExecutablePlugin:
        plugin_id = _checked_id(plugin_id)
        registry = self._read_registry()
        matching = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if not matching:
            raise PluginLifecycleError("Plugin is not installed")
        selected = next((item for item in matching if item["enabled"]), matching[-1])
        for item in matching:
            item["enabled"] = False
        self._append_receipt(
            registry,
            action="DISABLED_FUTURE_JOB",
            plugin_id=plugin_id,
            selected=(selected,),
        )
        self._write_registry(registry)
        return _plugin_from_record(selected | {"enabled": False})

    def remove(self, plugin_id: str) -> bool:
        """Withdraw a plugin from future Jobs without deleting pinned bytes.

        An already assembled runtime owns its exact ``ExecutablePlugin``
        object and may still need its package or isolated environment.  The
        registry removal therefore disables discovery for every future Job but
        leaves managed bytes intact.  Re-intake can only reuse exactly matching
        bytes; no package is altered in place.
        """

        plugin_id = _checked_id(plugin_id)
        registry = self._read_registry()
        selected = [item for item in registry["plugins"] if item["plugin_id"] == plugin_id]
        if not selected:
            return False
        remaining = [item for item in registry["plugins"] if item["plugin_id"] != plugin_id]
        for item in selected:
            path = Path(str(item["package_path"])).resolve()
            try:
                path.relative_to(self.package_root)
            except ValueError as exc:
                raise PluginLifecycleError("Plugin registry package path escaped its root") from exc
            if not path.is_dir():
                raise PluginLifecycleError("Plugin managed package is unavailable")
        registry["plugins"] = remaining
        for key in tuple(registry["receipts"]):
            if key.startswith(f"{plugin_id}@"):
                del registry["receipts"][key]
        for key in tuple(registry["environments"]):
            if key.startswith(f"{plugin_id}@"):
                del registry["environments"][key]
        self._append_receipt(
            registry,
            action="WITHDRAWN_FUTURE_JOB",
            plugin_id=plugin_id,
            selected=tuple(selected),
        )
        self._write_registry(registry)
        return True

    def lifecycle_receipts(self, plugin_id: str | None = None, *, limit: int = 20) -> tuple[Mapping[str, object], ...]:
        """Inspect bounded, content-free lifecycle facts without loading a host."""

        try:
            return lifecycle_receipts(self._read_registry(), plugin_id=plugin_id, limit=limit)
        except ValueError as exc:
            raise PluginLifecycleError("Plugin lifecycle receipt request is invalid") from exc

    @staticmethod
    def _append_receipt(registry: dict[str, Any], *, action: str, plugin_id: str, selected: Sequence[Mapping[str, object]]) -> None:
        try:
            append_selected_plugin_receipt(registry, action=action, plugin_id=plugin_id, selected=selected)
        except (KeyError, ValueError) as exc:
            raise PluginLifecycleError("Plugin lifecycle receipt could not be recorded") from exc

    def _read_registry(self) -> dict[str, Any]:
        try:
            return read_plugin_registry(
                self.root, self.registry_path, schema=REGISTRY_SCHEMA
            )
        except ValueError as exc:
            raise PluginLifecycleError(str(exc)) from exc

    def _write_registry(self, value: Mapping[str, Any]) -> None:
        write_plugin_registry(self.root, self.registry_path, value)


def _read_source_manifest(source: Path) -> tuple[Mapping[str, Any], str]:
    manifest_path = source / "noruct-plugin.json"
    if not source.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise PluginLifecycleError("Plugin source requires a regular noruct-plugin.json manifest")
    _validate_source_tree(source)
    tree_digest = _package_tree_digest(source)
    files = [item for item in sorted(source.rglob("*")) if item.is_file() and ".git" not in item.relative_to(source).parts and "__pycache__" not in item.relative_to(source).parts]
    if not files or len(files) > _MAX_PACKAGE_FILES:
        raise PluginLifecycleError("Plugin package file count is outside the limit")
    total = 0
    for item in files:
        relative = item.relative_to(source).as_posix()
        raw = item.read_bytes(); total += len(raw)
        if total > _MAX_PACKAGE_BYTES:
            raise PluginLifecycleError("Plugin package exceeds the size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError("Plugin manifest must be valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise PluginLifecycleError("Plugin manifest must be an object")
    return manifest, tree_digest


def _package_tree_digest(source: Path) -> str:
    if not source.is_dir():
        raise PluginLifecycleError("Plugin package is not a directory")
    _validate_source_tree(source)
    files = [item for item in sorted(source.rglob("*")) if item.is_file() and ".git" not in item.relative_to(source).parts and "__pycache__" not in item.relative_to(source).parts]
    if not files or len(files) > _MAX_PACKAGE_FILES:
        raise PluginLifecycleError("Plugin package file count is outside the limit")
    digest = hashlib.sha256(); total = 0
    for item in files:
        relative = item.relative_to(source).as_posix(); raw = item.read_bytes(); total += len(raw)
        if total > _MAX_PACKAGE_BYTES:
            raise PluginLifecycleError("Plugin package exceeds the size limit")
        digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _validate_source_tree(source: Path) -> None:
    """Reject links before copytree could traverse outside the reviewed package."""
    if source.is_symlink():
        raise PluginLifecycleError("Plugin package may not be a symbolic link")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise PluginLifecycleError("Plugin package may not contain symbolic links")


def _plugin_from_manifest(
    manifest: Mapping[str, Any],
    package_path: Path,
    package_digest: str,
    *,
    enabled: bool,
    dependency_environment: Path | None = None,
) -> ExecutablePlugin:
    expected = {"schema", "plugin_id", "version", "description", "command", "environment", "timeout_seconds", "tools"}
    optional = {"dependency_lock"}
    if set(manifest) - expected - optional or not expected.issubset(manifest) or manifest.get("schema") != PLUGIN_SCHEMA or not isinstance(manifest.get("command"), list) or not isinstance(manifest.get("environment"), list) or not isinstance(manifest.get("tools"), list):
        raise PluginLifecycleError("Plugin manifest has unsupported fields")
    dependency_lock = manifest.get("dependency_lock")
    if dependency_lock is not None and dependency_lock != _DEPENDENCY_LOCK:
        raise PluginLifecycleError("Plugin dependency lock must be requirements.lock")
    dependency_lock_digest = None
    if dependency_lock is not None:
        lock_path = package_path / _DEPENDENCY_LOCK
        if lock_path.is_symlink() or not lock_path.is_file() or lock_path.stat().st_size > _MAX_LOCK_BYTES:
            raise PluginLifecycleError("Plugin dependency lock is invalid")
        dependency_lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    tools = tuple(ExecutablePluginTool(name=str(item.get("name", "")), description=str(item.get("description", "")), input_schema=item.get("input_schema", {})) for item in manifest["tools"] if isinstance(item, Mapping))
    if len(tools) != len(manifest["tools"]):
        raise PluginLifecycleError("Plugin tools must be objects")
    plugin = ExecutablePlugin(
        plugin_id=str(manifest["plugin_id"]), version=str(manifest["version"]), description=str(manifest["description"]),
        package_path=package_path, command=tuple(str(item) for item in manifest["command"]),
        environment_names=tuple(str(item) for item in manifest["environment"]), timeout_seconds=float(manifest["timeout_seconds"]),
        tools=tools, package_digest=package_digest, enabled=enabled, dependency_lock=dependency_lock,
        dependency_lock_digest=dependency_lock_digest, dependency_environment=dependency_environment,
    )
    plugin.validate(); return plugin


def _plugin_record(plugin: ExecutablePlugin) -> dict[str, Any]:
    return {"plugin_id": plugin.plugin_id, "version": plugin.version, "description": plugin.description, "package_path": str(plugin.package_path), "command": list(plugin.command), "environment": list(plugin.environment_names), "timeout_seconds": plugin.timeout_seconds, "tools": [asdict(item) for item in plugin.tools], "package_digest": plugin.package_digest, "enabled": plugin.enabled, "dependency_lock": plugin.dependency_lock, "dependency_lock_digest": plugin.dependency_lock_digest}


def _plugin_from_record(value: object, *, dependency_environment: Path | None = None) -> ExecutablePlugin:
    legacy_fields = {"plugin_id", "version", "description", "package_path", "command", "environment", "timeout_seconds", "tools", "package_digest", "enabled"}
    current_fields = legacy_fields | {"dependency_lock", "dependency_lock_digest"}
    if not isinstance(value, Mapping) or (set(value) != legacy_fields and set(value) != current_fields) or not isinstance(value["command"], list) or not isinstance(value["environment"], list) or not isinstance(value["tools"], list) or not isinstance(value["enabled"], bool):
        raise PluginLifecycleError("Plugin registry record is malformed")
    manifest = {"schema": PLUGIN_SCHEMA, "plugin_id": value["plugin_id"], "version": value["version"], "description": value["description"], "command": value["command"], "environment": value["environment"], "timeout_seconds": value["timeout_seconds"], "tools": value["tools"]}
    lock = value.get("dependency_lock")
    digest = value.get("dependency_lock_digest")
    if lock is not None or digest is not None:
        if lock != _DEPENDENCY_LOCK or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PluginLifecycleError("Plugin dependency lock registry metadata is malformed")
        manifest["dependency_lock"] = lock
    plugin = _plugin_from_manifest(manifest, Path(str(value["package_path"])), str(value["package_digest"]), enabled=value["enabled"], dependency_environment=dependency_environment)
    if digest is not None and plugin.dependency_lock_digest != digest:
        raise PluginLifecycleError("Plugin dependency lock registry receipt no longer matches its package")
    return plugin


def _environment_python(environment: Path) -> Path | None:
    """Return the venv entrypoint without resolving its normal Python symlink."""
    root = environment.expanduser().resolve()
    candidate = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _recorded_environment(registry: Mapping[str, Any], record: Mapping[str, Any], environment_root: Path) -> Path | None:
    lock_digest = record.get("dependency_lock_digest")
    if lock_digest is None:
        return None
    if not isinstance(lock_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", lock_digest):
        raise PluginLifecycleError("Plugin dependency lock registry metadata is malformed")
    plugin_id = record.get("plugin_id")
    version = record.get("version")
    if not isinstance(plugin_id, str) or not isinstance(version, str):
        raise PluginLifecycleError("Plugin registry record is malformed")
    environment = registry["environments"].get(f"{plugin_id}@{version}")
    if environment is None:
        return None
    if not isinstance(environment, Mapping) or set(environment) != {"lock_sha256", "python"} or environment.get("lock_sha256") != lock_digest or not isinstance(environment.get("python"), str):
        raise PluginLifecycleError("Plugin dependency environment receipt is malformed")
    root = (environment_root / plugin_id / version).resolve()
    python = Path(environment["python"]).expanduser()
    expected_python = _environment_python(root)
    if expected_python is None:
        return None
    if not python.is_absolute() or python != expected_python:
        raise PluginLifecycleError("Plugin dependency environment receipt does not match its managed location")
    try:
        root.relative_to(environment_root)
    except ValueError as exc:
        raise PluginLifecycleError("Plugin dependency environment escaped its managed root") from exc
    if not root.is_dir():
        return None
    return root


def _checked_id(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise PluginLifecycleError("Plugin id is invalid")
    return value


def _catalog_provenance(value: Mapping[str, str] | None) -> dict[str, str]:
    """Validate optional signed-catalog origin facts kept beside a Git receipt."""

    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) != {"catalog_id", "catalog_digest"}:
        raise PluginLifecycleError("Plugin catalog provenance is malformed")
    catalog_id = value.get("catalog_id")
    catalog_digest = value.get("catalog_digest")
    if not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id):
        raise PluginLifecycleError("Plugin catalog provenance id is invalid")
    if not isinstance(catalog_digest, str) or not _SHA256.fullmatch(catalog_digest):
        raise PluginLifecycleError("Plugin catalog provenance digest is invalid")
    return {"catalog_id": catalog_id, "catalog_digest": catalog_digest}


def _safe_source_receipt(value: object) -> Mapping[str, str]:
    """Parse the source receipt without exposing package files or credentials."""

    if not isinstance(value, Mapping) or not isinstance(value.get("kind"), str):
        raise PluginLifecycleError("Plugin source receipt is malformed")
    if value["kind"] == "local_directory":
        if set(value) != {"kind"}:
            raise PluginLifecycleError("Plugin local source receipt is malformed")
        return {"kind": "local_directory"}
    if value["kind"] != "git":
        raise PluginLifecycleError("Plugin source receipt kind is invalid")
    allowed = {"kind", "repository_url", "commit", "subdirectory", "catalog_id", "catalog_digest"}
    required = {"kind", "repository_url", "commit", "subdirectory"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise PluginLifecycleError("Plugin Git source receipt is malformed")
    repository_url = value.get("repository_url")
    commit = value.get("commit")
    subdirectory = value.get("subdirectory")
    if (
        not isinstance(repository_url, str)
        or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?/[A-Za-z0-9._~:/-]+(?:\.git)?", repository_url)
        or not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
        or not isinstance(subdirectory, str)
        or len(subdirectory.encode()) > 240
        or "\x00" in subdirectory
    ):
        raise PluginLifecycleError("Plugin Git source receipt is malformed")
    provenance = _catalog_provenance(
        {"catalog_id": str(value["catalog_id"]), "catalog_digest": str(value["catalog_digest"])}
        if "catalog_id" in value or "catalog_digest" in value
        else None
    )
    return {"kind": "git", "repository_url": repository_url, "commit": commit.lower(), "subdirectory": subdirectory, **provenance}


@dataclass(frozen=True, slots=True)
class PluginRuntimeConfig:
    root: Path
    plugins: tuple[ExecutablePlugin, ...]


def plugin_config_from_settings(settings: Mapping[str, Any]) -> PluginRuntimeConfig | None:
    raw = settings.get("plugins")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    if set(raw) != {"enabled", "root"} or not isinstance(raw.get("root"), str):
        raise PluginLifecycleError("Plugin runtime configuration is malformed")
    store = ExecutablePluginStore(Path(raw["root"]))
    return PluginRuntimeConfig(store.root, store.active())
