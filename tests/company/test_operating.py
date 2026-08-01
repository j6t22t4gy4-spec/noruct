from __future__ import annotations

import unittest

from dynamic_firm.company.operating import (
    CompanyWorkMode,
    InitialCoordinationPolicy,
    OperatingReason,
    RequestedEffect,
    classify_company_input,
)
from dynamic_firm.kernel.models import (
    ExecutionReplicaPreference,
    ExecutionReplicaStrategy,
)


class CompanyOperatingTests(unittest.TestCase):
    def test_every_input_is_company_owned_including_direct_conversation(self) -> None:
        decision = classify_company_input("hello")

        self.assertTrue(decision.company_owned)
        self.assertEqual(decision.work_mode, CompanyWorkMode.DIRECT)
        self.assertEqual(
            decision.coordination_policy,
            InitialCoordinationPolicy.DIRECT,
        )
        self.assertEqual(decision.requested_effect, RequestedEffect.READ)
        self.assertEqual(decision.reason, OperatingReason.DIRECT_USER_MESSAGE)
        self.assertEqual(
            decision.execution_replica_preference,
            ExecutionReplicaPreference.DISABLED,
        )

    def test_simple_goals_start_solo_and_keep_effect_separate(self) -> None:
        cases = {
            "이 저장소 구조를 분석해줘": (
                RequestedEffect.READ,
                OperatingReason.WORKSPACE_CONTEXT,
            ),
            "로그인 기능을 구현해줘": (
                RequestedEffect.WORKSPACE_CHANGE,
                OperatingReason.ACTION_ORIENTED_GOAL,
            ),
            "Run the test command": (
                RequestedEffect.HOST_ACTION,
                OperatingReason.ACTION_ORIENTED_GOAL,
            ),
        }
        for value, (effect, reason) in cases.items():
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.SOLO_FIRST,
                )
                self.assertEqual(decision.requested_effect, effect)
                self.assertEqual(decision.reason, reason)
                self.assertEqual(
                    decision.execution_replica_preference,
                    ExecutionReplicaPreference.PERFORMANCE_FIRST,
                )

    def test_concrete_replica_value_signals_aggressively_start_planning(self) -> None:
        cases = {
            "이 저장소의 모든 파일을 분석하고 결과를 종합해줘": (
                ExecutionReplicaStrategy.PARTITION
            ),
            (
                "API와 CLI, TUI의 현재 동작을 각각 분석하고 결과를 종합해줘"
            ): ExecutionReplicaStrategy.PARTITION,
            "여러 후보안을 만들고 비교해서 가장 좋은 안을 골라줘": (
                ExecutionReplicaStrategy.CANDIDATE
            ),
            "원인이 불명확한 크래시를 진단하고 수정해줘": (
                ExecutionReplicaStrategy.DIAGNOSTIC
            ),
        }
        for value, strategy in cases.items():
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.PLAN_FIRST,
                )
                self.assertEqual(
                    decision.reason,
                    OperatingReason.REPLICA_VALUE_OPPORTUNITY,
                )
                self.assertEqual(
                    decision.execution_replica_preference,
                    ExecutionReplicaPreference.PERFORMANCE_FIRST,
                )
                self.assertEqual(
                    decision.suggested_execution_replica_strategy,
                    strategy,
                )

    def test_effectful_multi_feature_request_does_not_fake_a_replica_opportunity(self) -> None:
        decision = classify_company_input(
            "API와 CLI 기능을 각각 구현하고 결과를 통합해줘"
        )

        self.assertEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
        self.assertEqual(
            decision.reason,
            OperatingReason.STRUCTURED_MULTI_WORKSTREAM,
        )
        self.assertEqual(decision.requested_effect, RequestedEffect.WORKSPACE_CHANGE)
        self.assertIsNone(decision.suggested_execution_replica_strategy)

    def test_explicit_single_execution_disables_replica_proposal(self) -> None:
        decision = classify_company_input(
            "병렬로 하지 말고 한 명만 사용해서 모든 파일을 분석해줘"
        )

        self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(
            decision.execution_replica_preference,
            ExecutionReplicaPreference.DISABLED,
        )
        self.assertIsNone(decision.suggested_execution_replica_strategy)

    def test_code_change_remains_workspace_effect_when_tests_are_also_requested(self) -> None:
        decision = classify_company_input("코드를 수정하고 테스트를 실행해줘")

        self.assertEqual(decision.requested_effect, RequestedEffect.WORKSPACE_CHANGE)
        self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)

    def test_only_strong_compound_signals_start_plan_first(self) -> None:
        cases = {
            "로그인을 구현하고 별도 검토로 보안을 검증해줘": (
                OperatingReason.INDEPENDENT_REVIEW_REQUIRED
            ),
            "Research the issue, then design a fix, and finally implement and test it": (
                OperatingReason.COMPOUND_CROSS_FUNCTIONAL_GOAL
            ),
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.PLAN_FIRST,
                )
                self.assertEqual(decision.reason, reason)

    def test_team_or_parallel_wording_cannot_override_minimal_coordination(self) -> None:
        for value in (
            "저장소 분석을 여러 에이전트에게 맡겨줘",
            "이 파일 검토를 병렬로 진행해줘",
            "Use multiple agents to analyze this repository",
            "Form a team and inspect this file in parallel",
            "조사와 구현을 여러 에이전트가 병렬로 맡게 해줘",
        ):
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.SOLO_FIRST,
                )

    def test_korean_negated_coordination_and_effects_are_not_reversed(self) -> None:
        solo_cases = (
            "병렬로 하지 말고 한 명이 저장소를 분석해줘",
            "여러 에이전트를 쓰지 말고 혼자 코드를 분석해줘",
        )
        for value in solo_cases:
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.SOLO_FIRST,
                )
                self.assertFalse(decision.requires_independent_review)

        no_review = classify_company_input("독립 검토는 하지 말고 구현만 해줘")
        self.assertEqual(no_review.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(
            no_review.requested_effect,
            RequestedEffect.WORKSPACE_CHANGE,
        )
        self.assertFalse(no_review.requires_independent_review)

        no_write = classify_company_input("파일을 수정하지 말고 분석만 해줘")
        self.assertEqual(no_write.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(no_write.requested_effect, RequestedEffect.READ)
        self.assertFalse(no_write.requires_independent_review)

    def test_english_negated_coordination_and_effects_are_not_reversed(self) -> None:
        cases = (
            "Do not work in parallel; use one employee to analyze this repository",
            "Don't use multiple agents; work solo and inspect the codebase",
            "Do not independently review; just implement the requested fix",
            "Do not modify the file; only analyze src/example.py",
        )
        for value in cases:
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertNotEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
                self.assertNotEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.PLAN_FIRST,
                )
                self.assertFalse(decision.requires_independent_review)

        no_write = classify_company_input(cases[-1])
        self.assertEqual(no_write.requested_effect, RequestedEffect.READ)

    def test_positive_independent_review_is_an_explicit_plan_constraint(self) -> None:
        decision = classify_company_input(
            "로그인 변경안을 독립적으로 검토한 뒤 구현해줘"
        )

        self.assertEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
        self.assertEqual(
            decision.coordination_policy,
            InitialCoordinationPolicy.PLAN_FIRST,
        )
        self.assertTrue(decision.requires_independent_review)
        self.assertEqual(
            decision.reason,
            OperatingReason.INDEPENDENT_REVIEW_REQUIRED,
        )

    def test_explicit_independent_deliverables_start_with_a_bounded_team_plan(self) -> None:
        cases = (
            "백엔드 API와 프론트 화면을 각각 구현하고 통합해줘",
            "독립된 보고서 두 개를 각각 조사해줘",
            "Implement the backend and frontend separately, then integrate them",
        )
        for value in cases:
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.work_mode, CompanyWorkMode.TEAM_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.PLAN_FIRST,
                )
                self.assertEqual(
                    decision.reason,
                    OperatingReason.STRUCTURED_MULTI_WORKSTREAM,
                )

    def test_negated_settings_change_remains_a_read_request(self) -> None:
        for value in (
            "설정을 바꾸지 말고 현재 값만 확인해봐",
            "Do not change the settings; only inspect the current value",
        ):
            with self.subTest(value=value):
                decision = classify_company_input(value)
                self.assertEqual(decision.requested_effect, RequestedEffect.READ)
                self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
                self.assertEqual(
                    decision.coordination_policy,
                    InitialCoordinationPolicy.SOLO_FIRST,
                )

    def test_length_and_two_ordinary_steps_do_not_create_a_team(self) -> None:
        decision = classify_company_input(
            "이 저장소를 분석하고 결과를 간결하게 정리해줘. " + "배경 설명. " * 800
        )

        self.assertEqual(decision.work_mode, CompanyWorkMode.SOLO_JOB)
        self.assertEqual(
            decision.coordination_policy,
            InitialCoordinationPolicy.SOLO_FIRST,
        )

    def test_definitional_mutation_word_does_not_request_mutation(self) -> None:
        decision = classify_company_input("리팩터링이 뭐야?")

        self.assertEqual(decision.work_mode, CompanyWorkMode.DIRECT)
        self.assertEqual(decision.requested_effect, RequestedEffect.READ)

        team_definition = classify_company_input("병렬 팀이 어떻게 작동해?")
        self.assertEqual(team_definition.work_mode, CompanyWorkMode.DIRECT)
        self.assertEqual(team_definition.requested_effect, RequestedEffect.READ)

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            classify_company_input("  \n")


if __name__ == "__main__":
    unittest.main()
