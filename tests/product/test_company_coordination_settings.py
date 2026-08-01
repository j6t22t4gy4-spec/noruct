from __future__ import annotations

import os
import json
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from dynamic_firm.application.modern_terminal_settings import (
    execute_runtime_settings_command,
)
from dynamic_firm.product.company_coordination_settings import (
    CompanyCoordinationSettings,
    write_company_coordination_settings,
)
from dynamic_firm.product.company_coordination_settings import (
    company_coordination_config_from_settings,
    company_coordination_enrollment_preview,
    company_coordination_preflight,
)
from dynamic_firm.cli import main
from dynamic_firm.product.settings_dashboard import panel_options
from dynamic_firm.product.settings_registry import SettingsRegistry


_SCOPE = "a" * 64


class _Ports:
    @staticmethod
    def load_config(path: Path) -> dict[str, object]:
        return tomllib.loads(path.read_text(encoding="utf-8"))


class _Owner:
    def __init__(self, config_path: Path) -> None:
        self.config = SimpleNamespace(config_path=config_path)
        self.ports = _Ports()
        self.settings: dict[str, object] = {}


class CompanyCoordinationSettingsTests(unittest.TestCase):
    def test_enabled_config_is_atomic_secret_free_and_preserves_other_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"NORUCT_TEST_COORD_TOKEN": "test-token"}
        ):
            path = Path(directory) / "config.toml"
            path.write_text('[provider]\nkind = "openai_codex"\ncodex_command = "codex"\n', encoding="utf-8")

            write_company_coordination_settings(
                path,
                CompanyCoordinationSettings(
                    enabled=True,
                    endpoint="https://coordination.example.test",
                    company_scope_digest=_SCOPE,
                    device_id="device-test-laptop",
                    token_env="NORUCT_TEST_COORD_TOKEN",
                ),
            )

            settings = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(settings["provider"]["kind"], "openai_codex")
            self.assertNotIn("test-token", path.read_text(encoding="utf-8"))
            config = company_coordination_config_from_settings(settings)
            assert config is not None
            self.assertEqual(config.device_id, "device-test-laptop")
            entry = {item.key: item for item in SettingsRegistry(path).entries()}[
                "company.coordination"
            ]
            self.assertEqual(entry.state, "ready")
            self.assertEqual(entry.value, "device-test-laptop")

    def test_disabled_profile_never_requires_or_reads_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            write_company_coordination_settings(path, CompanyCoordinationSettings(enabled=False))
            settings = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(company_coordination_config_from_settings(settings))
            entry = {item.key: item for item in SettingsRegistry(path).entries()}[
                "company.coordination"
            ]
            self.assertEqual(entry.state, "disabled")

    def test_settings_command_requires_complete_non_secret_metadata_then_updates_future_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"NORUCT_TEST_COORD_TOKEN": "test-token"}
        ):
            path = Path(directory) / "config.toml"
            owner = _Owner(path)
            malformed = execute_runtime_settings_command(
                owner,
                "/company-coordination",
                '{"enabled":true}',
            )
            assert malformed is not None
            self.assertIn("needs endpoint", malformed.messages[0])

            payload = {
                "enabled": True,
                "endpoint": "https://coordination.example.test",
                "company_scope_digest": _SCOPE,
                "device_id": "device-test-laptop",
                "token_env": "NORUCT_TEST_COORD_TOKEN",
                "allow_insecure_loopback": False,
            }
            result = execute_runtime_settings_command(
                owner,
                "/company-coordination",
                json.dumps(payload),
            )
            assert result is not None
            self.assertIn("saved", result.messages[0])
            self.assertIn("company_coordination", owner.settings)

            disabled = execute_runtime_settings_command(
                owner, "/company-coordination", '{"enabled":false}'
            )
            assert disabled is not None
            self.assertIn("disabled", disabled.messages[0])
            self.assertIsNone(company_coordination_config_from_settings(owner.settings))

    def test_company_settings_catalog_exposes_coordination_panel(self) -> None:
        self.assertIn(
            "coordination", {item.key for item in panel_options("Company")}
        )

    def test_enrollment_preview_derives_device_bound_hash_without_exposing_token(self) -> None:
        token = "local-device-token"
        with patch.dict(os.environ, {"NORUCT_TEST_COORD_TOKEN": token}):
            preview = company_coordination_enrollment_preview(
                CompanyCoordinationSettings(
                    enabled=True,
                    endpoint="https://coordination.example.test",
                    company_scope_digest=_SCOPE,
                    device_id="device-test-laptop",
                    token_env="NORUCT_TEST_COORD_TOKEN",
                )
            )
        rendered = json.dumps(preview, sort_keys=True)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.assertNotIn(token, rendered)
        self.assertFalse(preview["network_requested"])
        self.assertFalse(preview["server_mutated"])
        self.assertEqual(
            preview["worker_allowlist_entry"],
            {digest: {"scopes": [_SCOPE], "devices": ["device-test-laptop"]}},
        )

    def test_cli_enrollment_preview_reads_enabled_profile_without_writing_or_requesting(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"NORUCT_TEST_COORD_TOKEN": "local-device-token"}
        ):
            path = Path(directory) / "config.toml"
            write_company_coordination_settings(
                path,
                CompanyCoordinationSettings(
                    enabled=True,
                    endpoint="https://coordination.example.test",
                    company_scope_digest=_SCOPE,
                    device_id="device-test-laptop",
                    token_env="NORUCT_TEST_COORD_TOKEN",
                ),
            )
            output = io.StringIO()
            self.assertEqual(
                main(
                    [
                        "--config", str(path), "company",
                        "coordination-enrollment-preview", "--json",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
                0,
            )
        self.assertNotIn("local-device-token", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["server_mutated"])

    def test_preflight_delegates_only_to_no_mutation_identity_client(self) -> None:
        settings = CompanyCoordinationSettings(
            enabled=True,
            endpoint="https://coordination.example.test",
            company_scope_digest=_SCOPE,
            device_id="device-test-laptop",
            token_env="NORUCT_TEST_COORD_TOKEN",
        )
        receipt = {
            "schema": "noruct.company-coordination-identity.v1",
            "status": "AUTHORIZED",
            "company_scope_digest": _SCOPE,
            "device_id": "device-test-laptop",
            "server_mutated": False,
            "remote_execution_enabled": False,
        }
        with patch.dict(os.environ, {"NORUCT_TEST_COORD_TOKEN": "test-token"}), patch(
            "dynamic_firm.product.company_coordination_settings.RemoteCompanyCoordinationClient.preflight_identity",
            return_value=receipt,
        ):
            self.assertEqual(company_coordination_preflight(settings), receipt)
