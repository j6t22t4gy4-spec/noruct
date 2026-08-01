"""Approval-gated adapter for a user-managed local ``cua-driver``.

The driver is an optional MIT executable installed and owned by the operator.
Noruct never installs it, passes provider credentials to it, or gives it a
general desktop authority.  Each Company Job receives a fresh connector with
one explicit application allowlist and each desktop read/mutation becomes an
ordinary first-party ToolIntent.

This is deliberately a small CLI adapter rather than the upstream persistent
MCP backend: Noruct's worker runtime is text-tool based and owns the Job,
approval, output-redaction and state boundaries.  The adapter uses only the
documented ``cua-driver call <tool> <json>`` surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolValidationError
from dynamic_firm._vendor.runtime_safety.redact import redact_terminal_output


_APP_RE = re.compile(r"[A-Za-z0-9._ -]{1,160}\Z")
_KEY_PART_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_BLOCKED_KEY_COMBOS = frozenset(
    {
        frozenset({"cmd", "shift", "backspace"}),
        frozenset({"cmd", "option", "backspace"}),
        frozenset({"cmd", "ctrl", "q"}),
        frozenset({"cmd", "shift", "q"}),
        frozenset({"win", "l"}),
        frozenset({"ctrl", "option", "delete"}),
        frozenset({"ctrl", "option", "del"}),
        frozenset({"option", "f4"}),
    }
)
_KEY_ALIASES = {
    "command": "cmd",
    "control": "ctrl",
    "alt": "option",
    "windows": "win",
    "super": "win",
    "meta": "win",
}
_MAX_TEXT_BYTES = 8_192
_MAX_RESULT_BYTES = 64_000


class ComputerUseCapabilityError(ValueError):
    """A bounded, non-sensitive failure from the user-managed driver."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ComputerUseConfig:
    """Non-secret operator policy for the optional desktop capability."""

    driver_command: Path
    allowed_apps: tuple[str, ...]
    timeout_seconds: float = 20.0
    max_result_bytes: int = 48_000
    allow_control: bool = False

    def validate(self) -> None:
        command = self.driver_command.expanduser().resolve(strict=False)
        if not command.is_absolute() or not command.is_file() or not os.access(command, os.X_OK):
            raise ValueError("Computer-use driver command must resolve to an absolute executable")
        if not 1 <= len(self.allowed_apps) <= 8:
            raise ValueError("Computer-use requires between one and eight allowed applications")
        normalized = tuple(item.strip() for item in self.allowed_apps)
        if len(set(item.lower() for item in normalized)) != len(normalized):
            raise ValueError("Computer-use allowed applications must be unique")
        if any(not _APP_RE.fullmatch(item) for item in normalized):
            raise ValueError("Computer-use allowed application names must be bounded identifiers")
        if not 1.0 <= self.timeout_seconds <= 45.0:
            raise ValueError("Computer-use timeout_seconds must be between 1 and 45")
        if not 4_096 <= self.max_result_bytes <= _MAX_RESULT_BYTES:
            raise ValueError(f"Computer-use max_result_bytes must be between 4096 and {_MAX_RESULT_BYTES}")
        if not isinstance(self.allow_control, bool):
            raise ValueError("Computer-use allow_control must be a boolean")


def computer_use_config_from_settings(settings: Mapping[str, Any]) -> ComputerUseConfig | None:
    raw = settings.get("computer_use")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    allowed = {"enabled", "driver_command", "allowed_apps", "timeout_seconds", "max_result_bytes", "allow_control"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown computer_use configuration field: {sorted(unknown)[0]}")
    command = raw.get("driver_command")
    apps = raw.get("allowed_apps")
    timeout = raw.get("timeout_seconds", 20.0)
    result_limit = raw.get("max_result_bytes", 48_000)
    allow_control = raw.get("allow_control", False)
    if (
        not isinstance(command, str)
        or not isinstance(apps, list)
        or not all(isinstance(item, str) for item in apps)
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not isinstance(result_limit, int)
        or isinstance(result_limit, bool)
        or not isinstance(allow_control, bool)
    ):
        raise ValueError("Computer-use configuration is malformed")
    config = ComputerUseConfig(
        driver_command=Path(command).expanduser(),
        allowed_apps=tuple(item.strip() for item in apps),
        timeout_seconds=float(timeout),
        max_result_bytes=result_limit,
        allow_control=allow_control,
    )
    config.validate()
    return config


def configured_driver_version(config: ComputerUseConfig) -> str | None:
    """Return a bounded version string without starting a desktop session."""

    config.validate()
    try:
        completed = subprocess.run(
            (str(config.driver_command.expanduser().resolve()), "--version"),
            cwd=tempfile.gettempdir(),
            env=_driver_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="ignore").strip()
    return value[:160] if value and "\x00" not in value else None


def _driver_environment() -> dict[str, str]:
    """Pass only OS/session routing variables, never provider credentials."""

    names = {"HOME", "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY"}
    environment = {key: value for key, value in os.environ.items() if key in names}
    # cua-driver documents this opt-out.  Noruct keeps external telemetry off
    # unless a future explicit first-party setting authorizes it.
    environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    return environment


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ComputerUseCapabilityError("INVALID_JSON", "Computer-use data is not valid JSON") from exc


def _safe_json_payload(raw: str) -> object:
    start = min((index for index in (raw.find("{"), raw.find("[")) if index >= 0), default=-1)
    if start < 0:
        raise ComputerUseCapabilityError("DRIVER_PROTOCOL", "Computer-use driver returned no JSON result")
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise ComputerUseCapabilityError("DRIVER_PROTOCOL", "Computer-use driver returned malformed JSON") from exc


def _bounded_json(value: object, limit: int) -> str:
    rendered = redact_terminal_output(_canonical_json(value).decode("utf-8"), force=True)
    data = rendered.encode("utf-8")
    if len(data) > limit:
        raise ComputerUseCapabilityError("RESULT_TOO_LARGE", "Computer-use result exceeds the configured byte limit")
    return rendered


@dataclass(slots=True)
class _Target:
    app: str
    pid: int
    window_id: int
    title: str = ""
    element_indices: set[int] = field(default_factory=set)


class ComputerUseConnector:
    """One Job-local, approval-gated CUA CLI projection.

    A capture establishes the target window and the valid element indexes for
    subsequent controls.  There is no general ``call_tool`` escape hatch.
    """

    def __init__(self, config: ComputerUseConfig) -> None:
        config.validate()
        self.config = config
        self._target: _Target | None = None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (self._definition(),)

    def _definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not isinstance(arguments, Mapping) or set(arguments) - {
                "action", "app", "element", "coordinate", "from_element", "to_element",
                "from_coordinate", "to_coordinate", "direction", "amount", "text", "keys",
                "value", "button", "capture_after", "seconds",
            }:
                raise ToolValidationError("computer_use received unsupported arguments")
            action = arguments.get("action")
            if action not in {"capture", "list_apps", "click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value", "wait"}:
                raise ToolValidationError("computer_use action is not supported")
            result = dict(arguments)
            if action not in {"capture", "list_apps", "wait"} and not self.config.allow_control:
                raise ToolValidationError("computer-use control is disabled by the operator")
            if action in {"capture", "list_apps"}:
                app = result.get("app")
                if action == "capture":
                    if not isinstance(app, str) or not self._allowed_app(app):
                        raise ToolValidationError("capture requires one configured allowed app")
                elif app is not None:
                    if not isinstance(app, str) or not self._allowed_app(app):
                        raise ToolValidationError("app must be one configured allowed app")
                return result
            if action == "wait":
                seconds = result.get("seconds", 1.0)
                if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= float(seconds) <= 15:
                    raise ToolValidationError("wait seconds must be between 0 and 15")
                return {"action": action, "seconds": float(seconds)}
            if action == "key":
                keys = result.get("keys")
                if not isinstance(keys, str) or not keys.strip() or len(keys.encode("utf-8")) > 128:
                    raise ToolValidationError("keys must be a bounded key combination")
                parts = frozenset(_KEY_ALIASES.get(part.strip().lower(), part.strip().lower()) for part in keys.split("+") if part.strip())
                if not parts or any(not _KEY_PART_RE.fullmatch(part) for part in parts) or parts in _BLOCKED_KEY_COMBOS:
                    raise ToolValidationError("keys contains an unsupported or blocked key combination")
            if self._target is None:
                raise ToolValidationError("capture one configured app before a desktop control action")
            if action in {"click", "double_click", "right_click", "middle_click"}:
                self._validate_element_or_coordinate(result, "element", "coordinate")
            elif action == "drag":
                self._validate_drag(result)
            elif action == "scroll":
                direction = result.get("direction")
                amount = result.get("amount", 3)
                if direction not in {"up", "down", "left", "right"} or not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 30:
                    raise ToolValidationError("scroll requires direction and amount between 1 and 30")
                if "element" in result:
                    self._validate_element(result["element"])
                elif "coordinate" in result:
                    self._validate_coordinate(result["coordinate"], "coordinate")
            elif action in {"type", "set_value"}:
                field_name = "text" if action == "type" else "value"
                text = result.get(field_name)
                if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
                    raise ToolValidationError(f"{field_name} must be a bounded text value")
                if action == "set_value":
                    self._validate_element(result.get("element"))
            capture_after = result.get("capture_after", False)
            if not isinstance(capture_after, bool):
                raise ToolValidationError("capture_after must be a boolean")
            return result

        async def handle(arguments: Mapping[str, Any], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            action = str(arguments["action"])
            if action == "wait":
                await asyncio.sleep(float(arguments["seconds"]))
                return _bounded_json({"source": "configured_local_computer", "action": "wait", "seconds": arguments["seconds"]}, self.config.max_result_bytes)
            if action == "list_apps":
                return await self._list_apps(arguments.get("app"))
            if action == "capture":
                return await self._capture(str(arguments["app"]))
            result = await self._control(action, arguments)
            if bool(arguments.get("capture_after", False)) and self._target is not None:
                capture = json.loads(await self._capture(self._target.app))
                result["post_action_snapshot"] = capture.get("result")
            return _bounded_json(result, self.config.max_result_bytes)

        return ToolDefinition(
            name="computer_use",
            description=(
                "Operate one explicitly configured local desktop application through a user-managed CUA driver. "
                "Call capture first, then use returned element indexes for click, drag, scroll, type, key, or set_value. "
                "Every desktop read or mutation requires explicit approval; screenshots are never included in model context."
            ),
            input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["capture", "list_apps", "click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value", "wait"]}}, "required": ["action"], "additionalProperties": True},
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda arguments: f"computer:local:{str(arguments.get('action', 'unknown'))}",
            handler=handle,
            timeout_ms=int((self.config.timeout_seconds + 5.0) * 1_000),
            output_limit_bytes=self.config.max_result_bytes,
            requires_approval=True,
            approval_preview=lambda arguments: self._approval_preview(arguments),
        )

    def _allowed_app(self, value: str) -> bool:
        return value.strip().lower() in {item.lower() for item in self.config.allowed_apps}

    def _validate_element(self, value: object) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ToolValidationError("element must be a positive capture element index")
        if self._target is None or value not in self._target.element_indices:
            raise ToolValidationError("element is not present in the current capture")

    @staticmethod
    def _validate_coordinate(value: object, name: str) -> None:
        if not isinstance(value, list) or len(value) != 2 or any(not isinstance(item, int) or isinstance(item, bool) or not -10_000 <= item <= 10_000 for item in value):
            raise ToolValidationError(f"{name} must be two bounded integer coordinates")

    def _validate_element_or_coordinate(self, arguments: Mapping[str, Any], element_name: str, coordinate_name: str) -> None:
        has_element = element_name in arguments
        has_coordinate = coordinate_name in arguments
        if has_element == has_coordinate:
            raise ToolValidationError(f"provide exactly one of {element_name} or {coordinate_name}")
        if has_element:
            self._validate_element(arguments[element_name])
        else:
            self._validate_coordinate(arguments[coordinate_name], coordinate_name)

    def _validate_drag(self, arguments: Mapping[str, Any]) -> None:
        elements = "from_element" in arguments or "to_element" in arguments
        coordinates = "from_coordinate" in arguments or "to_coordinate" in arguments
        if elements == coordinates or (elements and not ("from_element" in arguments and "to_element" in arguments)) or (coordinates and not ("from_coordinate" in arguments and "to_coordinate" in arguments)):
            raise ToolValidationError("drag requires both element indexes or both coordinate pairs")
        if elements:
            self._validate_element(arguments["from_element"])
            self._validate_element(arguments["to_element"])
        else:
            self._validate_coordinate(arguments["from_coordinate"], "from_coordinate")
            self._validate_coordinate(arguments["to_coordinate"], "to_coordinate")

    async def _list_apps(self, requested: object) -> str:
        payload = await self._call("list_apps", {})
        apps = payload.get("apps") if isinstance(payload, Mapping) else None
        if not isinstance(apps, list):
            apps = payload.get("data") if isinstance(payload, Mapping) else []
        allowed = {item.lower() for item in self.config.allowed_apps}
        filtered = [item for item in apps if isinstance(item, Mapping) and self._record_matches_allowed_app(item, allowed)] if isinstance(apps, list) else []
        if isinstance(requested, str):
            filtered = [item for item in filtered if self._record_matches_app(item, requested)]
        return _bounded_json({"source": "configured_local_computer", "allowed_apps": filtered[:8]}, self.config.max_result_bytes)

    async def _capture(self, app: str) -> str:
        target = await self._resolve_target(app)
        descriptor, screenshot_file = tempfile.mkstemp(prefix="noruct-cua-", suffix=".png")
        os.close(descriptor)
        try:
            payload = await self._call("get_window_state", {"pid": target.pid, "window_id": target.window_id, "screenshot_out_file": screenshot_file})
        finally:
            try:
                os.unlink(screenshot_file)
            except FileNotFoundError:
                pass
        result = self._snapshot_result(payload, target)
        self._target = _Target(app=target.app, pid=target.pid, window_id=target.window_id, title=target.title, element_indices=set(result["element_indexes"]))
        return _bounded_json({"source": "configured_local_computer", "trust": "private_local_desktop_evidence", "result": result}, self.config.max_result_bytes)

    async def _resolve_target(self, app: str) -> _Target:
        payload = await self._call("list_windows", {"on_screen_only": True})
        windows = payload.get("windows") if isinstance(payload, Mapping) else None
        if not isinstance(windows, list):
            data = payload.get("data") if isinstance(payload, Mapping) else None
            windows = data.get("windows") if isinstance(data, Mapping) else []
        for record in windows if isinstance(windows, list) else []:
            if not isinstance(record, Mapping) or not self._record_matches_app(record, app):
                continue
            pid = record.get("pid")
            window_id = record.get("window_id", record.get("id"))
            if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(window_id, int) and not isinstance(window_id, bool):
                label = str(record.get("app_name") or record.get("app") or record.get("bundle_id") or app)
                title = str(record.get("title") or record.get("window_title") or "")[:256]
                return _Target(app=label, pid=pid, window_id=window_id, title=title)
        raise ComputerUseCapabilityError("APP_NOT_AVAILABLE", "Configured desktop application has no available on-screen window")

    @staticmethod
    def _record_matches_app(record: Mapping[str, Any], app: str) -> bool:
        wanted = app.strip().lower()
        return any(str(record.get(field, "")).strip().lower() == wanted for field in ("app_name", "app", "bundle_id", "name"))

    @staticmethod
    def _record_matches_allowed_app(record: Mapping[str, Any], allowed: set[str]) -> bool:
        return any(str(record.get(field, "")).strip().lower() in allowed for field in ("app_name", "app", "bundle_id", "name"))

    def _snapshot_result(self, payload: object, target: _Target) -> dict[str, object]:
        source = payload if isinstance(payload, Mapping) else {}
        elements = source.get("elements")
        if not isinstance(elements, list):
            structured = source.get("structuredContent")
            elements = structured.get("elements") if isinstance(structured, Mapping) else []
        normalized_elements: list[dict[str, object]] = []
        for item in elements if isinstance(elements, list) else []:
            if not isinstance(item, Mapping):
                continue
            index = item.get("index", item.get("element_index"))
            if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= 10_000:
                continue
            normalized_elements.append({
                "index": index,
                "role": str(item.get("role", ""))[:80],
                "label": redact_terminal_output(str(item.get("label", item.get("name", "")))[:512], force=True),
                "bounds": item.get("bounds") if isinstance(item.get("bounds"), (list, tuple)) else None,
            })
            if len(normalized_elements) >= 128:
                break
        tree = source.get("tree_markdown")
        if not isinstance(tree, str):
            structured = source.get("structuredContent")
            tree = structured.get("tree_markdown") if isinstance(structured, Mapping) else source.get("data", "")
        if not isinstance(tree, str):
            tree = ""
        return {
            "app": target.app,
            "window_title": redact_terminal_output(target.title, force=True),
            "element_indexes": [item["index"] for item in normalized_elements],
            "elements": normalized_elements,
            "accessibility_tree": redact_terminal_output(tree[:32_000], force=True),
            "screenshot_available": bool(source.get("screenshot_png_b64") or source.get("screenshot_file_path")),
            "screenshot_in_model_context": False,
        }

    async def _control(self, action: str, arguments: Mapping[str, Any]) -> dict[str, object]:
        target = self._target
        if target is None:
            raise ComputerUseCapabilityError("NO_CAPTURE", "Capture a configured application before desktop control")
        if action in {"click", "double_click", "right_click", "middle_click"}:
            tool = "double_click" if action == "double_click" else "click"
            params: dict[str, object] = {"pid": target.pid, "window_id": target.window_id, "button": {"right_click": "right", "middle_click": "middle"}.get(action, "left")}
            if "element" in arguments:
                params["element_index"] = arguments["element"]
            else:
                params["x"], params["y"] = arguments["coordinate"]
        elif action == "drag":
            tool = "drag"; params = {"pid": target.pid, "window_id": target.window_id}
            if "from_element" in arguments:
                params["from_element"] = arguments["from_element"]; params["to_element"] = arguments["to_element"]
            else:
                params["from_x"], params["from_y"] = arguments["from_coordinate"]
                params["to_x"], params["to_y"] = arguments["to_coordinate"]
        elif action == "scroll":
            tool = "scroll"; params = {"pid": target.pid, "window_id": target.window_id, "direction": arguments["direction"], "amount": arguments.get("amount", 3)}
            if "element" in arguments: params["element_index"] = arguments["element"]
            if "coordinate" in arguments: params["x"], params["y"] = arguments["coordinate"]
        elif action == "type":
            tool = "type_text"; params = {"pid": target.pid, "text": arguments["text"]}
        elif action == "key":
            parts = [_KEY_ALIASES.get(part.strip().lower(), part.strip().lower()) for part in str(arguments["keys"]).split("+") if part.strip()]
            tool = "hotkey" if len(parts) > 1 else "press_key"; params = {"pid": target.pid, "keys": parts} if tool == "hotkey" else {"pid": target.pid, "key": parts[0]}
        elif action == "set_value":
            tool = "set_value"; params = {"pid": target.pid, "window_id": target.window_id, "element_index": arguments["element"], "value": arguments["value"]}
        else:
            raise ComputerUseCapabilityError("UNSUPPORTED_ACTION", "Computer-use action is not available")
        payload = await self._call(tool, params)
        if isinstance(payload, Mapping) and payload.get("isError") is True:
            raise ComputerUseCapabilityError("DRIVER_ACTION_FAILED", "Computer-use driver rejected the desktop action")
        return {"source": "configured_local_computer", "action": action, "app": target.app, "completed": True}

    async def _call(self, tool: str, arguments: Mapping[str, object]) -> Mapping[str, Any]:
        payload = _canonical_json(dict(arguments)).decode("utf-8")
        command = (str(self.config.driver_command.expanduser().resolve()), "call", tool, payload)
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=tempfile.gettempdir(),
                env=_driver_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComputerUseCapabilityError("DRIVER_TIMEOUT", "Computer-use driver did not respond in time") from exc
        except OSError as exc:
            raise ComputerUseCapabilityError("DRIVER_UNAVAILABLE", "Computer-use driver could not start") from exc
        text = completed.stdout.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise ComputerUseCapabilityError("DRIVER_FAILURE", "Computer-use driver rejected the request")
        parsed = _safe_json_payload(text)
        if not isinstance(parsed, Mapping):
            raise ComputerUseCapabilityError("DRIVER_PROTOCOL", "Computer-use driver returned an invalid result")
        return parsed

    def _approval_preview(self, arguments: Mapping[str, Any]) -> str:
        action = str(arguments.get("action", "desktop action"))
        app = str(arguments.get("app") or (self._target.app if self._target else "configured application"))
        return f"Computer-use {action} in configured local application {app}"
