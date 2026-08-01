from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.product.sessions import CompanySessionStore
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.company_session_recall import CompanySessionRecallTools
from dynamic_firm.runtime.models import ModelResponse, RunStatus, ToolCall, Usage
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.runtime.helpers import completion, make_request


class CompanySessionRecallToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_employee_can_search_then_read_only_prior_company_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = CompanySessionStore(root / "runtime.db")
            try:
                current = sessions.create(workspace=root, model="test")
                prior = sessions.create(workspace=root, model="test", title="Fixture review")
                sessions.append_turn(
                    session_id=prior.session_id,
                    goal="Review the fixture contract",
                    job_id="job-prior-1",
                    status="SUCCEEDED",
                    summary="The fixture contract needs a bounded read path.",
                    usage=Usage(),
                )
                sessions.append_turn(
                    session_id=current.session_id,
                    goal="Current private goal",
                    job_id="job-current-1",
                    status="SUCCEEDED",
                    summary="Current session must not appear in discovery.",
                    usage=Usage(),
                )
                registry = ToolRegistry()
                for definition in CompanySessionRecallTools(
                    sessions,
                    current_session_id=current.session_id,
                ).definitions():
                    registry.register(definition)
                provider = ScriptedModelProvider(
                    [
                        ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    "search-prior",
                                    "search_company_session_memory",
                                    {"query": "fixture"},
                                ),
                            )
                        ),
                        ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    "read-prior",
                                    "read_company_session_memory",
                                    {"session_id": prior.session_id},
                                ),
                            )
                        ),
                        ModelResponse(completion=completion("Recalled the prior Company result.")),
                    ]
                )
                run_store = RunStore()
                service = NativeEmployeeRuntimeService(
                    store=run_store,
                    provider=provider,
                    registry=registry,
                )
                try:
                    result = await service.collect(
                        await service.start(
                            make_request(
                                request_id="company-session-recall",
                                tool_names=(
                                    "search_company_session_memory",
                                    "read_company_session_memory",
                                ),
                                resource_patterns=("company:session:*",),
                            )
                        )
                    )
                finally:
                    await service.close()
                    run_store.close()
            finally:
                sessions.close()

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        final_context = str(provider.requests[-1].messages[-1].content)
        self.assertIn("bounded read path", final_context)
        self.assertNotIn("Current session must not appear", final_context)
        self.assertNotIn("Current private goal", final_context)

    async def test_current_or_unknown_session_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = CompanySessionStore(root / "runtime.db")
            try:
                current = sessions.create(workspace=root, model="test")
                tools = CompanySessionRecallTools(sessions, current_session_id=current.session_id)
                read = next(item for item in tools.definitions() if item.name == "read_company_session_memory")
                with self.assertRaisesRegex(Exception, "already available"):
                    read.validator({"session_id": current.session_id})
                valid = read.validator({"session_id": "00000000-0000-4000-8000-000000000000"})
                with self.assertRaisesRegex(Exception, "Unknown local Company session"):
                    await read.handler(valid, cancellation=_NeverCancelled())
            finally:
                sessions.close()


class _NeverCancelled:
    def raise_if_cancelled(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
