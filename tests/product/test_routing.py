from __future__ import annotations

import unittest

from dynamic_firm.product.routing import InputRoute, route_interactive_input


class InteractiveRoutingTests(unittest.TestCase):
    def test_greetings_and_ordinary_questions_take_the_direct_lane(self) -> None:
        for value in (
            "hello",
            "이름이 뭐야?",
            "파이썬 리스트가 뭐야?",
            "How does a hash table work?",
        ):
            with self.subTest(value=value):
                decision = route_interactive_input(value)
                self.assertEqual(decision.route, InputRoute.CONVERSATION)
                self.assertEqual(decision.reason, "DIRECT_USER_MESSAGE")

    def test_workspace_and_action_goals_take_the_company_lane(self) -> None:
        cases = {
            "이 저장소 구조를 분석해줘": "WORKSPACE_CONTEXT",
            "로그인 기능을 구현해줘": "ACTION_ORIENTED_GOAL",
            "Compare three deployment strategies": "ACTION_ORIENTED_GOAL",
            "Inspect src/dynamic_firm/cli.py": "WORKSPACE_CONTEXT",
            "caffeinate 실행해줘": "ACTION_ORIENTED_GOAL",
            "Run the test command": "ACTION_ORIENTED_GOAL",
            "설정을 바꾸지 말고 현재 값만 확인해봐": "ACTION_ORIENTED_GOAL",
            "백엔드 API와 프론트 화면을 각각 구현하고 통합해줘": (
                "STRUCTURED_MULTI_WORKSTREAM"
            ),
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                decision = route_interactive_input(value)
                self.assertEqual(decision.route, InputRoute.COMPANY_GOAL)
                self.assertEqual(decision.reason, reason)

    def test_definitional_question_is_not_mistaken_for_an_action_goal(self) -> None:
        decision = route_interactive_input("리팩터링이 뭐야?")
        self.assertEqual(decision.route, InputRoute.CONVERSATION)

    def test_knowledge_and_intent_language_enters_the_company_bridge(self) -> None:
        cases = {
            "이 PDF를 가격 전략 지식에 넣어줘": "KNOWLEDGE_OR_EVIDENCE_GOAL",
            "이 근거를 바탕으로 다음 결정을 재검토해줘": "INTENT_OR_DECISION_GOAL",
        }
        for value, reason in cases.items():
            with self.subTest(value=value):
                decision = route_interactive_input(value)
                self.assertEqual(decision.route, InputRoute.COMPANY_GOAL)
                self.assertEqual(decision.reason, reason)

    def test_strong_compound_goal_projects_to_the_legacy_company_route(self) -> None:
        decision = route_interactive_input(
            "Research the issue, then design a fix, and implement and test it"
        )

        self.assertEqual(decision.route, InputRoute.COMPANY_GOAL)
        self.assertEqual(decision.reason, "COMPOUND_CROSS_FUNCTIONAL_GOAL")

    def test_long_input_alone_does_not_force_a_company_job(self) -> None:
        decision = route_interactive_input("hello " * 300)

        self.assertEqual(decision.route, InputRoute.CONVERSATION)
        self.assertEqual(decision.reason, "DIRECT_USER_MESSAGE")


if __name__ == "__main__":
    unittest.main()
