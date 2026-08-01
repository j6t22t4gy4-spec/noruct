"""Deterministic first-pass operating policy for the persistent Company.

Every interactive input belongs to the Company.  This classifier does not
choose an employee, grant a tool, or approve an effect.  It records an
outcome-efficient initial coordination shape and the strongest effect
explicitly requested by the user.  Planning cost matters, but it cannot
suppress a bounded replica opportunity that is likely to improve coverage,
candidate quality, or diagnosis.  The Firm Kernel and action policy remain
authoritative for staffing, tools, approvals, and execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
)


class CompanyWorkMode(StrEnum):
    """User-facing Company work shape before runtime evidence is available."""

    DIRECT = "DIRECT"
    SOLO_JOB = "SOLO_JOB"
    TEAM_JOB = "TEAM_JOB"


class InitialCoordinationPolicy(StrEnum):
    """How the Company should begin, not how the Job must finish."""

    DIRECT = "DIRECT"
    SOLO_FIRST = "SOLO_FIRST"
    PLAN_FIRST = "PLAN_FIRST"


class RequestedEffect(StrEnum):
    """Strongest explicit effect in the input; never an authority grant."""

    READ = "READ"
    WORKSPACE_CHANGE = "WORKSPACE_CHANGE"
    HOST_ACTION = "HOST_ACTION"


class OperatingReason(StrEnum):
    DIRECT_USER_MESSAGE = "DIRECT_USER_MESSAGE"
    WORKSPACE_CONTEXT = "WORKSPACE_CONTEXT"
    INTENT_OR_DECISION_GOAL = "INTENT_OR_DECISION_GOAL"
    KNOWLEDGE_OR_EVIDENCE_GOAL = "KNOWLEDGE_OR_EVIDENCE_GOAL"
    ACTION_ORIENTED_GOAL = "ACTION_ORIENTED_GOAL"
    EXPLICIT_TEAM_COORDINATION = "EXPLICIT_TEAM_COORDINATION"
    INDEPENDENT_REVIEW_REQUIRED = "INDEPENDENT_REVIEW_REQUIRED"
    STRUCTURED_MULTI_WORKSTREAM = "STRUCTURED_MULTI_WORKSTREAM"
    COMPOUND_CROSS_FUNCTIONAL_GOAL = "COMPOUND_CROSS_FUNCTIONAL_GOAL"
    REPLICA_VALUE_OPPORTUNITY = "REPLICA_VALUE_OPPORTUNITY"


@dataclass(frozen=True, slots=True)
class CompanyOperatingDecision:
    """A Company-owned input classification with no implied permission."""

    work_mode: CompanyWorkMode
    coordination_policy: InitialCoordinationPolicy
    requested_effect: RequestedEffect
    reason: OperatingReason
    requires_independent_review: bool = False
    execution_replica_preference: ExecutionReplicaPreference = (
        ExecutionReplicaPreference.PERFORMANCE_FIRST
    )
    suggested_execution_replica_strategy: ExecutionReplicaStrategy | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.execution_replica_preference,
            ExecutionReplicaPreference,
        ):
            raise TypeError("Execution replica preference must be typed")
        if (
            self.suggested_execution_replica_strategy is not None
            and not isinstance(
                self.suggested_execution_replica_strategy,
                ExecutionReplicaStrategy,
            )
        ):
            raise TypeError("Suggested execution replica strategy must be typed")
        if (
            self.execution_replica_preference
            is ExecutionReplicaPreference.DISABLED
            and self.suggested_execution_replica_strategy is not None
        ):
            raise ValueError("Disabled replica planning cannot suggest a strategy")

    @property
    def company_owned(self) -> bool:
        """All inputs, including direct answers, are Company-owned."""

        return True


_DEFINITIONAL_QUESTION = re.compile(
    r"(?:뭐야|무엇(?:이야|인가|입니까)?|누구야|어떻게\s*(?:작동|동작)|설명해|알려줘|"
    r"what\s+is|what\s+are|who\s+is|how\s+does|explain)",
    re.IGNORECASE,
)
_WORKSPACE_SIGNAL = re.compile(
    r"(?:저장소|레포(?:지토리)?|워크스페이스|프로젝트|코드베이스|소스\s*코드|"
    r"repository|repo\b|workspace|codebase|this\s+project|this\s+file|"
    r"(?:^|[\s/])(?:src|tests?|docs?)/|\.[a-z0-9]{1,8}\b)",
    re.IGNORECASE,
)
_ACTION_SIGNAL = re.compile(
    r"(?:구현|수정|고쳐|바꿔|만들어|생성|작성|문서화|리팩터|디버그|테스트|검증|"
    r"분석|조사|리서치|비교|검토|리뷰|설계|계획|전략|배포|설치|통합|최적화|"
    r"실행(?:해|해줘|하|할)?|명령(?:어)?|터미널|프로세스|중지|재시작|설정|켜줘|꺼줘|caffeinate|"
    r"implement|modify|change|fix|build|create|write|document|refactor|debug|test|"
    r"validate|inspect|analy[sz]e|research|compare|review|design|plan|deploy|install|"
    r"integrate|optimi[sz]e|run|execute|command|terminal|process|start|stop|restart)",
    re.IGNORECASE,
)
_KNOWLEDGE_SIGNAL = re.compile(
    r"(?:pdf|docx|문서|자료|근거|출처|지식|knowledge|evidence|citation|ocr|기억|"
    r"remember|recall)",
    re.IGNORECASE,
)
_INTENT_SIGNAL = re.compile(
    r"(?:목표|결정|보류|재검토|우선순위|제약|성공\s*조건|질문|조사\s*요청|"
    r"intent|decision|priority|constraint)",
    re.IGNORECASE,
)
_SETTINGS_CONTEXT = re.compile(r"(?:설정|settings?\b)", re.IGNORECASE)

# Requested effects deliberately require explicit operational language.  A
# definition such as "리팩터링이 뭐야?" is READ even though it contains a word
# associated with mutation.
_WORKSPACE_CHANGE = re.compile(
    r"(?:구현(?:해|하|할|해서|하고)?|수정(?:해|하|할|해서|하고)?|고쳐|리팩터|"
    r"코드(?:를|를\s+직접)?\s*(?:바꿔|작성|생성)|파일(?:을|를)?\s*(?:만들|생성|작성|수정|삭제|이동)|"
    r"implement\b|refactor\b|fix\b|modify\b|edit\b|patch\b|"
    r"(?:create|write|delete|move|rename|change)\s+(?:the\s+)?(?:file|code|source|project))",
    re.IGNORECASE,
)
_HOST_ACTION = re.compile(
    r"(?:실행(?:해|해줘|하|할)?|명령(?:어)?|터미널|프로세스|중지|재시작|배포|설치|"
    r"설정(?:해|하|할)?|변경(?:해|하|할)?|바꿔|켜줘|꺼줘|저장(?:해|해줘|하|할)|"
    r"브라우저(?:를)?\s*(?:열|조작)|메시지(?:를)?\s*(?:보내|전송)|caffeinate|"
    r"run\b|execute\b|command\b|terminal\b|process\b|start\b|stop\b|restart\b|"
    r"deploy\b|install\b|publish\b|send\s+(?:a\s+)?message|open\s+(?:the\s+)?browser)",
    re.IGNORECASE,
)

_INDEPENDENT_REVIEW = re.compile(
    r"(?:독립(?:적(?:인|으로)?)?\s*(?:검토|리뷰|검증)|별도\s*(?:검토|리뷰|검증)|"
    r"independent(?:ly)?\s+(?:review|verify|validate)|separate\s+(?:review|validation))",
    re.IGNORECASE,
)
_EXPLICIT_MULTI_DELIVERABLE = re.compile(
    r"(?:"
    r"[^.!?\n]{1,48}(?:와|과|및)[^.!?\n]{1,48}(?:을|를)?\s*각각\s*"
    r"(?:구현|설계|조사|작성|분석)[^.!?\n]{0,48}(?:통합|결합|비교|종합)"
    r"|(?:독립(?:된|적인?)?|별도(?:의)?)\s*(?:보고서|결과|산출물|작업)\s*"
    r"(?:두|2)\s*(?:개|가지)?[^.!?\n]{0,32}(?:각각|별도|독립)"
    r"|(?:implement|design|research|analy[sz]e|write)\s+[^.!?\n]{1,48}\s+"
    r"and\s+[^.!?\n]{1,48}\s+(?:separately|independently|in\s+parallel)"
    r"[^.!?\n]{0,48}(?:integrate|combine|compare|synthesize)"
    r"|(?:in\s+parallel\s+)?(?:analy[sz]e|inspect|research|review)\s+"
    r"[^,.!?\n]{1,64}\s+and\s+(?:analy[sz]e|inspect|research|review)\s+"
    r"[^,.!?\n]{1,64},?\s*(?:then\s+)?(?:independently\s+)?"
    r"(?:integrate|combine|compare|synthesize)"
    r"|(?:two|2)\s+(?:separate|independent)\s+"
    r"(?:reports?|deliverables?|workstreams?|outputs?))",
    re.IGNORECASE,
)

# This is deliberately a small directive grammar, not general sentiment or
# natural-language parsing.  It removes only explicit negations of the exact
# coordination/effect signals owned by this classifier.  The positive clause
# after "말고" / "but" remains available for ordinary classification.
_NEGATED_DIRECTIVES = (
    re.compile(
        r"병렬(?:로|화해서|화하여)?\s*(?:진행|실행|작업)?\s*(?:하)?지\s*"
        r"(?:말(?:고|아|라|아줘)|마(?:라|세요|줘)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"여러\s*(?:직원|에이전트)(?:을|를)?\s*(?:쓰|사용하)지\s*"
        r"(?:말(?:고|아|라|아줘)|마(?:라|세요|줘)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:독립(?:적(?:인|으로)?)?|별도)\s*(?:검토|리뷰|검증)"
        r"(?:은|는|을|를)?\s*(?:하)?지\s*"
        r"(?:말(?:고|아|라|아줘)|마(?:라|세요|줘)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:파일|코드|소스|저장소|워크스페이스)(?:을|를)?\s*"
        r"(?:수정하|변경하|고치|바꾸|작성하|삭제하)지\s*"
        r"(?:말(?:고|아|라|아줘)|마(?:라|세요|줘)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"설정(?:을|를)?\s*(?:바꾸|변경하|수정하|저장하)지\s*"
        r"(?:말(?:고|아|라|아줘)|마(?:라|세요|줘)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:do\s+not|don['’]?t|never)\s+(?:use\s+)?(?:multiple\s+agents?|"
        r"a\s+team|parallel(?:ize)?|work\s+in\s+parallel)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:without|no)\s+(?:multiple\s+agents?|a\s+team|parallel(?:ization)?|"
        r"parallel\s+work)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:do\s+not|don['’]?t|never|skip)\s+(?:an?\s+)?"
        r"(?:independent(?:ly)?\s+)?(?:review|verify|validate)|"
        r"no\s+independent\s+(?:review|verification|validation))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:do\s+not|don['’]?t|never)\s+(?:modify|edit|change|write|patch)\s+"
        r"(?:the\s+)?(?:files?|code|source|workspace|repository)|"
        r"without\s+(?:modifying|editing|changing|writing|patching)\s+"
        r"(?:the\s+)?(?:files?|code|source|workspace|repository))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:do\s+not|don['’]?t|never)\s+(?:change|modify|edit|save)\s+"
        r"(?:the\s+)?settings?",
        re.IGNORECASE,
    ),
)
_LIST_ITEM = re.compile(r"(?m)^\s*(?:[-*]\s+|\d+[.)]\s+)")
_CONNECTOR = re.compile(
    r"(?:그리고|그\s*다음|이후|동시에|각각|\band\b|\bthen\b|\bafter\b|\bwhile\b)",
    re.IGNORECASE,
)
_WORKSTREAM_FAMILIES = (
    re.compile(r"(?:분석|조사|리서치|inspect|analy[sz]e|research)", re.IGNORECASE),
    re.compile(r"(?:설계|계획|전략|design|plan|architect)", re.IGNORECASE),
    re.compile(r"(?:구현|수정|고쳐|리팩터|implement|modify|fix|build|refactor)", re.IGNORECASE),
    re.compile(r"(?:테스트|검증|리뷰|검토|감사|test|validate|verify|review|audit)", re.IGNORECASE),
    re.compile(r"(?:배포|출시|설치|deploy|release|publish|install)", re.IGNORECASE),
)

# These signals only justify one bounded planning decision. They never create a
# graph, select an Employee, or grant budget. The Compiler still has to state
# exact scopes and aggregation, and the Kernel may reject the proposal.
_REPLICA_PARTITION_OPPORTUNITY = re.compile(
    r"(?:"
    r"(?:전체|모든|각각의?|각\s+)\s*(?:파일|문서|모듈|패키지|로그|항목|"
    r"데이터|소스|페이지|폴더|컴포넌트|저장소|코드베이스|워크스페이스|프로젝트)"
    r"|(?:파일|문서|모듈|패키지|로그|항목|데이터|소스|페이지|폴더|컴포넌트)들"
    r"|(?:저장소|코드베이스|워크스페이스|프로젝트)\s*(?:전체|전반|곳곳)"
    r"|(?:all|every|each)\s+(?:file|document|module|package|log|item|record|"
    r"source|page|folder|component)s?"
    r"|(?:across|throughout)\s+(?:the\s+)?(?:entire|whole)\s+"
    r"(?:repository|codebase|corpus|dataset|workspace)"
    r")",
    re.IGNORECASE,
)
_REPLICA_STRUCTURED_PARTITION_OPPORTUNITY = re.compile(
    r"(?:"
    # This is deliberately narrower than _EXPLICIT_MULTI_DELIVERABLE.  A
    # request to implement two features can deserve a TEAM plan, but it is not
    # safe evidence for same-Employee replication because a replica must be
    # structurally read-only at Kernel admission.  This pattern therefore
    # recognizes only independently inspectable/researchable scopes followed
    # by an explicit integration request.
    r"(?:[^.!?\n]{1,48}(?:와|과|및|,)\s*){1,3}[^.!?\n]{1,48}"
    r"(?:을|를)?\s*(?:각각|별도(?:로)?|분리해서|나눠서)\s*"
    r"(?:분석|조사|리서치|검토|리뷰|점검|탐색)"
    r"[^.!?\n]{0,80}(?:종합|통합|결합|요약)"
    r"|(?:analy[sz]e|inspect|research|review)\s+[^.!?\n]{1,64}\s+"
    r"and\s+(?:analy[sz]e|inspect|research|review)\s+[^.!?\n]{1,64}"
    r"[^.!?\n]{0,80}(?:separately|independently|in\s+parallel)"
    r"[^.!?\n]{0,80}(?:integrate|combine|synthesi[sz]e|summari[sz]e)"
    r")",
    re.IGNORECASE,
)
_REPLICA_CANDIDATE_OPPORTUNITY = re.compile(
    r"(?:"
    r"(?:여러|복수|다수의?|두|세|네|[2-4])\s*(?:개의?|가지)?\s*"
    r"(?:후보|대안|옵션|접근법?|해법|초안|설계안|구현안)"
    r"[^.!?\n]{0,80}(?:비교|평가|선택|고르|검증)"
    r"|(?:compare|evaluate|select|choose)\s+(?:between\s+)?(?:multiple|several|"
    r"two|three|four|[2-4])\s+(?:candidates?|alternatives?|options?|approaches?|"
    r"solutions?|drafts?|designs?)"
    r"|(?:multiple|several|two|three|four|[2-4])\s+"
    r"(?:candidates?|alternatives?|options?|approaches?|solutions?|drafts?|designs?)"
    r"[^.!?\n]{0,80}(?:compare|evaluate|select|choose)"
    r")",
    re.IGNORECASE,
)
_REPLICA_DIAGNOSTIC_OPPORTUNITY = re.compile(
    r"(?:"
    r"(?:원인|실패\s*지점|병목|크래시|오류)[^.!?\n]{0,48}"
    r"(?:불명확|명확하지|알\s*수\s*없|모르|간헐|재현되지)"
    r"|(?:여러|복수|가능한)\s*(?:개의?|가지)?\s*(?:원인|가설|실패\s*경로)"
    r"[^.!?\n]{0,64}(?:진단|탐색|검증|분석)"
    r"|(?:unknown|unclear|intermittent|unreproduced)\s+"
    r"(?:cause|failure|crash|error|bottleneck)"
    r"|(?:multiple|several|possible)\s+(?:causes?|hypotheses|failure\s+paths?)"
    r"[^.!?\n]{0,64}(?:diagnose|investigate|test|analy[sz]e)"
    r")",
    re.IGNORECASE,
)
_REPLICA_DISABLED = re.compile(
    r"(?:"
    r"(?:복제|병렬(?:화)?|여러\s*(?:직원|에이전트|instance))(?:은|는|을|를)?\s*"
    r"(?:하|쓰|사용하)지\s*(?:말|마)"
    r"|한\s*(?:명|직원|에이전트|instance)(?:만|으로)"
    r"|(?:do\s+not|don['’]?t|never)\s+(?:replicate|parallelize|use\s+multiple\s+"
    r"(?:employees?|agents?|instances?))"
    r"|(?:one|single)\s+(?:employee|agent|instance)\s+only"
    r"|no\s+(?:replicas?|parallel(?:ism|ization)?)"
    r")",
    re.IGNORECASE,
)


def classify_company_input(value: str) -> CompanyOperatingDecision:
    """Classify one input without selecting staff, tools, or permissions.

    PLAN_FIRST remains evidence-triggered rather than length-triggered.  In
    PERFORMANCE_FIRST mode a concrete partition, candidate-search, or
    diagnostic opportunity is evidence: merely proving that one Employee
    *could* finish is not enough to suppress a bounded higher-value proposal.
    """

    source_text = " ".join(value.split()).strip()
    if not source_text:
        raise ValueError("Company input must be non-empty")

    effective_value = _without_negated_directives(value)
    text = " ".join(effective_value.split()).strip()
    definitional = bool(_DEFINITIONAL_QUESTION.search(text))
    effect = _requested_effect(text, definitional=definitional)
    requires_independent_review = bool(
        not definitional and _INDEPENDENT_REVIEW.search(text)
    )
    execution_replica_preference = (
        ExecutionReplicaPreference.DISABLED
        if definitional or _REPLICA_DISABLED.search(value)
        else ExecutionReplicaPreference.PERFORMANCE_FIRST
    )
    suggested_execution_replica_strategy = (
        None
        if execution_replica_preference is ExecutionReplicaPreference.DISABLED
        else _suggested_execution_replica_strategy(text)
    )
    plan_reason = (
        None
        if definitional
        else _plan_first_reason(
            effective_value,
            text,
            requires_independent_review=requires_independent_review,
            suggested_execution_replica_strategy=(
                suggested_execution_replica_strategy
            ),
        )
    )
    if plan_reason is not None:
        return CompanyOperatingDecision(
            work_mode=CompanyWorkMode.TEAM_JOB,
            coordination_policy=InitialCoordinationPolicy.PLAN_FIRST,
            requested_effect=effect,
            reason=plan_reason,
            requires_independent_review=requires_independent_review,
            execution_replica_preference=execution_replica_preference,
            suggested_execution_replica_strategy=(
                suggested_execution_replica_strategy
            ),
        )

    # A negated write still names the workspace as context, so context routing
    # reads the original input while effect and coordination read the bounded
    # positive projection.
    workspace = bool(_WORKSPACE_SIGNAL.search(source_text))
    intent = bool(_INTENT_SIGNAL.search(source_text))
    knowledge = bool(_KNOWLEDGE_SIGNAL.search(source_text))
    action = bool(
        _ACTION_SIGNAL.search(text) or _SETTINGS_CONTEXT.search(source_text)
    ) and not definitional
    if not any((workspace, intent, knowledge, action)):
        return CompanyOperatingDecision(
            work_mode=CompanyWorkMode.DIRECT,
            coordination_policy=InitialCoordinationPolicy.DIRECT,
            requested_effect=RequestedEffect.READ,
            reason=OperatingReason.DIRECT_USER_MESSAGE,
            execution_replica_preference=ExecutionReplicaPreference.DISABLED,
        )

    if workspace:
        reason = OperatingReason.WORKSPACE_CONTEXT
    elif intent:
        reason = OperatingReason.INTENT_OR_DECISION_GOAL
    elif knowledge:
        reason = OperatingReason.KNOWLEDGE_OR_EVIDENCE_GOAL
    else:
        reason = OperatingReason.ACTION_ORIENTED_GOAL
    return CompanyOperatingDecision(
        work_mode=CompanyWorkMode.SOLO_JOB,
        coordination_policy=InitialCoordinationPolicy.SOLO_FIRST,
        requested_effect=effect,
        reason=reason,
        requires_independent_review=requires_independent_review,
        execution_replica_preference=execution_replica_preference,
        suggested_execution_replica_strategy=(
            suggested_execution_replica_strategy
        ),
    )


def _requested_effect(text: str, *, definitional: bool) -> RequestedEffect:
    if definitional:
        return RequestedEffect.READ
    # A requested code/file change remains a WORKSPACE_CHANGE even if the user
    # also asks the employee to run tests.  HOST_ACTION is the non-code action
    # lane; execution policy can still expose approved command tools to either.
    if _WORKSPACE_CHANGE.search(text):
        return RequestedEffect.WORKSPACE_CHANGE
    if _HOST_ACTION.search(text):
        return RequestedEffect.HOST_ACTION
    return RequestedEffect.READ


def _plan_first_reason(
    raw: str,
    text: str,
    *,
    requires_independent_review: bool,
    suggested_execution_replica_strategy: ExecutionReplicaStrategy | None,
) -> OperatingReason | None:
    if requires_independent_review:
        return OperatingReason.INDEPENDENT_REVIEW_REQUIRED
    if suggested_execution_replica_strategy is not None:
        return OperatingReason.REPLICA_VALUE_OPPORTUNITY
    if _EXPLICIT_MULTI_DELIVERABLE.search(text):
        return OperatingReason.STRUCTURED_MULTI_WORKSTREAM

    family_count = sum(bool(pattern.search(text)) for pattern in _WORKSTREAM_FAMILIES)
    list_items = len(_LIST_ITEM.findall(raw))
    if list_items >= 3 and family_count >= 3:
        return OperatingReason.STRUCTURED_MULTI_WORKSTREAM
    if family_count >= 3 and len(_CONNECTOR.findall(text)) >= 2:
        return OperatingReason.COMPOUND_CROSS_FUNCTIONAL_GOAL
    return None


def _suggested_execution_replica_strategy(
    text: str,
) -> ExecutionReplicaStrategy | None:
    """Return one bounded opportunity class, never an executable graph.

    Candidate comparison takes precedence because it requires validator
    selection; ambiguous failure analysis is next; broad disjoint coverage is
    the remaining partition case.  The Compiler must still define scopes and
    aggregation, and the Kernel can reject the resulting proposal.
    """

    if _REPLICA_CANDIDATE_OPPORTUNITY.search(text):
        return ExecutionReplicaStrategy.CANDIDATE
    if _REPLICA_DIAGNOSTIC_OPPORTUNITY.search(text):
        return ExecutionReplicaStrategy.DIAGNOSTIC
    if _REPLICA_STRUCTURED_PARTITION_OPPORTUNITY.search(text):
        return ExecutionReplicaStrategy.PARTITION
    if _REPLICA_PARTITION_OPPORTUNITY.search(text):
        return ExecutionReplicaStrategy.PARTITION
    return None


def _without_negated_directives(value: str) -> str:
    projected = value
    for pattern in _NEGATED_DIRECTIVES:
        projected = pattern.sub(" ", projected)
    return projected
