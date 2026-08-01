"""First-party privacy boundary over the audited private redactor."""

from __future__ import annotations

import json
from typing import Any, Mapping

from dynamic_firm._vendor.runtime_safety import redact as _source_redact


def redact_prompt_text(value: str) -> str:
    """Remove credential-like text before prompt hashing, storage or delivery."""

    return _source_redact.redact_sensitive_text(value, force=True)


def redact_tool_output(
    tool_name: str,
    arguments: Mapping[str, object],
    output: str,
) -> str:
    """Apply the audited file/terminal/general profile for one Noruct tool."""

    if tool_name == "read_workspace_file":
        return _source_redact.redact_sensitive_text(output, force=True, file_read=True)
    if tool_name in {
        "run_workspace_command",
        "run_workspace_background_command",
        "list_workspace_processes",
        "inspect_workspace_process",
        "wait_workspace_process",
        "stop_workspace_process",
    }:
        command = arguments.get("command")
        return _source_redact.redact_terminal_output(
            output,
            command if isinstance(command, str) else "",
            force=True,
        )
    return _source_redact.redact_sensitive_text(output, force=True)


def redact_runtime_value(value: Any) -> Any:
    """Redact a JSON-compatible value without exposing private source types.

    Serializing the complete value lets the upstream body-key rules see the
    relationship between keys such as ``api_key`` and otherwise opaque values.
    A parse failure omits the payload instead of returning the unredacted input.
    """

    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        redacted = _source_redact.redact_sensitive_text(serialized, force=True)
        return json.loads(redacted)
    except (TypeError, ValueError):
        return {"_redacted_payload": "unavailable"}
