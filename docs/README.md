# Noruct 문서 지도 / Document map

<a id="noruct-public-documentation"></a>

이 페이지가 공개 레포의 **전체 진입점**입니다. 문서의 역할부터 고른 뒤 해당 본문으로 이동하세요.
언어별 번호 00–10은 기존 문서 식별자이며, 연구논문의 장 번호나 기능 완성 순서가 아닙니다.

This is the repository-wide entry point. Choose by document role first. The existing 00–10 identifiers are not research-paper chapters or a feature-completion sequence.

## 어떤 문서를 읽을까? / Choose a path

| 구분 / Role | 답하는 질문 / Question | 읽을 곳 / Entry | 근거의 상태 / Status |
| --- | --- | --- | --- |
| **연구논문 / Research** | 어떤 이론적 주장과 연구명제를 제안하는가? | [계산적 거버넌스 working paper](literature/organization_governance_manuscript.md) | 이론 구축형 원고, 자체 실험 결과 없음 |
| **개념 / Concepts** | Noruct는 무엇이며 어떤 원칙을 지키는가? | [개념 읽기 경로](#concepts) | 제품 개념과 목표 동작 |
| **설계 / Design** | 개념을 어떤 구성요소와 경계로 구체화하는가? | [설계 읽기 경로](#design) | 목표 아키텍처, 구현 명세·출시 증거 아님 |
| **실험 / Experiments** | 주장을 어떻게 비교하고 측정할 것인가? | [프로토콜과 기록 양식](#experiments) | 미실행·미동결 초안, 결과 없음 |
| **문헌 / Literature** | 어떤 선행연구와 이전 논의를 참고하는가? | [장문 문헌자료와 이전 판본](#literature) | 해석·보조 논의, 인용 전체의 독립 검증 전 |
| **공개 범위 / Scope** | 공개 문서가 무엇을 주장하지 않는가? | [개발 범위와 비주장](#scope) | 구현 현황표·릴리스 노트 아님 |

## 문서 사이의 관계 / How the documents connect

**문헌자료 → 연구논문 → 실험 프로토콜 → 기록 양식**은 연구를 읽고 검증을 준비하는 경로입니다.
**개념 → 설계 → 공개 범위 확인**은 Noruct의 제품 방향과 경계를 읽는 별도 경로입니다.
연구논문의 이론과 설계 문서는 서로 참고하지만 어느 쪽도 다른 쪽의 검증 증거를 대신하지 않습니다.

Research path: literature → manuscript → protocol → record template. Product path: concepts → design → scope check.
The manuscript proposes P1–P6; the first protocol tests only P1→D1, P2→D2 and P4→D3. P3, P5 and P6 remain follow-up questions. None of these steps supplies observed results yet.

<a id="research"></a>
## 1. 연구논문 / Research manuscript

- **[AI 에이전트의 분업과 실행 권한 설계](literature/organization_governance_manuscript.md)**: 현재의 연결된 논증을 읽을 대표 원고. 분업의 비용, 실행 권한과 사람이 맡아야 할 판단을 다루는 한국어 이론 구축형 working paper입니다.
- 초록·이론적 기반·개념모형·P1–P6·경험적 연구설계·논의·참고문헌을 포함합니다. 자체 실험 결과를 보고하는 논문은 아닙니다.
- 판본 관계와 세부 근거를 대조하려면 [연구·문헌 안내](literature/README.md)를 봅니다.

<a id="concepts"></a>
## 2. 제품 개념 / Product concepts

제품을 이해하려면 **00 → 02 → 04 → 09** 순서가 짧은 첫 경로입니다. 연구논문 대신 읽는 요약본이 아니라, Noruct의 제품 관점을 설명하는 문서입니다.

| ID | 한국어 | English | 주 역할 / Primary role |
| --- | --- | --- | --- |
| 00 | [지향점](ko/00-north-star.md) | [North Star](en/00-north-star.md) | 목표·지속성·인간 권한 / Purpose and human authority |
| 02 | [지속 직원](ko/02-persistent-employee.md) | [Persistent Employee](en/02-persistent-employee.md) | Employee 능력·상태·할당·반환 경계 |
| 04 | [그래프와 회사 공학](ko/04-graph-and-firm-engineering.md) | [Graph & Firm Engineering](en/04-graph-and-firm-engineering.md) | 의존성과 산출물 흐름, 조직 구조의 비용 |
| 09 | [조직은 의사결정 구조다](ko/09-organization-as-decision-architecture.md) | [Organization as Decision Architecture](en/09-organization-as-decision-architecture.md) | 최소 충분 조직과 제품상 해석 / Minimum sufficient organization |

<a id="design"></a>
## 3. 설계와 아키텍처 / Design and architecture

개념을 구성요소로 연결하려면 **01 → 03 → 05 → 06**를 읽습니다. **07·10**은 네트워크 교환과 모델·Provider 경로라는 별도 설계 주제입니다. 이 분류는 주 역할 기준이며, 개념 문서에도 설계 예시가 포함될 수 있습니다.

| ID | 한국어 | English | 주 역할 / Primary role |
| --- | --- | --- | --- |
| 01 | [동적 회사 런타임](ko/01-dynamic-firm-runtime.md) | [Dynamic Firm Runtime](en/01-dynamic-firm-runtime.md) | Company·Manager·Kernel·Job 관계와 실행 수명주기 |
| 03 | [지식·의도·회사](ko/03-knowledge-intent-firm.md) | [Knowledge, Intent & Firm](en/03-knowledge-intent-firm.md) | 지식·방향·실행 권한의 구분 / Knowledge, direction and authority |
| 05 | [통제된 진화와 사용자 그래프](ko/05-governed-evolution-and-user-graphs.md) | [Governed Evolution & User Graphs](en/05-governed-evolution-and-user-graphs.md) | Blueprint·변경·재사용·학습의 경계 |
| 06 | [인식 통제·판정 기준·결과](ko/06-epistemic-control-and-outcome.md) | [Epistemic Control, Oracle & Outcome](en/06-epistemic-control-and-outcome.md) | 근거·불확실성·성공 판정 / Evidence, uncertainty and outcomes |
| 07 | [네트워크 공학](ko/07-network-engineering.md) | [Network Engineering](en/07-network-engineering.md) | 선택적 능력 교환과 로컬 권한 |
| 10 | [Model Intelligence와 다중 Provider](ko/10-model-intelligence-and-multi-provider-orchestration.md) | [Model Intelligence and Multi-Provider Orchestration](en/10-model-intelligence-and-multi-provider-orchestration.md) | 공유 evidence·로컬 호환성·route·정보 반출 |

**구현 문서와는 구분합니다.** 공개 레포에는 구현 소스, 설치·API 참조, 재현 가능한 제품 검증 묶음이 없습니다.
01·10의 개발 설명은 해당 문서의 개발 진술이며, 이번 문서 분류가 이를 새로 구현·검증했다는 뜻은 아닙니다.

These are design guides, not an implementation manual. Development statements in 01 and 10 remain statements in those documents, not newly verified implementation evidence.

<a id="experiments"></a>
## 4. 실험 설계와 기록 / Experiment design and records

1. [분업의 한계가치와 최소 충분 인계: 검증 프로토콜](literature/organization_value_experiment.md): D1–D3, 대조조건, 측정 단위, 반증 기준을 정의합니다.
2. [실험 기록 양식](literature/organization_experiment_record.template.json): 후보 집합·선택 전 문맥·실제 노출·결과·비용·결측을 기록할 JSON 양식입니다.

**현재: 실행 전 초안입니다.** 과업군·표본수·주 지표·예산·중단 규칙의 확정과 프로토콜 동결이 남아 있습니다.
JSON의 `NOT_RUN`과 `null`은 미실행·미관찰을 뜻하며 0이나 성공을 뜻하지 않습니다. 공개 실험 결과 보고서는 아직 없습니다.

The protocol is a pre-execution draft; the JSON is a template, not a dataset. No results report is published here.

<a id="literature"></a>
## 5. 문헌자료와 이전 판본 / Literature and versions

- [v0.9 장문 문헌자료](literature/organization_governance_theory.md): 선행연구, 용어, 설계 예시, 세부 명제와 한계를 대조할 보조 자료입니다. 대표 연구논문과 제목이 같아도 역할은 다릅니다.
- [v0.6 연구논문 원문](https://github.com/j6t22t4gy4-spec/noruct/blob/35124074/docs/literature/organization_governance_theory.md): 당시 판본을 보존한 역사 링크입니다. 현재 대표 원고는 위의 working paper입니다.
- 인용된 외부 연구의 관찰과 수치를 Noruct의 실험 결과로 읽지 않습니다. 전체 인용의 독립 검증 완료를 주장하지 않습니다.

<a id="scope"></a>
## 6. 개발 범위와 비주장 / Development scope and non-claims

**[한국어 08](ko/08-current-development-status.md) · [English 08](en/08-current-development-status.md)**

파일명에 `current-development-status`가 남아 있지만 실제 문서의 역할은 공개 개념·개발 범위·비주장의 경계를 설명하는 것입니다.
현재 구현 항목 목록이나 최신 릴리스 현황이 아닙니다. 본문의 마지막 검토일 **2026-08-15**는 그대로 유지하며, 이 문서 지도의 정리 날짜를 기능 검증일로 간주하지 않습니다.

Despite its historical filename, 08 describes scope and non-claims, not a current implementation inventory. Its last-review date is not refreshed by this navigation update.

## 언어·경로·유지 원칙 / Language, paths and maintenance

- **제품 개념·설계:** [English](en/README.md)가 정본이며 [한국어](ko/README.md)는 번역본입니다.
- **연구논문·문헌·실험:** 한국어 원문입니다. 영문 제품 안내의 번역본이 아닙니다.
- `en/`, `ko/`, `literature/`는 기존 본문 보관 경로입니다. 폴더 이름만으로 문서 역할을 판단하지 말고 위 분류와 각 문서의 첫 안내를 따릅니다. 기존 URL과 본문 앵커를 유지하기 위해 파일은 옮기지 않았습니다.
- 새 문서를 추가할 때는 이 지도에 주 역할·상태·진입 링크를 등록하고, 본문 첫머리에 같은 역할과 근거의 상태를 적습니다. 본문을 여러 경로에 복제하지 않습니다.
- 연구명제, 제품 설계 원칙, 구현 진술, 실험 결과는 서로 대체하지 않습니다. 앞으로 결과를 추가할 때도 프로토콜·기록 양식과 별도 문서로 두고 실행 조건과 한계를 밝힙니다.

[레포 첫 화면 / Repository home](../README.md) · [연구·문헌 안내 / Research guide](literature/README.md)
