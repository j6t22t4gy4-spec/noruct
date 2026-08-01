"""Dependency-free run-liveness classifier adapted from Paperclip.

Upstream: https://github.com/paperclipai/paperclip
Commit: ce7dedf33d2689673826ffdcfd6af7ee06be39af
Source files:
- server/src/services/run-liveness.ts
  c6e789f255e9b98cc6a06afb0c180e6b55c80cd0d09c3e4ad8ee3ff7954e72cd
- server/src/services/recovery/run-liveness-continuations.ts
  1b3ffa51f092e1a8bb8a72758f52496ba7ac7a85285edf072c4c65b62f7e34ee
Copyright (c) 2025 Paperclip AI. SPDX-License-Identifier: MIT.

Modifications: ported from TypeScript to dependency-free Python; narrowed to
the evidence available at Noruct's Employee Runtime boundary; added Korean
future-action and blocker recognition; accepts useful read-only answers as
completed; continuation scheduling remains outside this private module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrivateLivenessClassification:
    state: str
    reason: str
    actionability: str


_ENGLISH_PLANNING_ONLY = re.compile(
    r"\b(?:i(?:'ll| will| am going to|'m going to)|let me|i need to|"
    r"my next step is|the next step is)\s+(?:first\s+)?"
    r"(?:inspect|check|review|look|investigate|analy[sz]e|open|read|start|"
    r"begin|work on|implement|fix|test|update|create|add|write|verify|validate|"
    r"deploy|delete|rotate)\b",
    re.IGNORECASE,
)
_LABELED_FUTURE_ACTION = re.compile(
    r"^\s*(?:next action|next steps?)\s*:\s*"
    r"(?:inspect|check|review|look|investigate|analy[sz]e|open|read|start|"
    r"begin|implement|fix|test|update|create|add|write|verify|validate|run|execute|"
    r"deploy|delete|rotate)\b",
    re.IGNORECASE | re.MULTILINE,
)
_KOREAN_PLANNING_ONLY = re.compile(
    r"(?:먼저|이제|다음으로|우선|제가).{0,80}"
    r"(?:살펴보겠습니다|확인하겠습니다|검토하겠습니다|분석하겠습니다|"
    r"구현하겠습니다|진행하겠습니다|테스트하겠습니다|수정하겠습니다|"
    r"작성하겠습니다|검증하겠습니다|시작하겠습니다)"
)
_APPROVAL_REQUIRED = re.compile(
    r"\b(?:approval required|pending approval|waiting (?:on|for).{0,80}approval|"
    r"requires?.{0,80}approval|need(?:s|ed)?.{0,80}approval)\b|"
    r"(?:승인|허가).{0,40}(?:필요|대기)|(?:승인|허가)을? 기다",
    re.IGNORECASE,
)
_EXTERNAL_BLOCKER = re.compile(
    r"\b(?:can't proceed|cannot proceed|unable to proceed|waiting on|blocked by|"
    r"need(?:s|ed)?|requires?).{0,120}\b(?:access|credentials?|secrets?|api key|"
    r"token|password|login|account|permissions?|input|clarification)\b|"
    r"(?:접근 권한|자격 증명|비밀키|API 키|토큰|로그인|사용자 입력|추가 설명).{0,60}"
    r"(?:필요|없어|대기|진행할 수 없)",
    re.IGNORECASE,
)
_MANAGER_REVIEW = re.compile(
    r"\b(?:manager review|human review|manual review|security review|"
    r"production deploy|deploy(?:ing)? to prod(?:uction)?|production access|"
    r"cost approval|spend approval)\b|(?:운영|프로덕션).{0,40}(?:배포|삭제|접근)",
    re.IGNORECASE,
)
_PLANNING_DELIVERABLE = re.compile(
    r"\b(?:create|write|produce|draft|update|revise|prepare)\s+"
    r"(?:a\s+|the\s+)?(?:plan|proposal|design doc|research report|report|write-?up)\b|"
    r"(?:계획|제안서|설계 문서|연구 보고서|보고서).{0,30}(?:작성|초안|준비|수정)",
    re.IGNORECASE,
)


def _is_planning_deliverable(objective: str) -> bool:
    return bool(_PLANNING_DELIVERABLE.search(objective.strip()))


def _actionability(summary: str) -> str:
    if _APPROVAL_REQUIRED.search(summary):
        return "approval_required"
    if _EXTERNAL_BLOCKER.search(summary):
        return "blocked_external"
    if _MANAGER_REVIEW.search(summary):
        return "manager_review"
    return "runnable"


def _looks_like_planning_only(summary: str) -> bool:
    return bool(
        _ENGLISH_PLANNING_ONLY.search(summary)
        or _LABELED_FUTURE_ACTION.search(summary)
        or _KOREAN_PLANNING_ONLY.search(summary)
    )


def classify_run_liveness(
    *,
    run_status: str,
    objective: str,
    summary: str,
    concrete_action_count: int,
) -> PrivateLivenessClassification:
    """Classify a terminal run without trusting model-authored evidence claims."""

    if run_status != "SUCCEEDED":
        return PrivateLivenessClassification(
            state="failed",
            reason=f"Run ended with {run_status}",
            actionability="unknown",
        )

    useful_output = bool(summary.strip())
    actionability = _actionability(summary)
    if actionability in {"approval_required", "blocked_external"}:
        return PrivateLivenessClassification(
            state="blocked",
            reason="Run output declared a concrete external or approval blocker",
            actionability=actionability,
        )
    if concrete_action_count > 0:
        return PrivateLivenessClassification(
            state="advanced",
            reason="Run produced runtime-observed tool or artifact evidence",
            actionability=actionability,
        )
    if not useful_output:
        return PrivateLivenessClassification(
            state="empty_response",
            reason="Run succeeded without useful output or concrete action evidence",
            actionability="unknown",
        )
    if _is_planning_deliverable(objective):
        return PrivateLivenessClassification(
            state="advanced",
            reason="Planning deliverable produced useful output",
            actionability=actionability,
        )
    if _looks_like_planning_only(summary):
        if actionability == "runnable":
            return PrivateLivenessClassification(
                state="plan_only",
                reason="Run described future work without concrete action evidence",
                actionability=actionability,
            )
        return PrivateLivenessClassification(
            state="needs_review",
            reason="Future work is not safe to continue automatically",
            actionability=actionability,
        )
    return PrivateLivenessClassification(
        state="completed",
        reason="Run produced a useful terminal answer",
        actionability=actionability,
    )
