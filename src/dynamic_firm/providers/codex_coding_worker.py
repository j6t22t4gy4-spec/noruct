"""Shadow-workspace coding worker backed by the user-managed Codex CLI."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.coding.models import CodingWorkRequest, CodingWorkResult
from dynamic_firm.coding.ports import CodingWorkerError, CodingWorkerPort
from dynamic_firm.providers.wire_safety import sanitize_wire_payload
from dynamic_firm.runtime.models import Usage
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError
from dynamic_firm.runtime.redaction import redact_prompt_text

from .codex_exec import (
    CodexExecProvider,
    CodexExecProviderConfig,
    _OutputLimitExceeded,
    _SAFE_VALIDATION_DETAIL,
    _SAFE_VALIDATION_NAME,
    _codex_model_argument,
    _is_unsupported_model_error,
)

_CODING_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "acceptance_evidence": {"type": "array", "items": {"type": "string"}},
        "unresolved_issues": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "suggested_followups": {"type": "array", "items": {"type": "string"}},
        "verification_commands": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": [
        "summary",
        "acceptance_evidence",
        "unresolved_issues",
        "observations",
        "suggested_followups",
        "verification_commands",
    ],
    "additionalProperties": False,
}


class CodexExecCodingWorker(CodingWorkerPort):
    """Replaceable user-managed Codex surface with shadow-only write authority."""

    def __init__(
        self,
        config: CodexExecProviderConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.transport = CodexExecProvider(config, environ=environ)
        self.real_workspace = self.transport.workspace

    async def execute(
        self,
        request: CodingWorkRequest,
        cancellation: CancellationToken,
    ) -> CodingWorkResult:
        cancellation.raise_if_cancelled()
        shadow = request.workspace.expanduser().resolve()
        if (
            not shadow.is_dir()
            or shadow.name != "workspace"
            or not shadow.parent.name.startswith("noruct-shadow-")
            or shadow == self.real_workspace
            or shadow.is_relative_to(self.real_workspace)
        ):
            raise CodingWorkerError(
                "CODING_WORKSPACE_INVALID",
                "Codex coding authority was not bound to a disposable Noruct shadow workspace.",
                retryable=False,
            )
        prompt = self._prompt(request)
        encoded_prompt = prompt.encode("utf-8")
        if len(encoded_prompt) > self.transport.config.max_prompt_bytes:
            raise CodingWorkerError(
                "CODING_REQUEST_TOO_LARGE",
                "Codex coding request exceeded the configured prompt byte limit.",
                retryable=False,
            )

        with tempfile.TemporaryDirectory(prefix="noruct-codex-result-") as temporary:
            root = Path(temporary)
            schema_path = root / "output-schema.json"
            result_path = root / "final-result.json"
            try:
                schema_path.write_text(
                    json.dumps(_CODING_RESULT_SCHEMA, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                os.chmod(schema_path, 0o600)
            except OSError:
                raise CodingWorkerError(
                    "CODING_REQUEST_INVALID",
                    "Codex coding output schema could not be prepared.",
                    retryable=False,
                ) from None

            command = self._exec_command(
                shadow=shadow,
                schema_path=schema_path,
                result_path=result_path,
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=shadow,
                    env=self.transport._child_environment(self.transport._environ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
            except OSError:
                raise CodingWorkerError(
                    "CODING_TRANSPORT_ERROR",
                    "Codex coding process could not be started.",
                    retryable=True,
                ) from None
            try:
                returncode, stdout, stderr = await self.transport._communicate(
                    process,
                    encoded_prompt,
                    cancellation,
                )
            except _OutputLimitExceeded:
                raise CodingWorkerError(
                    "CODING_RESPONSE_TOO_LARGE",
                    "Codex coding process output exceeded the configured byte limit.",
                    retryable=False,
                ) from None
            except ModelProviderError as exc:
                raise CodingWorkerError(
                    exc.code.replace("MODEL_", "CODING_", 1),
                    exc.message_safe.replace("Codex execution", "Codex coding execution"),
                    retryable=exc.retryable,
                ) from None

            if returncode != 0:
                lowered = stderr.decode("utf-8", errors="replace").lower()
                if "not logged in" in lowered or "login" in lowered and "required" in lowered:
                    raise CodingWorkerError(
                        "CODING_AUTH_FAILED",
                        "Codex is not authenticated. Run `codex login` or sign in through the Codex IDE extension.",
                        retryable=False,
                    )
                if _is_unsupported_model_error(lowered):
                    raise CodingWorkerError(
                        "CODING_CONFIGURATION_INVALID",
                        "The configured Codex model is not supported by this authenticated Codex installation.",
                        retryable=False,
                    )
                raise CodingWorkerError(
                    "CODING_UPSTREAM_ERROR",
                    "Codex coding execution failed before returning a valid result.",
                    retryable=True,
                )
            try:
                value = self.transport._read_result(result_path)
                usage, request_id = self.transport._parse_events(stdout)
                return self._parse_result(value, usage, request_id)
            except ModelProviderError as exc:
                raise CodingWorkerError(
                    exc.code.replace("MODEL_", "CODING_", 1),
                    exc.message_safe,
                    retryable=exc.retryable,
                ) from None

    def _exec_command(
        self,
        *,
        shadow: Path,
        schema_path: Path,
        result_path: Path,
    ) -> list[str]:
        assert self.transport.executable is not None
        command = [
            self.transport.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--disable",
            "multi_agent",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
            "-c",
            'web_search="disabled"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "-c",
            'approval_policy="never"',
            "-C",
            str(shadow),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        if model := _codex_model_argument(self.transport.config.model):
            command.extend(("--model", model))
        command.append("-")
        return command

    @staticmethod
    def _prompt(request: CodingWorkRequest) -> str:
        feedback = CodexExecCodingWorker._validation_feedback(request)
        rules = [
            "The current directory is a disposable shadow copy, never the user's real workspace.",
            "Work only inside this shadow copy.",
            "Do not use web search, network access, connectors, MCP servers, plugins, skills, or subagents.",
            "Do not read credentials, authentication state, environment secrets, or paths outside the shadow.",
            "Do not delete files, create symbolic links, install dependencies, or perform destructive actions.",
            "Make only the smallest text-file changes required by the task.",
            "For an implementation task, make the requested change in the shadow rather than only describing a patch.",
            "Use the shadow terminal only for bounded local inspection or validation when it helps verify the change.",
            "Return at most three safe, local verification command suggestions in verification_commands. They are suggestions only: Noruct validates and asks the user again before replaying one in the real workspace.",
            "Validation feedback is a bounded Noruct observation; do not infer or choose a hidden validation command.",
        ]
        if feedback:
            rules.append(
                "This is the only recovery call; inspect the current shadow candidate and correct the reported expectation rather than only describing it."
            )
        rules.append("Return one final JSON object matching the supplied output schema.")
        payload = {
            "backend_contract": {
                "name": "noruct-codex-shadow-coding-v2",
                "rules": rules,
            },
            "task": {
                "task_id": request.task_id,
                "objective": request.objective,
                "required_capabilities": list(request.required_capabilities),
                "acceptance_criteria": list(request.acceptance_criteria),
                "dependency_context": list(request.dependency_context),
                "task_context": list(request.task_context),
            },
        }
        if feedback:
            payload["task"]["validation_feedback"] = feedback
        sanitize_wire_payload(payload)
        return (
            "You are a user-managed external coding worker invoked by Noruct.\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _validation_feedback(request: CodingWorkRequest) -> list[dict[str, str]]:
        if len(request.validation_feedback) > 1:
            raise CodingWorkerError(
                "CODING_VALIDATION_FEEDBACK_INVALID",
                "Codex coding recovery received too many validation records.",
                retryable=False,
            )
        feedback: list[dict[str, str]] = []
        for attempt in request.validation_feedback:
            if (
                type(attempt.passed) is not bool
                or attempt.passed
                or not isinstance(attempt.name, str)
                or not isinstance(attempt.detail, str)
            ):
                raise CodingWorkerError(
                    "CODING_VALIDATION_FEEDBACK_INVALID",
                    "Codex coding recovery received an invalid validation record.",
                    retryable=False,
                )
            name = attempt.name.strip()
            detail = " ".join(redact_prompt_text(attempt.detail).split())
            if (
                _SAFE_VALIDATION_NAME.fullmatch(name) is None
                or _SAFE_VALIDATION_DETAIL.fullmatch(detail) is None
            ):
                raise CodingWorkerError(
                    "CODING_VALIDATION_FEEDBACK_INVALID",
                    "Codex coding recovery received unsafe validation feedback.",
                    retryable=False,
                )
            feedback.append({"check": name, "detail": detail})
        return feedback

    @staticmethod
    def _parse_result(
        value: Mapping[str, Any],
        usage: Usage,
        request_id: str | None,
    ) -> CodingWorkResult:
        summary = value.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex coding result did not contain a summary.",
                retryable=False,
            )

        def string_tuple(name: str, *, maximum: int | None = None) -> tuple[str, ...]:
            raw = value.get(name)
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ModelProviderError(
                    "MODEL_STRUCTURED_OUTPUT_INVALID",
                    "Codex coding result did not match the required schema.",
                    retryable=False,
                )
            if maximum is not None and len(raw) > maximum:
                raise ModelProviderError(
                    "MODEL_STRUCTURED_OUTPUT_INVALID",
                    "Codex coding result exceeded the allowed verification command count.",
                    retryable=False,
                )
            return tuple(item.strip() for item in raw if item.strip())

        return CodingWorkResult(
            summary=summary.strip(),
            acceptance_evidence=string_tuple("acceptance_evidence"),
            unresolved_issues=string_tuple("unresolved_issues"),
            observations=string_tuple("observations"),
            suggested_followups=string_tuple("suggested_followups"),
            verification_commands=string_tuple("verification_commands", maximum=3),
            usage=usage,
            provider_request_id=request_id,
        )
