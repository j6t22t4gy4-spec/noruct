from __future__ import annotations

from typing import Protocol

from .models import (
    CompanyRunRequest,
    GraphPatchEvent,
    GraphPatchProposalEvent,
    JobGraph,
    JobMutationEvent,
    JobResult,
    TaskAttemptRecord,
)


class ActiveJobLedgerPort(Protocol):
    """Fail-closed durable audit boundary owned by the Firm Kernel caller."""

    def start_job(
        self,
        request: CompanyRunRequest,
        graph: JobGraph,
        frozen_snapshot_hash: str,
    ) -> None: ...

    def append_attempt(self, job_id: str, record: TaskAttemptRecord) -> None: ...

    def append_mutation(self, job_id: str, event: JobMutationEvent) -> None: ...

    def append_graph_patch(self, job_id: str, event: GraphPatchEvent) -> None: ...

    def append_graph_proposal(
        self,
        job_id: str,
        event: GraphPatchProposalEvent,
    ) -> None: ...

    def finish_job(self, job_id: str, result: JobResult) -> None: ...
