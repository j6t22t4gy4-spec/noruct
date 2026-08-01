"""User-managed Google ADC transport for Vertex AI's OpenAI-compatible API."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

from dynamic_firm.providers.openai_compat import OpenAICompatProvider, OpenAICompatProviderConfig
from dynamic_firm.runtime.ports import ModelProviderError


_TOKEN_NAME = "NORUCT_VERTEX_EPHEMERAL_ACCESS_TOKEN"
_TOKEN = re.compile(r"^[A-Za-z0-9._-]{20,8192}$")


@dataclass(frozen=True, slots=True)
class VertexProviderConfig:
    base_url: str
    model: str
    timeout_seconds: float = 30.0
    gcloud_command: str = "gcloud"


class _VertexAdcResolver:
    def __init__(self, command: str, timeout_seconds: float) -> None:
        self._command = command
        self._timeout_seconds = timeout_seconds

    def resolve(self, name: str) -> str:
        if name != _TOKEN_NAME:
            raise ModelProviderError("MODEL_SECRET_SCOPE_MISSING", "Vertex token resolver was called outside its provider scope.", retryable=False)
        executable = shutil.which(self._command)
        if executable is None:
            raise ModelProviderError("VERTEX_GCLOUD_MISSING", "Vertex requires the user-managed gcloud executable with Application Default Credentials.", retryable=False)
        try:
            result = subprocess.run(
                (executable, "auth", "application-default", "print-access-token"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=min(max(self._timeout_seconds, 1.0), 30.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ModelProviderError("VERTEX_ADC_UNAVAILABLE", "Vertex Application Default Credentials could not provide an access token.", retryable=True) from exc
        token = result.stdout.strip()
        if result.returncode != 0 or not _TOKEN.fullmatch(token):
            raise ModelProviderError("VERTEX_ADC_UNAVAILABLE", "Vertex Application Default Credentials are unavailable or invalid.", retryable=False)
        return token


class VertexProvider(OpenAICompatProvider):
    """Refresh a user-managed short-lived ADC token for each provider call."""

    def __init__(self, config: VertexProviderConfig) -> None:
        parsed = urlsplit(config.base_url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("-aiplatform.googleapis.com"):
            raise ValueError("Vertex base URL must use an HTTPS regional aiplatform.googleapis.com endpoint")
        if not re.fullmatch(r"/v1/projects/[A-Za-z0-9_-]+/locations/[A-Za-z0-9_-]+/endpoints/openapi/?", parsed.path):
            raise ValueError("Vertex base URL must be the documented OpenAI-compatible project/location endpoint")
        if not config.model.strip():
            raise ValueError("Vertex model must be non-empty")
        super().__init__(
            OpenAICompatProviderConfig(
                base_url=config.base_url,
                model=config.model,
                api_key_env=_TOKEN_NAME,
                timeout_seconds=config.timeout_seconds,
                stream_responses=True,
                stream_include_usage=False,
            ),
            secret_resolver=_VertexAdcResolver(config.gcloud_command, config.timeout_seconds),
        )

