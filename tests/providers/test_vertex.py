from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dynamic_firm.providers.vertex import VertexProvider, VertexProviderConfig
from dynamic_firm.runtime.ports import ModelProviderError


_BASE = "https://us-central1-aiplatform.googleapis.com/v1/projects/project_1/locations/us-central1/endpoints/openapi"


class VertexProviderTests(unittest.TestCase):
    def test_uses_user_managed_gcloud_adc_token_without_environment_storage(self) -> None:
        with patch("dynamic_firm.providers.vertex.shutil.which", return_value="/usr/local/bin/gcloud"), patch(
            "dynamic_firm.providers.vertex.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="a" * 32 + "\n"),
        ) as command:
            provider = VertexProvider(VertexProviderConfig(_BASE, "google/gemini-2.5-flash"))
            self.assertEqual(provider.secret_resolver.resolve("NORUCT_VERTEX_EPHEMERAL_ACCESS_TOKEN"), "a" * 32)
        self.assertEqual(command.call_args.args[0][1:], ("auth", "application-default", "print-access-token"))

    def test_rejects_non_vertex_endpoint_and_unavailable_adc(self) -> None:
        with self.assertRaisesRegex(ValueError, "Vertex base URL"):
            VertexProvider(VertexProviderConfig("https://example.com/v1", "model"))
        with patch("dynamic_firm.providers.vertex.shutil.which", return_value=None):
            provider = VertexProvider(VertexProviderConfig(_BASE, "model"))
            with self.assertRaises(ModelProviderError):
                provider.secret_resolver.resolve("NORUCT_VERTEX_EPHEMERAL_ACCESS_TOKEN")
