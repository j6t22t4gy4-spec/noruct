# 09 — 조직은 직책이 아니라 의사결정 구조다

> 정본: [English](../en/09-organization-as-decision-architecture.md) · [← 현재 개발 상태](08-current-development-status.md) · [문서 인덱스](README.md)

## 중심 명제

조직은 직책의 목록이 아닙니다. 조직은 업무, 정보, 권한, 조정, 검증과 학습을 배치하는 실행 구조입니다.

여러 model instance에 관리자, 조사자, 검토자라는 이름을 붙인다고 조직이 되지는 않습니다. 서로 다른 업무,
정보, capability, permission, 산출물, 검증 책임과 예외 상향 경로가 실제로 부여될 때만 역할명이 구조가 됩니다.

따라서 Noruct는 조직설계를 **의사결정 구조의 설계**로 봅니다.

> **가역적인 인지활동은 분산시키고, 공유 상태는 명시적으로 연결하며, 불가역적인 commit은 단일화하고,
> 검증은 독립시키며, 요청별 실행조직만 동적으로 바꿉니다.**

이 문서는 개념 모델입니다. 모든 메커니즘이 일반 제공된다는 주장이나, 관리형 팀이 강한 단일 Employee보다
언제나 낫다는 성능 주장이 아닙니다.

## 구분해야 할 네 수준

“멀티에이전트”라는 말에는 조직적 깊이가 전혀 다른 시스템이 섞여 있습니다.

| 수준 | 구조 | Noruct에서의 의미 |
|---|---|---|
| 0 — 앙상블 | 같은 문제를 독립 생성한 뒤 투표·선택 | 제한된 candidate replica는 Team이 아닌 앙상블일 수 있습니다 |
| 1 — 워크플로 | model·tool node의 순서가 미리 정해짐 | frozen Job Graph는 조직이 아니라 워크플로일 수 있습니다 |
| 2 — 조직 | 국소 상태·정보·권한·업무가 다르고 제한된 실행 중 재편이 존재 | heterogeneous Team과 typed semantic replan이 해당합니다 |
| 3 — 제도 | 여러 Job에 걸쳐 identity·가입·감사·규칙변경·학습이 지속 | Company, Roster, 지속 Employee와 통제된 Patch lifecycle이 해당합니다 |

Noruct 전체는 제도 수준을 지향하지만, 모든 Noruct 실행이 조직인 것은 아닙니다. Direct 작업은 direct로,
replica는 replica로, 고정 graph는 workflow로 봐야 합니다. model node가 여러 개라는 이유만으로 회사 지능이
생기지는 않습니다.

## AI 조직의 일곱 구조

하나의 조직도나 통신 graph만으로 실제 조직을 표현할 수 없습니다. 통제된 AI 조직에는 최소한 다음 일곱 구조가
필요합니다.

| 구조 | 질문 | Noruct 개념 |
|---|---|---|
| 과업 의존성 | 어떤 출력이 어떤 입력이 되는가? | Job task, dependency, final task와 revision lineage |
| 작업 할당 | 어떤 실행 identity가 각 task를 소유하는가? | staffing, delegation, Employee snapshot과 attempt |
| 정보 접근 | 각 Employee가 어떤 source·Memory·Tool 결과를 볼 수 있는가? | bounded Context, Knowledge scope, private Memory와 Tool projection |
| 통신 | 누가 누구에게 어떤 typed artifact를 보낼 수 있는가? | dependency artifact, evidence handoff와 Manager integration lane |
| 결정·실행 권한 | 누가 제안·admit·승인·실행·commit하는가? | 사용자 권한, Company policy, Firm Kernel, ActionPolicy와 effect owner |
| 검증 | 누가 누구의 결과를 어떤 독립 방식으로 검사하는가? | acceptance, validator, reviewer edge, evaluator와 receipt |
| 기억·학습 | 어떤 관찰이 미래 capability를 바꿀 수 있는가? | Episode, evidence, Patch, revision, future snapshot과 outcome |

예산과 재편 규칙은 일곱 구조 전체를 제한합니다. 다른 model이 임의로 바꿀 수 있는 상태가 아닙니다.

~~~mermaid
flowchart LR
  W["Frozen Work Order"] --> T["과업 의존성"]
  W --> X["작업 할당"]
  W --> I["정보 접근"]
  W --> C["통신"]
  W --> D["결정·실행 권한"]
  W --> V["검증"]
  W --> M["기억·학습 계보"]
  T --> P["읽기 전용 조직 투영"]
  X --> P
  I --> P
  C --> P
  D --> P
  V --> P
  M --> P
~~~

이 구조들은 함께 관찰할 수 있어야 하지만 하나의 mutable authority가 되어서는 안 됩니다. task topology를
고친다는 이유로 데이터 접근, permission, effect approval이나 조직 기억까지 조용히 바뀌면 안 됩니다.

## 헌법과 실행조직

가장 견고한 일반형은 고정된 회사 피라미드도, 제한 없는 동료 network도 아닙니다. 안정적인 헌법적 통제층과
동적인 실행조직의 결합입니다.

| 작업 사이에 안정적으로 유지 | 하나의 Job을 위해 편성 |
|---|---|
| 목적 해석 원칙 | task decomposition |
| 권한과 데이터 접근 상한 | Employee와 replica 선택 |
| 금지 행동 | Job-local 정보 projection |
| 예산과 승인 규칙 | 통신 topology |
| 감사 요구사항 | 실행 순서와 concurrency |
| 규칙 변경과 rollback 절차 | 검증 깊이와 replan 조건 |

Manager 또는 조직 Compiler는 Job-local 구조를 선택하고 축소할 수 있습니다. 새로운 권한을 만들 수는 없습니다.
Deterministic Firm Kernel이 제안을 frozen constitution과 대조하고 실행 snapshot을 봉인합니다.

~~~text
Company constitution
→ Manager / 조직 제안
→ deterministic 권한·예산·위험 admission
→ bounded Employee 실행
→ typed artifact와 독립 검증
→ 하나의 final owner와 제한된 effect 실행
→ 감사와 선택적 evidence-bound future Patch
~~~

## 조직을 편성하기 전에 과업을 진단합니다

모든 과업에 가장 좋은 topology는 없습니다.

| 과업 특성 | 구조적 의미 |
|---|---|
| 분해 가능성이 높고 상호의존성이 낮음 | 독립 병렬 작업과 단일 통합자 |
| 순차 의존성이 강함 | typed 단계 계약과 checkpoint가 있는 pipeline |
| 상호의존성 또는 문맥 결합도가 높음 | 강한 단일 Employee 또는 매우 작은 공유 상태 팀 |
| 정보가 여러 위치에 분산됨 | 제한된 판단권을 정보 가까이에 배치 |
| 검증 가능성이 높음 | 다른 범용 model보다 deterministic validator 우선 |
| 위험·불가역성이 높음 | 제안은 분산하고 독립 검증과 effect authority는 단일화 |
| 오류 상관성이 높음 | 역할명 추가 대신 source·model·tool·context·validator를 변경 |
| 환경 변동성이 높음 | bounded 재할당과 명시적 replan 조건 |
| 지연 민감도가 높음 | handoff를 줄이고 가역적 로컬 판단을 확대 |

Noruct는 위험, 권한과 검증 가능성에는 의도적으로 강합니다. 반면 오류 상관성, 정보 분산도, 문맥 결합도와
조정 지연을 예측하는 능력은 상대적으로 약합니다. 안전한 graph admission은 완성된 조직 최적화와 다릅니다.

## Manager는 예외를 압축해야 합니다

Manager는 조직 상태를 변환할 때만 가치가 있습니다.

~~~text
일상적인 dependency·retry
→ deterministic Kernel

새로운 모호함·capability gap·충돌·변경된 가정
→ Manager의 의미 판단

반복되는 예외
→ 평가된 rule·Skill·Workflow·Roster Patch 후보
~~~

메시지를 그대로 전달하는 Manager는 지연 계층입니다. 모든 tool event를 검토하는 Manager는 중앙 병목입니다.
Manager의 가치는 피한 graph 작업, 더 나은 staffing과 integration, 해결한 예외, 미래 예외 감소에서 planning,
supervision, queue, 압축 손실을 뺀 값으로 이해해야 합니다.

따라서 규모가 커졌다는 이유로 관리 계층을 더하는 것은 기본 해법이 아닙니다. 먼저 반복 예외를 규칙화하고,
handoff를 명시하며, 남은 예외 부하를 측정해야 합니다.

## 통신은 손실 압축이자 보안경계입니다

Typed artifact 교환은 자유로운 agent 대화보다 role-play 비용, context 오염과 불명확한 책임을 줄입니다. 좋은
handoff는 다음 의미를 보존해야 합니다.

- claim과 인용된 evidence
- assumption과 적용 범위
- unresolved question과 uncertainty
- artifact와 source revision
- validation 상태와 downstream이 해야 할 일

그러나 모든 handoff는 압축입니다. upstream에 정보가 있었고 올바르게 전달됐어도 최종 판단에 통합되지 않을 수
있습니다.

~~~text
정보가 있었다
≠ 정보가 전달됐다
≠ 정보가 통합됐다
≠ 결론이 검증됐다
~~~

통신 topology는 잘못되거나 공격적인 정보의 피해범위도 결정합니다. 정보 접근, 통신 permission과 실행 권한을
분리해야 합니다. 연결성이 높은 hub가 가장 많은 정보, 최종 결정권과 effect 실행권을 동시에 자동 소유해서는
안 됩니다.

## 검증은 오류구조를 겨냥해야 합니다

“비평가”라는 역할명을 붙여도 독립 검증은 생기지 않습니다. 독립성은 source, model, tool, context boundary,
검증 알고리즘, 성공 기준 또는 실행 permission의 실제 차이에서 나옵니다.

검증은 선택적이어야 합니다. 모든 task를 두 번 실행하는 것은 대개 낭비입니다. 독립 계산은 다음 지점에서
가장 가치가 큽니다.

- 실패가 graph 전체로 전파되는 가정
- 충돌하거나 오래된 evidence
- 의미 acceptance가 약한 산출물
- 불가역적인 외부 effect
- rollback이 제한된 고비용 계산과 변경
- 실제로 다른 candidate 사이의 최종 선택

중요한 것은 reviewer 수가 아닙니다. 다른 오류경로가 과도한 오탐과 조정비용 없이 실제 결함을 잡았는지입니다.

## 조직 잉여와 총비용

여러 Employee는 동일 권한과 총예산의 strong single baseline보다 조직적 잉여를 만들 때만 정당화됩니다.

~~~text
조직 잉여
= 품질·coverage·안전·회복·유효 지연의 개선
- 실행·통신·통합·검증·거버넌스·예외·실패 비용
~~~

model call과 평균 quality만으로는 부족합니다. 사용되지 않은 artifact, 중복 source read, integration rejection,
reviewer 탐지율과 오탐률, exception escalation, Manager queue, 오류 전파 깊이, 최고의 specialist 결과가 최종안에
반영된 비율도 조직 성능입니다.

하나의 종합점수는 trade-off를 숨깁니다. 품질, 비용, 지연, 안전과 감사 가능성을 Pareto 관계로 읽는 편이 맞습니다.

## Noruct가 개념적으로 개선할 부분

다음 조직적 진화는 자율성 확대보다 관찰성 개선이 먼저입니다.

1. 기존 authority에서 일곱 구조의 읽기 전용 projection을 만듭니다.
2. Handoff를 의미 보존 계약으로 보고 hidden reasoning을 저장하지 않으면서 누락 맥락을 관찰합니다.
3. 정적인 capability 차이에서 실제 오류 다양성과 검증 기여로 이동합니다.
4. Manager를 중앙 지능이 아니라 예외 경제로 평가합니다.
5. Team, replica, independent review를 선택할 때 bounded 가치 가설을 남깁니다.
6. 실행 usage와 함께 통신·통합·거버넌스·예외 비용을 귀속합니다.
7. 오래 지속되는 capability evidence에 freshness와 confidence decay를 둡니다.
8. active·superseded·incompatible 평가를 구분하는 evidence lineage를 유지합니다.
9. 큰 실행형태 비교를 information·communication·verifier·selector ablation으로 확장합니다.

이 방향은 하나의 mutable organization graph를 요구하지 않습니다. Projection은 관찰만 하고 기존 authority owner가
계속 결정합니다.

## 이 개념이 거부하는 것

- material capability 대신 직책과 persona를 늘리는 것
- 모든 요청에서 Team을 기본으로 만드는 것
- 자유 형식 agent 회의를 기본 coordination primitive로 삼는 것
- Manager가 자신의 권한이나 불가역 effect를 승인하는 것
- task·data·permission·verification·learning을 하나의 mutable graph로 합치는 것
- hidden reasoning을 조직 기억으로 노출하는 것
- 한 번의 좋은 campaign으로 workflow나 Employee를 승격하는 것
- 모든 graph·replica·pipeline을 조직이라고 부르는 것

## 문헌검토 원문

이 개념문서의 배경이 된 두 편의 한국어 장문 문헌검토를 별도로 공개한다. 원문의 논증과 문헌 지도를 보존하되,
원문 주장을 제품 권위로 바꾸지 않는다.

- [AI 에이전트 조직 설계 원리](../literature/organization-as-decision-structure.ko.md)
- [LLM 멀티에이전트 운영 구조와 설계](../literature/from-human-organization-to-ai-agent-organization.ko.md)

## 최종 입장

Noruct에는 의사결정 구조를 가진 회사의 개념적 재료가 이미 있습니다. 지속 identity, frozen assignment, bounded
information, typed artifact, deterministic authority, 독립 검증 경로와 evidence-gated evolution입니다. 약한 부분은
더 많은 agent나 계층이 없다는 것이 아닙니다. 정보가 어디에서 손실됐는지, 예외가 어디에 쌓였는지, 검증이 실제로
독립적이었는지, 총조정비용 뒤에도 조직 잉여가 남았는지를 한 화면에서 설명하는 능력입니다.

따라서 다음 단계는 더 큰 조직이 아닙니다. 자신의 의사결정 구조를 관찰하고, 근거가 정당화하는 Job-local 구조만
바꿀 수 있는 회사입니다.
