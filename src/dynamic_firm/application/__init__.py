"""First-party application orchestration for Noruct product surfaces.

Modules here compose already-authoritative Company, Employee and product
contracts.  They do not own Company state, permissions, budgets or UI state.
"""

from .goal_execution import GoalExecutionServices, PreparedGoalExecution
from .graph_proposal_continuation import (
    GraphProposalContinuationService,
    GraphProposalDecisionOutcome,
)

__all__ = [
    "GoalExecutionServices",
    "PreparedGoalExecution",
    "GraphProposalContinuationService",
    "GraphProposalDecisionOutcome",
]
