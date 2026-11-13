# 08 — 공개 문서의 범위와 확인되지 않은 주장

<a id="08--개발-범위와-비주장"></a>

> **문서 역할:** 개발 범위와 비주장 · **상태:** 공개 범위 설명이며 최신 구현 현황표가 아닙니다.
> [전체 문서 지도](../README.md) · [역할별 읽기 경로](../README.md#scope) · [연구논문](../literature/organization_governance_manuscript.md)

> 정본: [English](../en/08-current-development-status.md) · [← Network Engineering](07-network-engineering.md) · [문서 인덱스](README.md) · [다음: 조직은 직책이 아니라 의사결정 구조다 →](09-organization-as-decision-architecture.md)

**마지막 검토: 2026-08-15.** 영문판이 정본입니다.

## 이 문서에서 다루는 질문

**이 문서 묶음만으로 어디까지 확인할 수 있는가?**

개념 설명과 구현 증거, 실험 결과를 구분하는 기준입니다. 기존 검토일을 유지하며 최신 기능 목록으로 사용하지 않습니다.

[공통 용어](README.md#terms) · [목적별 읽기 순서](README.md#reading-paths)

## 목적

이 문서는 중요한 구분을 유지합니다. Noruct의 공개 개념문서는 목표 operating model과 안전 경계를 설명할 뿐,
software artifact, release 발표, benchmark 결과, availability 약속, commercial-readiness 주장이 아닙니다.

## 제품 표면 원칙

Noruct는 product application과 execution engine을 협력하지만 분리된 계층으로 다룹니다.

- product application은 사용자가 보는 대화, project 선택, draft, local workspace view, terminal layout, command
  admission을 소유합니다.
- execution engine은 model/tool 실행, checkpoint, effect transaction, policy enforcement, 검증 가능한 execution receipt를
  소유합니다.
- 실제 작업을 일으킬 수 있는 command는 interactive, headless, editor-connected surface 전체에서 하나의 reviewable
  intent와 하나의 execution record를 유지해야 합니다.

이 분리는 모든 화면이 실행 권한을 가진다는 뜻이 아닙니다. local terminal은 사용자가 직접 수행하는 shell 작업을
지원할 수 있지만, managed Job은 product의 통제된 command 경로로만 시작합니다. 외부 coding session을 관찰하는 경우도
managed Job이나 approval channel이 아닙니다. 외부의 `blocked`, 연결 끊김, 상태 부재는 사용자에게 주는 정보일 뿐,
Job을 resume·approve·retry·success로 처리할 근거가 될 수 없습니다.

## 개발과 qualification의 경계

구현과 qualification은 서로 다른 속도로 진행될 수 있습니다. local feature, fixture, internal receipt는 좁은 contract가
실행되었음을 보일 수 있지만, 그것만으로 broad reliability, customer availability, commercial suitability, 조직 성능 우위를
입증하지는 못합니다.

특히 이 공개 문서는 다음을 주장하지 않습니다.

- Manager-led Firm 또는 team이 강한 single Employee보다 일관되게 우수하다는 것
- 모든 graph mutation, continuation, tool, provider, platform이 안전하거나 일반 제공된다는 것
- 외부 terminal, provider, plugin, Network artifact, shared service가 local firm의 권한을 가질 수 있다는 것
- Shared Evolution, account sync, marketplace, relay, telemetry, automatic update가 고객 서비스라는 것
- 특정 package, 운영체제, desktop surface, deployment path, 법무 검토, commercial release gate가 완료됐다는 것

## 개념 성숙도 지도

| 영역 | 공개 위치 |
| --- | --- |
| Minimum sufficient organization | 추가 구조에는 선언된 partition, candidate 비교, diagnostic, verification, recovery 가치가 필요합니다. 그 가치가 불명확하면 solo가 기본입니다. |
| Employee capability | Employee는 제한된 assignment, 허용 capability, 관련 evidence, acceptance condition을 받으며 스스로 authority를 늘리거나 durable organization identity를 고칠 수 없습니다. |
| Graph와 Blueprint | Blueprint는 사용자가 볼 수 있는 재사용 가설입니다. Job graph와 Run record는 실제로 admission·실행된 구조를 보존합니다. |
| Knowledge와 outcome | extraction, inference, delivery, delayed outcome은 분리됩니다. 그럴듯한 답변이나 완료된 mechanism은 자동으로 verified value가 되지 않습니다. |
| Recursive improvement | 외부 artifact는 명시적으로 review·pin되며 future Job에만 적용됩니다. local-derived candidate도 허용된 advancement 전 compatibility와 authority 검사를 통과해야 합니다. |
| Network | distribution은 선택적 availability일 뿐 remote execution이나 local authority가 아닙니다. private content, credential, active work, 문서화되지 않은 context는 local에 남습니다. |
| Product와 terminal surface | product view는 작업을 정리할 수 있지만 execution result를 만들어내지 않습니다. external terminal observation은 별도 통제 capability가 생기기 전까지 display-only입니다. |

## 검증 가능한 제품 질문

Noruct의 핵심 가설은 의도적으로 검증 가능해야 합니다.

> 같은 model access, authority, total budget에서, 최소 충분 구조를 admission·staffing·validation·revision하는
> firm이 강한 single Employee보다 더 많은 검증된 가치를 만들 수 있는가?

많은 요청의 정답은 direct 또는 solo일 수 있습니다. 더 눈에 띄는 조직이 성공은 아닙니다. 추가 구조는 측정·설명·rollback
가능할 때에만 정당화됩니다.
