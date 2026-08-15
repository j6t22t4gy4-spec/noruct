# 10 — Model Intelligence와 다중 Provider 오케스트레이션

> 정본: [English](../en/10-model-intelligence-and-multi-provider-orchestration.md) · [← 조직은 직책이 아니라 의사결정 구조다](09-organization-as-decision-architecture.md) · [문서 인덱스](README.md)

## 중심 명제

모델 catalog는 조직 판단에 정보를 제공해야 하지만 조직 권위가 되어서는 안 됩니다.

Noruct의 모든 설치가 모든 모델을 다시 benchmark할 필요는 없습니다. 통제된 평가 환경이 task별 관측치,
불확실성, 지연, 비용 가용성, 오류 패턴을 담은 서명된 data-only intelligence snapshot을 공개할 수 있습니다. 로컬
회사는 이 공유 prior에 자체 호환성, 권한·데이터 경계, 관측된 outcome과 사용자 정책을 결합합니다. Firm Kernel은
그 결과로 각 EmployeeRun 또는 task attempt의 정확한 실행 route를 동결합니다.

~~~text
공유 benchmark prior
+ 로컬 호환성
+ 로컬 관측 outcome
+ 권한·privacy·task 제약
→ 다음 Job의 동결된 실행 route
~~~

Benchmark service는 기본적으로 실제 Job 내용을 받지 않으며, Employee를 dispatch하거나 capability를 설치하거나
실행 중 Job을 바꾸지 않습니다.

## 네 종류의 evidence를 분리합니다

“이 모델이 좋다”는 표현은 서로 다른 질문을 섞습니다. Noruct는 이를 구분합니다.

| Evidence | 질문 | 경계 |
|---|---|---|
| Provider 선언 | Provider는 이 route가 무엇을 지원한다고 말하는가? | 유용한 metadata지만 로컬 호환성·품질 proof는 아님 |
| 공유 benchmark prior | 기록된 harness와 task 분포에서 어떤 결과였는가? | 불확실성을 가진 재사용 evidence이며 보편 순위가 아님 |
| 로컬 호환성 | 이 환경에서 필요한 wire/runtime 계약을 만족하는가? | 짧은 smoke이며 또 하나의 성능 benchmark가 아님 |
| 로컬 outcome | 이 회사의 matched task에서 실제로 무슨 일이 있었는가? | context별 보정이며 무제한 자기최적화 권한이 아님 |

공유 benchmark는 task class, harness·dataset revision, 표본 수, 분산, complete failure, evaluator 조건, 알려진 한계,
sponsorship 또는 contamination 가능성을 보존해야 합니다. 하나의 leaderboard 점수만으로 조직 route를 고를 수는
없습니다.

## 모델 identity는 실제로 아는 수준만 표현합니다

원격 model identifier는 immutable weight의 digest가 아닐 수 있습니다. Noruct는 실제로 뒷받침할 수 있는 가장 강한
identity claim만 기록해야 합니다.

- 로컬 content digest
- 변경 불가능하다고 관찰 가능한 provider revision
- weight를 독립 확인하지 못한 versioned model identifier
- 같은 이름 뒤에서 바뀔 수 있는 floating alias
- identity assurance unknown

이는 모든 provider의 weight를 검사하자는 요구가 아닙니다. 요청한 alias를 content-addressed artifact처럼 잘못
표현하지 않기 위한 경계입니다. Material drift는 다음 선택의 route를 만료시킬 수 있지만 active Job을 조용히
reroute하거나 과거 evidence를 다시 쓰지 않습니다.

## Intelligence snapshot은 실행 capability가 아닌 data입니다

Model-intelligence snapshot은 bounded metric, 불확실성, provenance, orchestration weight profile, expiry, payload digest와
signature를 포함할 수 있습니다. 실행 코드, prompt, Tool, Skill, Plugin, credential이나 Company state를 바꾸는 지시는
포함할 수 없습니다.

~~~text
downloaded
→ signature·schema·expiry 검증
→ local candidate
→ 다음 Job에 활성화
→ retire 또는 rollback
~~~

Invalid, unknown, expired 또는 unavailable intelligence는 보존된 last-known-good snapshot이나 보수적인 local default로
닫힙니다. Offline startup을 막거나 무한 background retry를 만들거나 다른 package를 설치해서는 안 됩니다.

## 직책 하나가 아니라 task와 상태에 따라 route를 선택합니다

Persistent Employee는 capability profile, private bounded state와 허용 execution class를 가집니다. Provider account를
소유하지는 않습니다. Job은 Work Order, Employee capability, information boundary, organization state, 사용자 정책과
현재 evidence를 고려해 required execution class를 exact route로 해석합니다.

상태마다 다른 route가 적합할 수 있습니다.

| 상태 | 일반적인 요구 |
|---|---|
| Frame | 강한 요구사항·위험 해석 |
| Explore | 다양성의 기대가치가 있을 때 bounded 독립 후보 |
| Select | Evidence 비교와 안정적인 structured output |
| Integrate | Context를 다룰 수 있는 단일 owner |
| Verify | 실제로 다른 오류경로·source·tool·model |
| Commit | Model이 아닌 deterministic Kernel과 Executor |
| Learn | 직접 자기수정이 아닌 다음 변경 후보 |

따라서 재사용 Blueprint는 provider 브랜드가 아니라 required capability와 execution class를 결박합니다. 새 Job마다
자신의 route를 해석·동결하며 Blueprint나 실행 중 Job을 수정하지 않습니다.

## 하나의 Job이 여러 provider를 사용해도 권위는 늘어나지 않습니다

다중 provider에는 서로 다른 의미가 있습니다.

1. 서로 다른 task 또는 Employee가 다른 exact route를 사용합니다.
2. Advisory route가 tool 없는 bounded candidate evidence를 만듭니다.
3. Read-only verifier가 실제로 다른 오류경로를 사용합니다.
4. 사전 승인된 fallback route가 output/effect 전 retryable availability failure를 처리합니다.

Fallback은 독립 검증이 아닙니다. Reference model은 acting Employee가 아닙니다. Advisory output은 system instruction이
아니라 source가 표시된 untrusted evidence입니다. 여러 route를 쓸 수 있다는 이유로 전체 prompt를 모두에게 보내지
않고 각 provider의 data-egress grant가 허용한 context projection만 전달합니다.

최종 artifact는 계속 owner 하나, acting integrator 하나, commit path 하나를 가집니다. 사용자에게 partial output을
보냈거나 tool·외부 effect가 시작됐거나 commit한 뒤에는 route를 교체하지 않습니다.

## 라우팅은 먼저 자격을 검사하고 그다음 최적화합니다

Resolver는 authority, data egress, required capability, availability 또는 continuation 제약을 만족하지 않는 route를
먼저 제외합니다. 적격 route만 task별 quality, complete-task reliability, specialization, verification independence,
latency, local outcome과 cost로 비교합니다.

비용은 하나의 최적화 입력이지 회사의 목적이 아닙니다. 호출·시간·비용 hard ceiling은 폭주 방지용 안전 통제로
남습니다. 사용자 정책은 하나의 전역 scalar가 모든 trade-off를 나타낸다고 가장하지 않고 quality-first, balanced,
efficient, private-local-first 같은 의도를 표현할 수 있습니다.

점수 차이가 불확실성보다 작다면 더 단순한 route와 strong Solo가 보수적인 선택입니다.

## 모든 실제 호출에는 immutable receipt가 필요합니다

다중 provider 실행을 감사하려면 모든 physical call의 route/context projection, attempt 또는 fan-out lineage,
terminal/indeterminate 상태, safe provider metadata, usage, cost availability, latency, error code와 output digest를 기록해야
합니다. 공유 provider 객체의 mutable field는 durable execution evidence가 아닙니다.

Cancellation은 모든 child call과 local process에 전파되어야 합니다. Provider-native thread는 Company state가
아닙니다. Continuation은 frozen route를 사용하거나 fresh session과 receipt가 있는 명시적 rebound를 사용하며, 과거
context를 다른 endpoint로 조용히 옮기지 않습니다.

## 인간 통제는 의미 있는 경계에 집중합니다

이미 승인된 route의 ordinary reuse가 반복 승인 prompt를 만들어서는 안 됩니다. 인간 검토는 권한이나 노출이 실제로
변하는 경계에 둡니다.

- provider 또는 credential 추가
- data-egress class 확대
- benchmark corpus·license·공개 claim 승인
- 유료 live qualification 승인
- snapshot 또는 product release의 서명·게시
- release keep·pause·rollback·replacement 결정

기본 architecture는 download-only입니다. 로컬 Job outcome 업로드는 별도 opt-in data-minimization 계약이 필요하며
prompt, artifact, workspace identity, credential과 customer content는 기본 telemetry가 아닙니다.

## 현재 경계

현재 개발 runtime에는 여러 provider adapter, explicit bounded fallback과 bounded advisory fan-out이 있습니다. 하지만
이들은 현재 Job 전역 provider composition이며 Employee/task별 routing이 완성된 것은 아닙니다. Signed intelligence
snapshot, local compatibility cache, per-run exact route resolution, provider별 egress와 production multi-provider
qualification은 개발 작업으로 남아 있습니다. 이 문서는 목표 architecture이며 성능 또는 release claim이 아닙니다.
