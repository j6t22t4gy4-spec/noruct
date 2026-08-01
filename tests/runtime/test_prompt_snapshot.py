from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from dynamic_firm.runtime.models import (
    ActionPolicy,
    PolicyDecision,
    ToolEffect,
    ToolGrant,
    VersionedContent,
    validate_request,
)
from dynamic_firm.runtime.prompt import PromptBuilder
from tests.runtime.helpers import make_request


class PromptSnapshotTests(unittest.TestCase):
    def test_snapshot_is_stable_and_excludes_secret_references(self) -> None:
        request = make_request()
        request = replace(
            request,
            action_policy=replace(request.action_policy, secret_refs=("SECRET_REF_SHOULD_NOT_APPEAR",)),
            requested_at=datetime.now(UTC),
        )
        later = replace(request, requested_at=request.requested_at + timedelta(hours=1))

        first = PromptBuilder().build(request)
        second = PromptBuilder().build(later)

        self.assertEqual(first.prompt_hash, second.prompt_hash)
        self.assertEqual(first.context_hash, second.context_hash)
        self.assertNotIn("SECRET_REF_SHOULD_NOT_APPEAR", first.system_prompt)
        self.assertNotIn("SECRET_REF_SHOULD_NOT_APPEAR", first.user_message)

    def test_employee_revision_changes_prompt_but_memory_changes_only_context(self) -> None:
        request = make_request()
        baseline = PromptBuilder().build(request)
        changed_employee = replace(
            request,
            employee=replace(request.employee, authority_revision="2"),
        )
        changed_memory = replace(
            request,
            context=replace(
                request.context,
                selected_memory=(
                    VersionedContent(
                        "employee-memory:employee-researcher:fact-2",
                        "1",
                        "New ephemeral fact",
                    ),
                ),
            ),
            employee=replace(
                request.employee,
                selected_memory_refs=("employee-memory:employee-researcher:fact-2",),
            ),
        )

        self.assertNotEqual(baseline.prompt_hash, PromptBuilder().build(changed_employee).prompt_hash)
        memory_snapshot = PromptBuilder().build(changed_memory)
        self.assertEqual(baseline.prompt_hash, memory_snapshot.prompt_hash)
        self.assertNotEqual(baseline.context_hash, memory_snapshot.context_hash)

    def test_knowledge_projection_proves_content_without_persisting_it(self) -> None:
        request = make_request()

        snapshot = PromptBuilder().build(request)
        projection = snapshot.knowledge_projection
        serialized = str(projection)

        self.assertEqual(projection["revision"], "noruct-employee-knowledge-v1")
        self.assertEqual(projection["skill_count"], 1)
        self.assertEqual(projection["memory_count"], 1)
        self.assertEqual(projection["authority"], "advisory-only")
        self.assertEqual(len(str(projection["skill_sha256"])), 64)
        self.assertEqual(len(str(projection["memory_sha256"])), 64)
        self.assertNotIn(request.employee.skills[0].content, serialized)
        self.assertNotIn(request.context.selected_memory[0].content, serialized)

    def test_knowledge_projection_rejects_cross_employee_content(self) -> None:
        request = make_request()
        crossed = replace(
            request,
            context=replace(
                request.context,
                selected_memory=(
                    VersionedContent(
                        "employee-memory:employee-other:private-fact",
                        "1",
                        "Do not cross this boundary.",
                    ),
                ),
            ),
            employee=replace(
                request.employee,
                selected_memory_refs=("employee-memory:employee-other:private-fact",),
            ),
        )

        with self.assertRaisesRegex(ValueError, "crossed its employee namespace"):
            validate_request(crossed)

    def test_memory_references_must_match_the_frozen_projection(self) -> None:
        request = make_request()
        mismatched = replace(
            request,
            employee=replace(
                request.employee,
                selected_memory_refs=("employee-memory:employee-researcher:missing",),
            ),
        )

        with self.assertRaisesRegex(ValueError, "exactly identify"):
            validate_request(mismatched)

    def test_phase_rejects_default_allow_policy(self) -> None:
        request = make_request()
        request = replace(
            request,
            action_policy=replace(request.action_policy, default_decision=PolicyDecision.ALLOW),
        )
        with self.assertRaisesRegex(ValueError, "default-deny"):
            validate_request(request)

    def test_phase_rejects_ambiguous_duplicate_tool_grants(self) -> None:
        request = make_request(tool_names=("read_fixture",))
        request = replace(
            request,
            action_policy=replace(
                request.action_policy,
                tool_grants=request.action_policy.tool_grants * 2,
            ),
        )
        with self.assertRaisesRegex(ValueError, "unique tool names"):
            validate_request(request)

    def test_employee_request_rejects_native_delegation_and_mcp_tool_names(self) -> None:
        for name in ("delegate_task", "mcp_issue_read"):
            with self.subTest(name=name):
                request = replace(
                    make_request(),
                    action_policy=ActionPolicy(
                        tool_grants=(
                            ToolGrant(name, (ToolEffect.READ,), ("*",), max_calls=1),
                        ),
                    ),
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "native delegation or MCP discovery",
                ):
                    validate_request(request)

    def test_external_read_policy_rejects_unapproved_resource_family(self) -> None:
        request = replace(
            make_request(),
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "raw_remote_reader",
                        (ToolEffect.NETWORK,),
                        ("arbitrary-network:https://example.invalid",),
                        max_calls=1,
                    ),
                ),
                network_policy="EXTERNAL_READ_ONLY",
            ),
        )
        with self.assertRaisesRegex(ValueError, "approved first-party resource family"):
            validate_request(request)

    def test_external_read_policy_accepts_a_bounded_normalized_multi_tool_allowlist(self) -> None:
        request = replace(
            make_request(),
            action_policy=ActionPolicy(
                tool_grants=(
                    ToolGrant(
                        "read_external_repository_context_1",
                        (ToolEffect.NETWORK,),
                        ("external-read:repository-context:read_external_repository_context_1",),
                        max_calls=1,
                    ),
                    ToolGrant(
                        "read_external_repository_context_2",
                        (ToolEffect.NETWORK,),
                        ("external-read:repository-context:read_external_repository_context_2",),
                        max_calls=1,
                    ),
                ),
                network_policy="EXTERNAL_READ_ONLY",
            ),
        )
        validate_request(request)

    def test_external_read_policy_rejects_more_than_twenty_four_network_capabilities(self) -> None:
        request = replace(
            make_request(),
            action_policy=ActionPolicy(
                tool_grants=tuple(
                    ToolGrant(
                        f"read_external_context_{index}",
                        (ToolEffect.NETWORK,),
                        (f"external-read:context:tool-{index}",),
                        max_calls=1,
                    )
                    for index in range(25)
                ),
                network_policy="EXTERNAL_READ_ONLY",
            ),
        )
        with self.assertRaisesRegex(ValueError, "between one and twenty-four"):
            validate_request(request)


if __name__ == "__main__":
    unittest.main()
