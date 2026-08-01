from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.application.natural_graph_editor import (
    NATURAL_GRAPH_EDIT_SCHEMA,
    NaturalGraphEditor,
    natural_graph_edit_schema,
)
from dynamic_firm.company.graph_blueprint_models import (
    GraphBlueprint,
    GraphBlueprintOrigin,
    GraphBlueprintTask,
)
from dynamic_firm.runtime.models import StructuredOutputResponse, Usage


def source_blueprint() -> GraphBlueprint:
    return GraphBlueprint(
        blueprint_id="release_review",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective", "requested_outcome"),
        tasks=(
            GraphBlueprintTask(
                task_id="final",
                objective_template="Review {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Answer {{requested_outcome}}",),
            ),
        ),
        final_task_id="final",
        origin=GraphBlueprintOrigin.DRAFT,
    )


class _Provider:
    structured_model_call_ceiling = 1

    def __init__(self, value: dict[str, object]) -> None:
        self.value = value
        self.requests = []

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        return StructuredOutputResponse(
            value=self.value,
            usage=Usage(input_tokens=14, output_tokens=9, model_calls=1),
            provider_request_id="natural-editor-fixture",
        )


class NaturalGraphEditorTests(unittest.TestCase):
    def test_returns_validated_unsaved_user_revision_candidate(self) -> None:
        provider = _Provider(
            {
                "rationale": "Add independent evidence before the final synthesis.",
                "objective_class": "general",
                "execution_profiles": ["read_only"],
                "parameters": ["objective", "requested_outcome"],
                "tasks": [
                    {
                        "task_id": "evidence",
                        "objective_template": "Collect evidence for {{objective}}",
                        "depends_on": [],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["Evidence for {{requested_outcome}}"],
                        "risk_level": "LOW",
                        "execution_replica": None,
                    },
                    {
                        "task_id": "final",
                        "objective_template": "Review {{objective}}",
                        "depends_on": ["evidence"],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["Answer {{requested_outcome}}"],
                        "risk_level": "LOW",
                        "execution_replica": None,
                    },
                ],
                "final_task_id": "final",
            }
        )
        source = source_blueprint()

        proposal = asyncio.run(
            NaturalGraphEditor(provider, model_profile="fixture", timeout_seconds=1).propose(
                source, "Add an evidence step before the final review."
            )
        )

        self.assertEqual(proposal.source, source)
        self.assertEqual(proposal.candidate.version, 2)
        self.assertEqual(proposal.candidate.parent_ref, source.ref)
        self.assertEqual(proposal.candidate.origin, GraphBlueprintOrigin.USER_REVISION)
        self.assertEqual(tuple(task.task_id for task in proposal.candidate.tasks), ("evidence", "final"))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0].schema_name, NATURAL_GRAPH_EDIT_SCHEMA)
        self.assertEqual(proposal.payload()["origin"], "USER_REVISION")

    def test_refuses_replica_creation_and_preserves_source(self) -> None:
        provider = _Provider(
            {
                "rationale": "Try a replica.",
                "objective_class": "general",
                "execution_profiles": ["read_only"],
                "parameters": ["objective", "requested_outcome"],
                "tasks": [
                    {
                        "task_id": "final",
                        "objective_template": "Review {{objective}}",
                        "depends_on": [],
                        "required_capabilities": ["analysis"],
                        "acceptance_templates": ["Answer {{requested_outcome}}"],
                        "risk_level": "LOW",
                        "execution_replica": {"not": "supported"},
                    }
                ],
                "final_task_id": "final",
            }
        )
        source = source_blueprint()

        with self.assertRaisesRegex(ValueError, "does not create replica"):
            asyncio.run(
                NaturalGraphEditor(provider, model_profile="fixture", timeout_seconds=1).propose(
                    source, "Use a replica."
                )
            )
        self.assertEqual(source.version, 1)
        self.assertEqual(len(source.tasks), 1)

    def test_schema_has_strict_top_level_contract(self) -> None:
        schema = natural_graph_edit_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
