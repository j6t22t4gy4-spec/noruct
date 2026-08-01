"""Approval-gated OpenAI media tools with workspace-only artifacts.

This deliberately uses the public HTTP API rather than importing a provider SDK.
The Company owns tool approval, job budgets and artifact paths; this connector
only performs one explicitly configured media operation at a time.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolValidationError
from dynamic_firm.runtime.tools import validate_workspace_mutation_path


_ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?\Z")
_VOICE_RE = re.compile(r"[a-z0-9_-]{1,64}\Z")
_SIZE_RE = re.compile(r"(?:1024x1024|1536x1024|1024x1536)\Z")
_OUTPUT_EXTENSIONS = {
    "image": frozenset({".png", ".webp", ".jpg", ".jpeg"}),
    "speech": frozenset({".mp3", ".wav", ".opus", ".aac", ".flac"}),
    "video": frozenset({".mp4"}),
}
_MAX_TEXT_BYTES = 16_000
_MAX_INPUT_BYTES = 24 * 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class MediaCapabilityError(ValueError):
    """A safe configuration or remote-service failure."""


@dataclass(frozen=True, slots=True)
class OpenAIMediaConfig:
    """Explicit non-secret policy for direct media endpoints.

    Feature switches are intentionally separate.  Enabling transcription does
    not give an employee image, voice or video generation authority.
    """

    api_key_env: str = "OPENAI_API_KEY"
    image_enabled: bool = False
    speech_enabled: bool = False
    transcription_enabled: bool = False
    video_enabled: bool = False
    image_model: str = "gpt-image-2"
    speech_model: str = "gpt-4o-mini-tts"
    transcription_model: str = "gpt-4o-mini-transcribe"
    video_model: str = "sora-2"
    timeout_seconds: float = 90.0
    video_timeout_seconds: float = 600.0

    def validate(self) -> None:
        if not _ENV_RE.fullmatch(self.api_key_env):
            raise ValueError("OpenAI media credential must be an environment variable name")
        if not any((self.image_enabled, self.speech_enabled, self.transcription_enabled, self.video_enabled)):
            raise ValueError("Enable at least one OpenAI media capability")
        for name, value in (
            ("image_model", self.image_model),
            ("speech_model", self.speech_model),
            ("transcription_model", self.transcription_model),
            ("video_model", self.video_model),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", value):
                raise ValueError(f"{name} must be a bounded model identifier")
        if not 5 <= self.timeout_seconds <= 180:
            raise ValueError("OpenAI media timeout_seconds must be between 5 and 180")
        if not 30 <= self.video_timeout_seconds <= 900:
            raise ValueError("OpenAI media video_timeout_seconds must be between 30 and 900")

    @property
    def enabled_capabilities(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, enabled in (
                ("image", self.image_enabled),
                ("speech", self.speech_enabled),
                ("transcription", self.transcription_enabled),
                ("video", self.video_enabled),
            )
            if enabled
        )


def media_config_from_settings(settings: Mapping[str, Any]) -> OpenAIMediaConfig | None:
    raw = settings.get("openai_media")
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    allowed = {
        "enabled", "api_key_env", "image_enabled", "speech_enabled", "transcription_enabled",
        "video_enabled", "image_model", "speech_model", "transcription_model", "video_model",
        "timeout_seconds", "video_timeout_seconds",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown openai_media configuration field: {sorted(unknown)[0]}")
    values: dict[str, Any] = {}
    for name, default in (
        ("api_key_env", "OPENAI_API_KEY"), ("image_enabled", False), ("speech_enabled", False),
        ("transcription_enabled", False), ("video_enabled", False), ("image_model", "gpt-image-2"),
        ("speech_model", "gpt-4o-mini-tts"), ("transcription_model", "gpt-4o-mini-transcribe"),
        ("video_model", "sora-2"), ("timeout_seconds", 90.0), ("video_timeout_seconds", 600.0),
    ):
        values[name] = raw.get(name, default)
    if not isinstance(values["api_key_env"], str) or any(
        not isinstance(values[name], bool)
        for name in ("image_enabled", "speech_enabled", "transcription_enabled", "video_enabled")
    ) or any(
        not isinstance(values[name], str)
        for name in ("image_model", "speech_model", "transcription_model", "video_model")
    ) or any(
        not isinstance(values[name], (int, float)) or isinstance(values[name], bool)
        for name in ("timeout_seconds", "video_timeout_seconds")
    ):
        raise ValueError("OpenAI media configuration is malformed")
    config = OpenAIMediaConfig(
        api_key_env=values["api_key_env"], image_enabled=values["image_enabled"],
        speech_enabled=values["speech_enabled"], transcription_enabled=values["transcription_enabled"],
        video_enabled=values["video_enabled"], image_model=values["image_model"],
        speech_model=values["speech_model"], transcription_model=values["transcription_model"],
        video_model=values["video_model"], timeout_seconds=float(values["timeout_seconds"]),
        video_timeout_seconds=float(values["video_timeout_seconds"]),
    )
    config.validate()
    return config


class OpenAIMediaConnector:
    """Expose only selected media operations as named Company tools."""

    def __init__(self, config: OpenAIMediaConfig, workspace: Path, *, workspace_id: str) -> None:
        config.validate()
        self.config = config
        self.workspace = workspace.resolve()
        self.workspace_id = workspace_id

    def definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        if self.config.image_enabled:
            definitions.append(self._image_definition())
        if self.config.speech_enabled:
            definitions.append(self._speech_definition())
        if self.config.transcription_enabled:
            definitions.append(self._transcription_definition())
        if self.config.video_enabled:
            definitions.append(self._video_definition())
        return tuple(definitions)

    def _output_path(self, value: object, kind: str) -> tuple[str, Path]:
        if not isinstance(value, str):
            raise ToolValidationError("output_path must be a workspace-relative path")
        path = validate_workspace_mutation_path(value)
        if path.suffix.lower() not in _OUTPUT_EXTENSIONS[kind]:
            raise ToolValidationError(f"output_path must use an allowed {kind} extension")
        resolved = (self.workspace / path).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolValidationError("output_path escapes the workspace") from exc
        if resolved.exists():
            raise ToolValidationError("output_path already exists; choose a new artifact path")
        return path.as_posix(), resolved

    def _input_path(self, value: object) -> tuple[str, Path]:
        if not isinstance(value, str):
            raise ToolValidationError("input_path must be a workspace-relative path")
        raw = PurePosixPath(value)
        if raw.is_absolute() or ".." in raw.parts or not value:
            raise ToolValidationError("input_path must stay inside the workspace")
        resolved = (self.workspace / raw).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolValidationError("input_path escapes the workspace") from exc
        if not resolved.is_file() or resolved.is_symlink() or resolved.stat().st_size > _MAX_INPUT_BYTES:
            raise ToolValidationError("input_path must be a regular media file within the size limit")
        return raw.as_posix(), resolved

    @staticmethod
    def _text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ToolValidationError(f"{name} must be non-empty and within the text limit")
        return value.strip()

    def _definition(self, *, name: str, description: str, schema: Mapping[str, Any], validator, handler, resource_key) -> ToolDefinition:
        return ToolDefinition(
            name=name, description=description, input_schema=schema,
            effect=ToolEffect.NETWORK, risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.NONE, validator=validator, handler=handler,
            resource_key=resource_key, timeout_ms=int(self.config.video_timeout_seconds * 1000 if name == "generate_video" else self.config.timeout_seconds * 1000),
            output_limit_bytes=12_000, requires_approval=True, allow_session_approval=False,
            parallel_safe=False,
        )

    def _image_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"prompt", "output_path", "size"}) or not {"prompt", "output_path"}.issubset(arguments):
                raise ToolValidationError("generate_image requires prompt and output_path")
            output, _ = self._output_path(arguments["output_path"], "image")
            size = str(arguments.get("size", "1024x1024"))
            if not _SIZE_RE.fullmatch(size):
                raise ToolValidationError("size must be one of 1024x1024, 1536x1024, or 1024x1536")
            return {"prompt": self._text(arguments["prompt"], "prompt"), "output_path": output, "size": size}
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled(); output, target = self._output_path(arguments["output_path"], "image")
            data = await asyncio.to_thread(self._json_request, "/images/generations", {"model": self.config.image_model, "prompt": arguments["prompt"], "size": arguments["size"], "response_format": "b64_json"})
            try: raw = base64.b64decode(data["data"][0]["b64_json"], validate=True)
            except (KeyError, IndexError, TypeError, ValueError) as exc: raise MediaCapabilityError("Image service returned no artifact") from exc
            self._write_artifact(target, raw); cancellation.raise_if_cancelled()
            return json.dumps({"artifact": output, "kind": "image", "model": self.config.image_model}, sort_keys=True)
        return self._definition(name="generate_image", description="Generate one image into a new approved workspace artifact path.", schema={"type":"object","properties":{"prompt":{"type":"string"},"output_path":{"type":"string"},"size":{"type":"string","enum":["1024x1024","1536x1024","1024x1536"]}},"required":["prompt","output_path"],"additionalProperties":False}, validator=validate, handler=handle, resource_key=lambda a: f"workspace:{self.workspace_id}:media:image:{a['output_path']}")

    def _speech_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"text", "output_path", "voice", "format"}) or not {"text", "output_path"}.issubset(arguments):
                raise ToolValidationError("synthesize_speech requires text and output_path")
            output, _ = self._output_path(arguments["output_path"], "speech")
            voice = str(arguments.get("voice", "alloy")); fmt = str(arguments.get("format", "mp3"))
            if not _VOICE_RE.fullmatch(voice) or fmt not in {"mp3", "wav", "opus", "aac", "flac"}:
                raise ToolValidationError("voice or format is invalid")
            if Path(output).suffix.lower() != f".{fmt}": raise ToolValidationError("output_path extension must match format")
            return {"text": self._text(arguments["text"], "text"), "output_path": output, "voice": voice, "format": fmt}
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled(); output, target = self._output_path(arguments["output_path"], "speech")
            raw = await asyncio.to_thread(self._bytes_request, "/audio/speech", {"model": self.config.speech_model, "input": arguments["text"], "voice": arguments["voice"], "response_format": arguments["format"]})
            self._write_artifact(target, raw); cancellation.raise_if_cancelled()
            return json.dumps({"artifact": output, "kind": "speech", "model": self.config.speech_model}, sort_keys=True)
        return self._definition(name="synthesize_speech", description="Synthesize text into one new approved workspace audio artifact.", schema={"type":"object","properties":{"text":{"type":"string"},"output_path":{"type":"string"},"voice":{"type":"string"},"format":{"type":"string","enum":["mp3","wav","opus","aac","flac"]}},"required":["text","output_path"],"additionalProperties":False}, validator=validate, handler=handle, resource_key=lambda a: f"workspace:{self.workspace_id}:media:speech:{a['output_path']}")

    def _transcription_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"input_path", "language"}) or "input_path" not in arguments: raise ToolValidationError("transcribe_audio requires input_path")
            input_path, _ = self._input_path(arguments["input_path"]); language = arguments.get("language")
            if language is not None and (not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language)): raise ToolValidationError("language must be a short BCP-47 tag")
            return {"input_path": input_path, "language": language}
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled(); _, source = self._input_path(arguments["input_path"])
            data = await asyncio.to_thread(self._multipart_request, "/audio/transcriptions", {"model": self.config.transcription_model, **({"language": arguments["language"]} if arguments.get("language") else {})}, source)
            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(text, str): raise MediaCapabilityError("Transcription service returned no text")
            # Tool output is intentionally much smaller than the audio input
            # limit so the ordinary runtime output ledger remains bounded.
            return json.dumps({"kind":"transcription","model":self.config.transcription_model,"text":text[:8_000]}, ensure_ascii=False, sort_keys=True)
        return self._definition(name="transcribe_audio", description="Transcribe one approved workspace audio file; the audio is sent to the configured external media service.", schema={"type":"object","properties":{"input_path":{"type":"string"},"language":{"type":"string"}},"required":["input_path"],"additionalProperties":False}, validator=validate, handler=handle, resource_key=lambda a: f"workspace:{self.workspace_id}:media:transcription:{a['input_path']}")

    def _video_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"prompt", "output_path"}) or not {"prompt", "output_path"}.issubset(arguments): raise ToolValidationError("generate_video requires prompt and output_path")
            output, _ = self._output_path(arguments["output_path"], "video")
            return {"prompt":self._text(arguments["prompt"], "prompt"), "output_path":output}
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled(); output, target = self._output_path(arguments["output_path"], "video")
            created = await asyncio.to_thread(self._json_request, "/videos", {"model":self.config.video_model,"prompt":arguments["prompt"]})
            identifier = created.get("id") if isinstance(created, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier): raise MediaCapabilityError("Video service returned an invalid job receipt")
            raw = await asyncio.to_thread(self._wait_for_video, identifier)
            self._write_artifact(target, raw); cancellation.raise_if_cancelled()
            return json.dumps({"artifact":output,"kind":"video","model":self.config.video_model,"receipt":identifier[:32]}, sort_keys=True)
        return self._definition(name="generate_video", description="Generate one video into a new approved workspace artifact path; waits only within the configured job limit.", schema={"type":"object","properties":{"prompt":{"type":"string"},"output_path":{"type":"string"}},"required":["prompt","output_path"],"additionalProperties":False}, validator=validate, handler=handle, resource_key=lambda a: f"workspace:{self.workspace_id}:media:video:{a['output_path']}")

    def _credential(self) -> str:
        value = os.environ.get(self.config.api_key_env)
        if not value: raise MediaCapabilityError(f"Media credential environment variable is not set: {self.config.api_key_env}")
        return value

    def _request(self, path: str, *, body: bytes | None = None, content_type: str | None = None, timeout: float | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self._credential()}", "User-Agent":"Noruct-media/1"}
        if content_type: headers["Content-Type"] = content_type
        request = urllib.request.Request(_DEFAULT_BASE_URL + path, data=body, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.timeout_seconds) as response:
                raw = response.read(_MAX_OUTPUT_BYTES + 1)
        except urllib.error.HTTPError as exc: raise MediaCapabilityError(f"Media service request failed ({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc: raise MediaCapabilityError("Media service request could not be completed") from exc
        if len(raw) > _MAX_OUTPUT_BYTES: raise MediaCapabilityError("Media service response exceeded the artifact limit")
        return raw

    def _json_request(self, path: str, payload: Mapping[str, object]) -> dict[str, Any]:
        raw = self._request(path, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type="application/json")
        try: value = json.loads(raw)
        except json.JSONDecodeError as exc: raise MediaCapabilityError("Media service returned an invalid response") from exc
        if not isinstance(value, dict): raise MediaCapabilityError("Media service returned an invalid response")
        return value

    def _bytes_request(self, path: str, payload: Mapping[str, object]) -> bytes:
        return self._request(path, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type="application/json")

    def _multipart_request(self, path: str, fields: Mapping[str, object], source: Path) -> dict[str, Any]:
        boundary = "----noruct" + secrets.token_hex(16); parts: list[bytes] = []
        for name, value in fields.items():
            parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), str(value).encode(), b"\r\n"))
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="file"; filename="{source.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(), source.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode()))
        raw = self._request(path, body=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}")
        try: value = json.loads(raw)
        except json.JSONDecodeError as exc: raise MediaCapabilityError("Media service returned an invalid response") from exc
        if not isinstance(value, dict): raise MediaCapabilityError("Media service returned an invalid response")
        return value

    def _wait_for_video(self, identifier: str) -> bytes:
        import time
        deadline = time.monotonic() + self.config.video_timeout_seconds
        while time.monotonic() < deadline:
            raw = self._request(f"/videos/{identifier}")
            try: status = json.loads(raw).get("status")
            except (json.JSONDecodeError, AttributeError) as exc: raise MediaCapabilityError("Video service returned an invalid status") from exc
            if status == "completed": return self._request(f"/videos/{identifier}/content", timeout=self.config.video_timeout_seconds)
            if status in {"failed", "cancelled", "expired"}: raise MediaCapabilityError("Video service did not complete")
            time.sleep(2.0)
        raise MediaCapabilityError("Video generation did not complete before the configured limit")

    @staticmethod
    def _write_artifact(target: Path, raw: bytes) -> None:
        if not raw or len(raw) > _MAX_OUTPUT_BYTES: raise MediaCapabilityError("Generated artifact is empty or exceeds the size limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists(): raise MediaCapabilityError("Artifact path was created while the request was running")
        target.write_bytes(raw)
