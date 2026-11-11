# AI 에이전트 조직 연구

인간 조직설계·조직경제학·전통적 다중에이전트 시스템과 LLM 에이전트 연구를 연결한 한국어 연구자료다.
연구논문, 장문 문헌자료와 실험 프로토콜을 구분해 보관한다. 제품 규범과 현재 구현은 개념문서를 따른다.

## 연구논문

[AI 에이전트 조직의 계산적 거버넌스](organization_governance_manuscript.md)

분업과 조정의 설계 및 인간의 잔여 통제권을 다루는 이론 구축형 working paper다.
초록, 서론, 연구 접근, 이론적 기반, 개념모형, 연구명제, 실험설계, 논의, 결론과 참고문헌으로 구성한다.
v0.6의 이론적 논증을 이어가며, v0.9에서 보강한 권한 구분과 분업·인계·선택 정책의 검증 설계를 통합했다.
자체 실험 결과는 포함하지 않는다.

## 원문과 장문 문헌자료

- [v0.6 논문 원문](https://github.com/j6t22t4gy4-spec/noruct/blob/35124074/docs/literature/organization_governance_theory.md): 초록과 21개 장으로 작성된 최초 장문 연구논문. 당시의 문장과 서지정보를 그대로 확인할 수 있다.
- [v0.9 장문 문헌자료](organization_governance_theory.md): 이후의 용어·설계 예시·세부 명제와 근거 한계를 포함한 자료. 기존 파일은 수정하지 않고 보존한다. 포함된 모든 인용과 수치의 독립 검증이 완료된 것은 아니다.

새 연구논문은 논증을 연결한 본문이며, 장문 자료는 세부 논점과 이전 판본을 대조하는 데 사용한다.
원문의 개별 실험 수치를 새 원고의 검증 결과로 옮기지 않았다.

## 실험 프로토콜

- [분업의 한계가치와 최소 충분 인계 검증 프로토콜](organization_value_experiment.md): 첫 비교실험의 세 가설, MAO-ARAG와의 비교, 대조조건과 반증 기준.
- [실험 기록 양식](organization_experiment_record.template.json): 후보 집합, 선택 전 문맥, 실제 노출, 품질·비용과 결측의 기록 형식.

프로토콜은 미실행 초안이다. 과업군·표본수·주 지표·예산과 중단 규칙을 확정한 뒤 사전등록과 실행을 진행할 수 있다.
새 연구논문의 P1·P2·P4는 프로토콜의 D1·D2·D3에 각각 대응하며, P3·P5·P6는 후속 연구명제다.
비공개 개발 기록과 제품 구현 상태는 공개 연구의 성능 증거에 포함하지 않는다.

## Noruct 개념문서

연구의 제품 설계상 해석은
[조직은 직책이 아니라 의사결정 구조다](../ko/09-organization-as-decision-architecture.md)와
[Model Intelligence와 다중 Provider 오케스트레이션](../ko/10-model-intelligence-and-multi-provider-orchestration.md)을 참고한다.
