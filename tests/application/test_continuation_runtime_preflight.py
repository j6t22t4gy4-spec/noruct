from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from unittest import mock

from dynamic_firm.application.continuation_runtime_preflight import (
    ContinuationRuntimePreflightCode,
    ContinuationRuntimePreflightError,
    bind_company_run_request_runtime,
    bind_continuation_runtime,
    build_validated_continuation_provider,
    company_coordination_binding_digest,
    granted_tool_contract_digest,
    provider_binding_digest,
    validate_continuation_runtime,
)
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.company_coordination import RemoteCompanyCoordinationConfig
from dynamic_firm.runtime.models import (
    ActionPolicy,
    IdempotencyMode,
    ToolEffect,
    ToolGrant,
    ToolRisk,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tool_contracts import ToolDefinition, ToolRegistry
from tests.kernel.helpers import company_request, task


class FixtureMode(StrEnum):
    DIRECT = "direct"


@dataclass(frozen=True, slots=True)
class FixtureProviderConfig:
    workspace: Path
    model: str
    api_key_env: str = "FIXTURE_API_KEY"
    mode: FixtureMode = FixtureMode.DIRECT
    options: object = None


@dataclass(frozen=True, slots=True)
class UnsafeProviderConfig:
    model: str
    api_key: str


async def _handler(arguments, cancellation: CancellationToken) -> str:
    cancellation.raise_if_cancelled()
    return str(arguments)


def _definition(
    name: str,
    effect: ToolEffect = ToolEffect.READ,
    **changes,
) -> ToolDefinition:
    definition = ToolDefinition(
        name=name,
        description=f"Use {name}",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        effect=effect,
        risk=ToolRisk.LOW,
        idempotency_mode=IdempotencyMode.NATURAL_KEY,
        validator=lambda value: value,
        resource_key=lambda _: "fixture:*",
        handler=_handler,
    )
    return replace(definition, **changes)


def _registry(*definitions: ToolDefinition) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry


def _policy(*grants: tuple[str, ToolEffect]) -> ActionPolicy:
    return ActionPolicy(
        tool_grants=tuple(
            ToolGrant(name, (effect,)) for name, effect in grants
        )
    )


class ContinuationRuntimePreflightTests(unittest.TestCase):
    def assert_code(
        self,
        code: ContinuationRuntimePreflightCode,
        function,
        /,
        *args,
        **kwargs,
    ) -> None:
        with self.assertRaises(ContinuationRuntimePreflightError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code.value)

    def test_provider_binding_is_order_stable_and_excludes_workspace(self) -> None:
        first = FixtureProviderConfig(
            workspace=Path("/first/private/workspace"),
            model="fixture-model",
            options={"limits": [1, 2], "route": "primary"},
        )
        second = FixtureProviderConfig(
            workspace=Path("/different/private/workspace"),
            model="fixture-model",
            options={"route": "primary", "limits": [1, 2]},
        )

        self.assertEqual(
            provider_binding_digest(first),
            provider_binding_digest(second),
        )
        self.assertRegex(provider_binding_digest(first), r"^[0-9a-f]{64}$")

    def test_provider_type_and_non_workspace_path_are_bound(self) -> None:
        @dataclass(frozen=True, slots=True)
        class AlternateProviderConfig:
            workspace: Path
            model: str

        @dataclass(frozen=True, slots=True)
        class PathProviderConfig:
            workspace: Path
            executable: Path

        fixture = FixtureProviderConfig(Path("/workspace"), "model")
        alternate = AlternateProviderConfig(Path("/workspace"), "model")
        self.assertNotEqual(
            provider_binding_digest(fixture),
            provider_binding_digest(alternate),
        )
        self.assertNotEqual(
            provider_binding_digest(
                PathProviderConfig(Path("/a"), Path("/bin/first"))
            ),
            provider_binding_digest(
                PathProviderConfig(Path("/b"), Path("/bin/second"))
            ),
        )

    def test_provider_binding_rejects_raw_secret_and_cycles(self) -> None:
        self.assert_code(
            ContinuationRuntimePreflightCode.PROVIDER_CONFIG_INVALID,
            provider_binding_digest,
            UnsafeProviderConfig("model", "do-not-hash-this"),
        )
        cyclic: dict[str, object] = {}
        cyclic["nested"] = cyclic
        self.assert_code(
            ContinuationRuntimePreflightCode.PROVIDER_CONFIG_INVALID,
            provider_binding_digest,
            cyclic,
        )

    def _coordination(
        self,
        *,
        endpoint: str = "https://coordination.example.com",
        scope: str = "a" * 64,
        device_id: str = "device-laptop-a",
        token_env: str = "NORUCT_COMPANY_COORDINATION_TOKEN",
        allow_insecure_loopback: bool = False,
    ) -> RemoteCompanyCoordinationConfig:
        return RemoteCompanyCoordinationConfig(
            endpoint=endpoint,
            company_scope_digest=scope,
            device_id=device_id,
            token_env=token_env,
            allow_insecure_loopback=allow_insecure_loopback,
        )

    def test_coordination_binding_normalizes_origin_and_excludes_device(self) -> None:
        first = self._coordination(
            endpoint=" https://Coordination.Example.COM:443/ ",
            device_id="device-laptop-a",
        )
        second = self._coordination(
            endpoint="https://coordination.example.com",
            device_id="device-laptop-b",
        )

        self.assertEqual(
            company_coordination_binding_digest(first),
            company_coordination_binding_digest(second),
        )

    @mock.patch(
        "dynamic_firm.runtime.company_coordination._token_from_environment",
        side_effect=AssertionError("coordination digest must not read a token"),
    )
    def test_coordination_binding_never_reads_the_secret(self, _token) -> None:
        digest = company_coordination_binding_digest(self._coordination())

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        _token.assert_not_called()

    def test_coordination_binding_covers_domain_and_disabled_state(self) -> None:
        baseline = company_coordination_binding_digest(self._coordination())
        disabled = company_coordination_binding_digest(None)
        variants = (
            self._coordination(endpoint="https://other.example.com"),
            self._coordination(scope="b" * 64),
            self._coordination(token_env="OTHER_COORDINATION_TOKEN"),
            self._coordination(
                endpoint="http://127.0.0.1:8787",
                allow_insecure_loopback=True,
            ),
        )

        self.assertNotEqual(baseline, disabled)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    baseline,
                    company_coordination_binding_digest(variant),
                )

    def test_tool_binding_contains_only_granted_definitions(self) -> None:
        read = _definition("read_fixture")
        withheld = _definition("withheld_fixture")
        first, count = granted_tool_contract_digest(
            _registry(read),
            _policy(("read_fixture", ToolEffect.READ)),
        )
        second, second_count = granted_tool_contract_digest(
            _registry(read, withheld),
            _policy(("read_fixture", ToolEffect.READ)),
        )

        self.assertEqual((first, count), (second, second_count))
        self.assertEqual(count, 1)

    def test_tool_binding_is_order_stable_but_contract_sensitive(self) -> None:
        read = _definition("read_fixture")
        inspect = _definition("inspect_fixture")
        first, _ = granted_tool_contract_digest(
            _registry(read, inspect),
            _policy(
                ("read_fixture", ToolEffect.READ),
                ("inspect_fixture", ToolEffect.READ),
            ),
        )
        reordered, _ = granted_tool_contract_digest(
            _registry(inspect, read),
            _policy(
                ("inspect_fixture", ToolEffect.READ),
                ("read_fixture", ToolEffect.READ),
            ),
        )
        changed, _ = granted_tool_contract_digest(
            _registry(
                replace(
                    read,
                    requires_approval=True,
                    approval_preview=lambda _value: "safe preview",
                    allow_session_approval=True,
                    parallel_safe=True,
                    timeout_ms=read.timeout_ms + 1,
                    output_limit_bytes=read.output_limit_bytes + 1,
                ),
                inspect,
            ),
            _policy(
                ("read_fixture", ToolEffect.READ),
                ("inspect_fixture", ToolEffect.READ),
            ),
        )

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_tool_binding_fails_closed_on_dangling_or_effect_mismatch(self) -> None:
        registry = _registry(_definition("write_fixture", ToolEffect.WRITE))
        self.assert_code(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID,
            granted_tool_contract_digest,
            registry,
            _policy(("missing_fixture", ToolEffect.READ)),
        )
        self.assert_code(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID,
            granted_tool_contract_digest,
            registry,
            _policy(("write_fixture", ToolEffect.READ)),
        )

    def test_tool_binding_rejects_duplicate_grants(self) -> None:
        registry = _registry(_definition("read_fixture"))
        policy = _policy(
            ("read_fixture", ToolEffect.READ),
            ("read_fixture", ToolEffect.READ),
        )
        self.assert_code(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID,
            granted_tool_contract_digest,
            registry,
            policy,
        )

    def test_bind_and_validate_exact_runtime_contract(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        binding = bind_continuation_runtime(
            provider_config=provider,
            registry=registry,
            policy=policy,
        )

        result = validate_continuation_runtime(
            expected_provider_digest=binding.provider_binding_digest,
            expected_tool_digest=binding.tool_contract_digest,
            expected_company_coordination_digest=(
                binding.company_coordination_digest
            ),
            provider_config=replace(provider, workspace=Path("/moved")),
            registry=registry,
            policy=policy,
            company_coordination=None,
        )

        self.assertEqual(result.provider_binding_digest, binding.provider_binding_digest)
        self.assertEqual(result.tool_contract_digest, binding.tool_contract_digest)
        self.assertEqual(
            result.company_coordination_digest,
            binding.company_coordination_digest,
        )
        self.assertEqual(result.tool_count, 1)

    def test_new_company_request_freezes_every_runtime_binding(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        request = replace(
            company_request(
                (task("only"),),
                final_task_id="only",
                roster=(EmployeeRecord("employee", "Analyst", ("analysis",)),),
            ),
            action_policy=policy,
        )

        bound = bind_company_run_request_runtime(
            request,
            "f" * 64,
            provider,
            registry,
            None,
        )

        self.assertEqual(bound.firm_admission_digest, "f" * 64)
        self.assertEqual(
            bound.runtime_provider_binding_digest,
            provider_binding_digest(provider),
        )
        self.assertEqual(
            bound.runtime_tool_contract_digest,
            granted_tool_contract_digest(registry, policy)[0],
        )
        self.assertEqual(
            bound.runtime_company_coordination_digest,
            company_coordination_binding_digest(None),
        )
        self.assertRegex(
            bound.runtime_company_coordination_digest,
            r"^[0-9a-f]{64}$",
        )

    def test_validate_reports_stable_missing_and_mismatch_codes(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        binding = bind_continuation_runtime(
            provider_config=provider,
            registry=registry,
            policy=policy,
        )
        arguments = {
            "expected_provider_digest": binding.provider_binding_digest,
            "expected_tool_digest": binding.tool_contract_digest,
            "expected_company_coordination_digest": (
                binding.company_coordination_digest
            ),
            "provider_config": provider,
            "registry": registry,
            "policy": policy,
            "company_coordination": None,
        }

        for field, value, code in (
            (
                "expected_provider_digest",
                "",
                ContinuationRuntimePreflightCode.PROVIDER_BINDING_MISSING,
            ),
            (
                "expected_tool_digest",
                "",
                ContinuationRuntimePreflightCode.TOOL_CONTRACT_MISSING,
            ),
            (
                "expected_company_coordination_digest",
                "",
                ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISSING,
            ),
            (
                "expected_provider_digest",
                "f" * 64,
                ContinuationRuntimePreflightCode.PROVIDER_BINDING_MISMATCH,
            ),
            (
                "expected_tool_digest",
                "e" * 64,
                ContinuationRuntimePreflightCode.TOOL_CONTRACT_MISMATCH,
            ),
            (
                "expected_company_coordination_digest",
                "d" * 64,
                ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISMATCH,
            ),
        ):
            with self.subTest(field=field):
                self.assert_code(
                    code,
                    validate_continuation_runtime,
                    **{**arguments, field: value},
                )

    def test_validate_classifies_a_broken_current_registry_as_invalid(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        valid_registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        binding = bind_continuation_runtime(
            provider_config=provider,
            registry=valid_registry,
            policy=policy,
        )

        self.assert_code(
            ContinuationRuntimePreflightCode.TOOL_CONTRACT_INVALID,
            validate_continuation_runtime,
            expected_provider_digest=binding.provider_binding_digest,
            expected_tool_digest=binding.tool_contract_digest,
            expected_company_coordination_digest=(
                binding.company_coordination_digest
            ),
            provider_config=provider,
            registry=ToolRegistry(),
            policy=policy,
            company_coordination=None,
        )

    def test_missing_historical_coordination_never_constructs_provider(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        binding = bind_continuation_runtime(
            provider_config=provider,
            registry=registry,
            policy=policy,
        )
        calls: list[object] = []

        self.assert_code(
            ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISSING,
            build_validated_continuation_provider,
            expected_provider_digest=binding.provider_binding_digest,
            expected_tool_digest=binding.tool_contract_digest,
            expected_company_coordination_digest="",
            provider_config=provider,
            registry=registry,
            policy=policy,
            provider_factory=lambda config: calls.append(config),
            company_coordination=None,
        )

        self.assertEqual(calls, [])

    def test_coordination_domain_drift_and_enablement_drift_fail_closed(self) -> None:
        provider = FixtureProviderConfig(Path("/workspace"), "model")
        registry = _registry(_definition("read_fixture"))
        policy = _policy(("read_fixture", ToolEffect.READ))
        enabled = self._coordination()
        binding = bind_continuation_runtime(
            provider_config=provider,
            registry=registry,
            policy=policy,
            company_coordination=enabled,
        )
        arguments = {
            "expected_provider_digest": binding.provider_binding_digest,
            "expected_tool_digest": binding.tool_contract_digest,
            "expected_company_coordination_digest": (
                binding.company_coordination_digest
            ),
            "provider_config": provider,
            "registry": registry,
            "policy": policy,
        }

        for current in (None, self._coordination(scope="b" * 64)):
            with self.subTest(current=current):
                self.assert_code(
                    ContinuationRuntimePreflightCode.COMPANY_COORDINATION_BINDING_MISMATCH,
                    validate_continuation_runtime,
                    **arguments,
                    company_coordination=current,
                )


if __name__ == "__main__":
    unittest.main()
