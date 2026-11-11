---
id: NORUCT-RESEARCH-ORG-VALUE
title: "분업의 한계가치와 최소 충분 인계: 검증 프로토콜"
version: "0.1"
status: draft
type: research
authority: exploratory
last_updated: "2026-11-11"
language: ko
tags: [organization, marginal-value, handoff, contextual-bandit, evaluation]
---

# 분업의 한계가치와 최소 충분 인계: 검증 프로토콜

> **문서 역할:** 실험 프로토콜 · **상태:** v0.1 초안 · 미실행·미동결 · 결과 보고서 아님
> [전체 문서 지도](../README.md#experiments) · [연구·문헌 안내](README.md)
> [연구논문](organization_governance_manuscript.md)의 일부 명제를 비교실험으로 옮기는 설계입니다. [기록 양식](organization_experiment_record.template.json)은 관찰자료가 아닙니다.

## 1. 연구 질문과 현재 상태

[계산적 거버넌스 연구논문](organization_governance_manuscript.md)의 일부 연구명제를 측정 가능한 비교로 옮긴다.
세부 배경은 [v0.9 장문 문헌자료](organization_governance_theory.md)를 함께 본다.

**어떤 과업에서 추가 분업이 강한 단일 실행보다 나으며, 그 이득이 정보·검증·계산량 중 무엇에서 오는가?**

현재 상태는 **실행 전 프로토콜 초안**이다. 실제 데이터셋 확정·표본수 산정·프로토콜 동결·실험 실행은
완료하지 않았다. 기존 Noruct 기능을 새로 구현했다는 문서도 아니다. 아래 표의 빈 결과를 성공으로 채우지 않는다.
일반 조직론의 광범위한 논증을 대신하기보다, 먼저 검증할 세 가설로 좁힌다.

| ID | 검증할 가설 | 주 조작 | 약화 또는 반증하는 관찰 |
| --- | --- | --- | --- |
| D1 | 독립 정보나 오류 탐지 기여가 있을 때 분업의 순가치가 커진다 | 같은 추가 예산의 독립 탐색·검증 vs 재샘플링 | 분업 효과가 계산량·도구 접근을 맞추면 사라지거나 음수가 됨 |
| D2 | 최소 충분 인계가 전체 대화 인계보다 낮은 비용으로 과업 요구를 보존한다 | 인계 방식만 변경 | 누락·재조회·재작업 비용을 포함하면 품질/비용이 악화됨 |
| D3 | 관측 문맥에 따른 제한된 조직 선택이 항상 같은 구조보다 유용하다 | 고정 정책 vs 사전 학습된 선택 정책 | held-out에서 가장 좋은 고정 정책을 넘지 못하거나 선택 overhead가 이득을 상쇄 |

D1–D3은 이 프로토콜의 가설 ID다. 대표 연구논문의 P1→D1, P2→D2, P4→D3에 대응한다.
P3·P5·P6는 후속 연구 범위이며 이 프로토콜에서 검증하지 않는다. v0.9 문헌자료의 P/H 번호와는 구분한다.
에이전트 수 자체는 전문성, 독립성 또는 기여의 대리값으로 사용하지 않는다.

## 2. 측정 단위와 분업의 한계가치

한 평가 단위는 **원 요청 + 초기 상태 + 허용 도구/자료 + 권한 + 자원 상한 + 독립 채점 기준**이 고정된
task instance다. 한 task의 반복 실행은 별도 독립 과업이 아니다.

조직 O에 분할·검증·탐색 단계 e를 추가한 O+e의 한계가치는 다음 estimand로 정의한다.

```math
\Delta V_e(x)=\mathbb{E}[U(Y(O+e))-U(Y(O))\mid X=x]
```

여기서 X는 실행 전에 관찰한 과업 특성이다. Y에는 품질, 비용, 지연, 재작업, 인간 검토 부담이 포함된다.
U의 환산 기준은 실험 전에 정한다. 사후에 가중치를 바꿔 좋은 결과를 만들지 않는다.

### 2.1 총비용을 두 번 세지 않는다

토큰·도구·검증·통합·학습/review를 단계별로 분류하더라도 총합은 한 번만 계산한다.
wall time과 worker 시간을 구분하고 병렬 worker 시간의 합을 wall time이라 부르지 않는다.
인간 검토 대기와 실제 검토 시간도 분리한다. 비용을 돈으로 환산할 때는 모델별 단가·cached token·
reasoning token·청구 범위가 확인되어야 한다. 확인되지 않은 token 감소를 청구 금액 절감으로 바꾸지 않는다.

공통 화폐 단위가 불가능하면 임의의 종합점수보다 품질·token·wall time·인간 검토 시간의 원래 단위와
파레토 비교를 먼저 보고한다. 보안·권한·필수 요구는 비용과 교환 가능한 보상항이 아니라 admissibility
제약이다. 금지행위를 하는 값싼 조직은 선택 후보에서 제외한다.

### 2.2 성공한 실행만 비교하지 않는다

실패·timeout·취소·도구 장애도 사전 정의한 분모에 포함한다. 외부 중단과 모델 실패는 원인을 나누되
실패 arm을 조용히 삭제하지 않는다. 품질을 채점할 수 없으면 null과 사유를 기록한다.
완료까지 시간을 주 결과로 쓰는 경우 중도절단 규칙도 동결한다.

한 worker의 산출물이 최종 답에 인용됐다는 사실은 인과 기여의 증거가 아니다. 단계 제거 ablation이나
무작위 대조가 필요하다. 한 번의 성공 실행에서 생성한 “이 agent 덕분” 설명은 사후 귀속 가설로 남긴다.

## 3. 최소 충분 인계

정보를 짧게 만드는 목적은 다음 실행자가 **같은 요구와 권한을 보존한 채 다음 결정을 할 수 있게 하는 것**이다.
원문을 모두 전달하지 않았다는 사실만으로 좋은 압축이라 부르지 않는다.

| 필드 | 최소 내용 | 누락 시 위험 |
| --- | --- | --- |
| 목적·요구 | 원 요청 참조, 이번 단계의 완료 조건, 반드시 보존할 제약 | 목표 변형 |
| 현재 상태 | 완료·미완료·불명 상태, artifact revision | 중복작업·stale 입력 |
| 근거 | 주장별 원천 위치, 확인된 사실과 가설, 접근 가능한 참조 | 근거 세탁 |
| 권한·금지 | 허용한 행동·데이터 범위, 승인 필요 조건 | 권한 확대 |
| 다음 행동 | 의존성, 산출물 소유자, 통합·검증 경로 | 충돌·종료 불일치 |
| 실패·복구 | 이미 시도한 접근, 미해결 반례, 재조회·상향 조건 | 반복 실패 |

원문 조회는 별도 권한 검사를 통과해야 한다. 원문 pointer를 안다고 원문 전체를 읽을 권한이 생기지 않는다.
private source, 고객 데이터와 raw 대화는 공개 연구 기록에 넣지 않는다.

세 비교 arm은 다음과 같다.

- FULL: 허용된 관련 원문/대화 전체를 인계한다. context 한도 초과는 숨기지 않고 실행 불가/실패로 기록한다.
- SUMMARY: PACKET과 같은 token 상한의 자유서술 요약 + 같은 조건의 원문 조회.
- PACKET: 위 구조를 가진 제한된 packet + 필요시 근거 원문 조회.

세 arm 모두 같은 허용 원문을 같은 retriever·호출 권한으로 다시 조회할 수 있다. SUMMARY와 PACKET은
초기 인계 token 상한도 맞춘다. 자료 접근권·초기 task·모델을 고정하고 모든 arm의 재조회 비용을 포함한다.
SUMMARY와 PACKET의 차이는 정보의 구성 방식이며 조회 기능의 유무가 아니다. 원문 조회를 PACKET에만
허용하는 별도 실험은 구조화+조회의 결합 개입으로 표시한다.
주 지표는 독립 채점의 요구 보존율, 근거 오류율, 권한 위반, 전체 token과 재작업이다.
단순 packet 길이 또는 요약문 유사도는 충분성의 증거가 아니다.

검증 기준은 요구 보존의 사전 정의 비열등성 한계, 비용 우위, 안전 제약을 모두 만족하는지다.
엄격한 제약의 허용 손실은 0으로 고정한다. 일반 품질의 허용 한계는 과업 책임자가 동결해야 하며,
이번 문서가 임의로 정하지 않는다. “최소”는 시험한 표현 집합 안에서만 주장한다.

## 4. 조직 선택의 contextual bandit 근사

[Li et al. (2010)](https://arxiv.org/abs/1003.0146v2)의 contextual-bandit 문제 설정을
조직 선택에 적용할 수 있는지는 연구 가설이다. 해당 논문의 뉴스 추천 성과를 agent 조직 효과로 이전하지 않는다.

초기 실험에서 action은 무제한 graph 생성이 아니라 **사전에 검토된 소수 조직 template 중 하나의 선택**이다.
예를 들면 SOLO, 동일 계산량 앙상블, 고정 검증 포함 TEAM이다. 독립성·권한·예산을 만족하지 않는 action은
admissible set에서 먼저 제거한다. “더 싸다”는 이유로 필수 독립 검증이나 사용자의 명시 분업 요구를 없애지 않는다.

- context: 사전 판정한 분해 가능성, 정보 분산, 문맥 결합, 검증 가능성, 위험 등. 정답·종료 신호는 넣지 않는다.
- action: 선택한 exact template와 모델/도구/Skill revision.
- reward: 사전에 고정한 품질·비용 기준. 관측 기간이 끝나지 않은 결과는 pending이다.
- log: 전체 admissible 후보 집합, 선택확률, 정책 revision, 실제 노출, 중단·실패와 지연 피드백.

한 Job 시작 시 한 번 template를 고르고 끝의 결과를 받는 제한 문제는 bandit으로 근사할 수 있다.
그러나 중간 검색·질문·분업·압축이 미래 정보와 상태를 바꾸고, 여러 Job의 기억에 영향을 주면
단일 단계 근사가 놓치는 효과가 있다. 그때는 순차 의사결정/MDP 계열 또는 별도의 종단 설계가 필요하다.
context를 매 단계 기록한다고 장기 credit assignment 문제가 저절로 없어지지 않는다.

초기 안전 순서는 **관찰 → offline 평가 → shadow 추천 → 승인된 격리 실험**이다.
실사용자의 비가역 행동을 탐색 arm으로 실행하지 않는다. 이 연구 노트는 production 자동 선택을 승인하지 않는다.

### 4.1 기존 로그로 알 수 없는 것

선택한 action의 성공만으로 선택하지 않은 action의 보상을 알 수 없다. 후보 집합과 행동확률이 없는
관찰 로그에는 “기존 정책보다 나았을 것”이라는 반사실 값을 채우지 않는다.

IPS/DR 평가를 계획한다면 정책의 support overlap, propensity의 정확성, 결과의 누락기제, 일관된 action 정의를
확인한다. propensity가 0인 action은 로그가 뒷받침하지 않으며 임의의 작은 양수로 고치지 않는다.
중요도 가중치·유효표본수·clipping 규칙과 민감도도 보고한다.
[Dudík et al. (2011)](https://arxiv.org/abs/1103.4601v2)의 doubly robust 방법은
보상 모형과 행동정책 모형을 결합하지만, 없는 support나 측정하지 않은 개입을 만들어 주는 방법은 아니다.
보상모형 fitting과 평가 group을 분리하고 모델·작업 분포 변화에 따른 불안정성을 기록한다.

### 4.2 결정 전 context·보상 정의·support의 복원 계약

실제 평가 record에는 다음 세 묶음이 필수다. template의 null은 미실행 표시이며 평가 적격성을 뜻하지 않는다.

1. `decision_context`: 결정 전에 관측한 feature 실제값과 관측 시각, feature schema revision을 보존한다.
   실제값은 inline 또는 접근 통제된 immutable 참조+digest로 남긴다. digest만 있고 값을 읽을 수 없으면
   평가 context를 복원한 것이 아니다. 정답·종료 후 정보로 빈 feature를 보충하지 않는다.
2. `protocol.reward_definition`: outcome을 reward로 바꾸는 고정 transform·revision·digest와 weight
   참조·digest를 결속한다. 단위·정규화·결측/지연 결과 처리도 그 정의에 포함하고, outcome을 보기 전에 동결한다.
3. `assignment.logging_policy_support`: 후보별 전체 logging 확률표 또는 exact logger
   artifact·configuration·결정 시점 state를 context와 함께 복원할 수 있는 참조+digest를 남긴다.
   복원 검증 전에는 `reconstruction_verified=false`다. seed나 정책 이름만으로 확률을 복원했다고 하지 않는다.

선택된 action의 `selection_probability` 하나는 IPS 분모에 충분하지만, 다른 후보의 support까지 입증하지 않는다.
전체 확률표는 중단/선택 보류를 포함해 당시 정책이 선택할 수 있었던 action을 빠짐없이 식별하고 합이 1이어야 한다.
선택 action의 확률은 scalar와 일치해야 한다. 복원 경로도 같은 확률분포를 재구성해야 한다.

**허용 집합과 통계적 support는 다르다.** 권한상 허용된 action이어도 logger의 선택확률은 0일 수 있다.
평가 정책이 양의 확률을 주는 action에는 해당 context에서 logger도 양의 확률을 주어야 한다.
알려진 결정론 logger는 선택 action이 1, 나머지가 0이다. 이는 결측이 아니지만, 선택하지 않는 action의
효과를 일반적인 IPS/DR로 식별하지는 못한다. 반대로 확률을 모르는 기록에 1/0·균등확률·작은 양수를
임의로 넣지 않는다. null과 결측 사유를 보존하고 해당 평가를 부적격으로 표시한다.

[Sachdeva et al. (2020)](https://arxiv.org/abs/2006.09438v1)은 support가 부족한 로그의 문제와
action/policy 범위 제한 또는 보상 외삽 접근을 분석한다. 이 프로토콜에서는 먼저 평가 정책을 검증된
support로 제한하고 제외 범위를 보고한다. 미관측 영역의 외삽은 추가 가정과 민감도 분석이 필요한
별도 추정이며, 관측으로 검증된 인과 효과와 섞지 않는다.

## 5. MAO-ARAG와의 공정한 위치 설정

[Chen et al., MAO-ARAG (2025), v1](https://arxiv.org/html/2508.01005v1)의
§3–4는 RAG 모듈을 조합하는 planner, multi-agent semi-Markov 정식화, PPO와
F1·비용·형식 penalty를 제시한다. 순차·병렬 query decomposition을 모두 포함하며
QA 벤치마크에서 품질과 token·검색 호출·turn 비용을 평가한다.
따라서 “동적 분업” 또는 “품질–비용을 학습하는 planner” 자체를 이 노트의 새로운 발명으로 주장할 수 없다.

| 비교 축 | MAO-ARAG 원문 범위 | 이 연구에서 추가로 검증할 질문 |
| --- | --- | --- |
| action | RAG executor 조합, 복수 turn | 제한된 조직 template와 추가 단계의 한계가치 |
| 목표 | QA의 F1와 비용/형식 penalty | 요구 보존·재작업·인간 검토까지 포함한 순가치 |
| 상태 | query/subquery 중심 planner 입력 | 권한·artifact revision·정보 접근을 고정한 과업 문맥 |
| 학습 단위 | PPO로 planner 훈련 | 먼저 one-shot bandit 근사 및 로그 적격성 검사 |
| 평가 범위 | 원문 QA 설정 | 코드·문서·정보검색 등 별도 업무 범위의 통제 실험 |

오른쪽 열은 입증된 우위가 아니라 추가 검증 범위다. 원문이 다루지 않은 거버넌스나 장기 기억을
측정하지 않았다는 이유로 MAO-ARAG가 열등하다고 결론내리지 않는다.
이 문서는 논문을 대조했으며, 원 코드 재현·동일 조건 실험은 하지 않았다.

직접 비교는 공유 QA subset, 같은 corpus/retriever·executor·평가자·자원 envelope를 확보한 뒤에만 한다.
원래 학습 가중치/훈련비가 다르면 보고하고, 변경한 구현은 “MAO-ARAG-inspired”라고 표시한다.
서로 다른 논문의 숫자를 나란히 놓는 leaderboard는 이 프로토콜의 증거가 아니다.

## 6. 통제 실험: 한 번에 한 효과만

### 6.1 과업과 비교 arm

과업은 먼저 독립 병렬 정보수집, 강결합 수정/통합, 검증이 필요한 산출물의 세 층으로 나눈다.
출처와 difficulty를 결과를 보기 전에 고정하고, 비슷한 과업·같은 repo·같은 생성 template를 한 group에 묶는다.
학습/개발/최종 평가 사이에 group을 나누고 시간순 holdout도 둔다.

| Arm | 실행 형태 | 주로 분리하는 효과 |
| --- | --- | --- |
| S | 강한 단일 실행 + 동일 허용 도구 | 기본 능력 |
| E | 독립 재샘플링 + 고정 선택자 | 추가 계산·탐색 효과 |
| F | 고정 분업 + 고정 통합/검증 | 조직화의 추가 효과 |
| A | 실행 전 context로 S/E/F 선택 | 선택 정책 효과 |

S가 분업에 필요한 도구를 못 쓰게 만들지 않는다. 비교 목적상 필요한 access 차이는 별도 실험 요인으로 선언한다.
모든 arm의 planner·selector·review·실패·재시도 비용을 포함한다.

두 estimand를 구분한다.

1. **구조 효과:** 같은 모델/도구와 같은 자원 상한 아래 F vs S/E를 비교한다. 상한이 같다는 것이
   실제 소비가 같다는 뜻은 아니다. budget 수준별 곡선과 실제 소비를 함께 보고한다.
2. **정책 효과:** held-out에서 A vs 최선의 사전 선택 고정 정책을 비교한다. 최종 평가 결과를 보고
   최선 고정 정책을 다시 고르지 않는다. template 후보 집합과 meta-policy 학습비도 공개한다.

추가 계산 자체를 통제하려면 품질–실제 비용 곡선의 공통 지지 구간에서 비교한다.
모델·Skill·runtime 버전을 동시에 바꾼 비교는 **결합 개입 효과**로만 표시한다.
선택 head만의 효과를 주장하려면 같은 runtime·후보 Skill·학습 상태에서 head on/off를 비교해야 한다.

### 6.2 작은 실험부터 순서대로

1. **계측 점검:** 각 arm의 요구·token·도구·시간·노출 기록이 끝까지 연결되는지 확인한다. 성능 판정에 쓰지 않는다.
2. **분업 대조:** S/E/F와 과업 층의 상호작용을 평가한다. 좋은 F만 골라 사후 보고하지 않는다.
3. **인계 ablation:** 동일한 F에서 FULL/SUMMARY/PACKET만 바꾼다.
4. **선택 정책 대조:** 개발 group으로 정책을 고정한 뒤 보지 않은 group에서 A를 평가한다.
5. **장기 기억 대조:** 동결한 실행 조직에서 memory/Skill의 on/off만 바꾸고 오염·이월 효과를 따로 검사한다.

일괄 대형 factorial을 먼저 돌리기보다 주효과와 측정 가능성을 확보한다.
실험 순서·sample size·중단 규칙·비열등성 한계는 실행 전에 등록한다.
현재 이 값들이 확정되지 않았으므로 “사전 등록 완료”라고 부르지 않는다.

### 6.3 통계와 판정

- 같은 task와 초기 snapshot을 여러 arm에 배정하고, 실행 순서는 무작위화/균형화한다.
  동일 seed가 원격 모델의 동일 난수를 보장한다고 주장하지 않는다.
- 독립 task/group 단위 paired 차이와 95% 구간을 보고한다. 반복 run만 bootstrap해 유효표본을 부풀리지 않는다.
- 표본수는 별도 pilot 분산·최소 탐지 효과·비열등성 한계로 정한다. pilot을 최종 held-out에 재사용하지 않는다.
- 채점자는 가능하면 arm을 모르게 하고, deterministic acceptance와 의미 품질을 분리한다.
  평가자 불일치·사후 rubric 수정·결측을 기록한다.
- 주 endpoint와 confirmatory contrast를 먼저 지정한다. 다른 지표·하위집단은 탐색 결과로 표시하고
  다중비교 교정 또는 계층적 검정 계획을 동결한다.
- 안전 사건 0건은 안전성 증명이 아니다. 분모·검출 방식·관찰 구간과 불확실성을 함께 보고한다.

결과 분류는 POSITIVE, NEGATIVE, INCONCLUSIVE, MIXED, NOT_RUN으로 남긴다.
“통계적으로 유의하지 않음”을 동등성으로 바꾸지 않는다. 비열등성도 별도 설계·구간이 필요하다.

## 7. 최소 evidence record

[기계가 읽을 수 있는 template](organization_experiment_record.template.json)을 사용한다.
현재 값은 모두 미실행 placeholder다. 결과로 집계해서는 안 된다.

필수 결속은 task/group/split, 초기 snapshot, 프로토콜 revision, runtime·model·Skill pin,
결정 전 context 실제값·schema revision, 고정 reward transform·weights, 후보 집합, 배정/선택확률,
후보별 logger support 복원 정보, 실제 노출, 결과·비용, 검증 방식, 누락 사유다.
행동확률은 설계/정책이 실제 사용한 값만 기록한다. 모르면 null을 남긴다.

원문 입력과 사용자 데이터는 접근 통제된 로컬 artifact에 둔다. 공개 record에는 비식별 task ID,
동의된 집계와 검토된 fixture만 사용한다. hash는 무결성 표지이며 익명화나 원본 공개 권한을 대신하지 않는다.

## 8. 주장과 근거 관리

| 주장 | 현재 근거 등급 | 현재 허용되는 결론 | 다음 필요한 근거 |
| --- | --- | --- | --- |
| 최소 충분 조직이 유용한 설계 원칙이다 | 이론적 해석 | 조직 추가의 비용·이득을 함께 보자 | 과업별 비교 |
| 동적 RAG 조합을 학습할 수 있다 | 외부 논문 관찰 | MAO-ARAG의 연구 조건에서 보고됨 | 독립 재현/공통 조건 비교 |
| 분업의 한계가치를 미리 예측할 수 있다 | D1/D3 가설 | 측정·평가 대상 | 독립 held-out, 정책 대조 |
| PACKET이 가장 효율적이다 | D2 가설 | 결론 없음 | 인계 대조, 누락/재조회 포함 |
| bandit이 충분한 조직 모델이다 | 제한된 근사 가설 | 한 번의 template 선택에 한정 | 순차·장기 효과 민감도 |
| Noruct의 조직 구조가 일반적으로 우수하다 | 미입증 | 주장하지 않음 | 독립 재현 가능한 제품/사용자 연구 |

private 개발 영수증은 공개 재현 결과로 취급하지 않는다. 후속 공개에는 데이터 권리·비식별화·
접근 가능한 fixture·정확한 실행 조건·부정 결과를 포함한 별도 검토가 필요하다.
이론 원고의 다른 인용 전체를 이번 수정에서 검증한 것은 아니다.

## 9. 출처와 변경 이력

- Chen, Y., Zhang, E., Yan, L., Wang, S., Huang, J., Yin, D., & Mao, J. (2025).
  [MAO-ARAG](https://arxiv.org/abs/2508.01005v1). 대조 범위: 본문 §3–4; 코드 미실행.
- Li, L., Chu, W., Langford, J., & Schapire, R. E. (2010).
  [A Contextual-Bandit Approach to Personalized News Article Recommendation](https://arxiv.org/abs/1003.0146v2).
  WWW 2010. 대조 범위: 서지·문제 설정; agent 적용은 본 노트의 가설.
- Dudík, M., Langford, J., & Li, L. (2011).
  [Doubly Robust Policy Evaluation and Learning](https://arxiv.org/abs/1103.4601v2).
  ICML 2011. 대조 범위: 서지·초록; 구현·성능 재현 없음.

- Sachdeva, N., Su, Y., & Joachims, T. (2020).
  [Off-policy Bandits with Deficient Support](https://arxiv.org/abs/2006.09438v1).
  KDD 2020. 대조 범위: 서지·초록의 support 부족과 접근 구분; 알고리즘 재현 없음.

2026-11-11: 기존 v0.7 논증의 저자 관점과 장 구성을 보존하며 검증 단위를 구체화했다.
기존 원고를 새 저작으로 대체하지 않으며 새 가설·프로토콜은 검토 전 연구 초안이다.

2026-11-11 후속 검토: context·reward·logger support의 기록 계약을 보완했다. 기존 로그의 빈 확률을 생성하거나 실험을 실행하지 않았다.
