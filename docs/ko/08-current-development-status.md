# 08 — 현재 개발 상태

> 정본: [English](../en/08-current-development-status.md) · [← Network Engineering](07-network-engineering.md) · [문서 인덱스](README.md)

**마지막 검토: 2026-07-31.** 영문판이 정본입니다.

## 목적

이 문서는 Noruct의 개념과 현재 개발 구현을 구분합니다. 상용 출시 발표, benchmark 성능 주장, 또는 모든 설치 환경에서
선택 기능이 활성화된다는 약속이 아닙니다.

## 현재 개발 기준선

로컬 개발 runtime은 현재 다음을 제공합니다.

- 지속 Company·Roster·Playbook·Employee·session·audit 상태를 가진 하나의 Company CLI/TUI 진입점
- deterministic authority·approval·budget Kernel 아래의 direct, managed solo, bounded team 실행
- role 이름만 다른 전문화 대신 material Employee capability profile, 그리고 partition·candidate·diagnostic을 위한
  제한된 same-Employee execution replica
- typed task handoff, 하나의 final result owner, 제한된 retry/reroute/insert 경로, append-only Job audit
- preview·constraint·revision·fork·pin과 retained revision lineage를 갖는 사용자 통제 Graph Blueprint
- bounded evidence bridge로 연결되지만 상태 권위는 분리된 local Knowledge, Intent/Decision, Firm
- 별도 review와 apply lifecycle을 요구하는 versioned Skill·Workflow·Roster Patch proposal

Noruct에는 실행 가능한 Employee Runtime이 하나만 있습니다. historical employee-state compatibility는 local state를
읽고 검증된 backup receipt를 만들 수 있지만, 다른 engine·runtime 선택기·현재 authority와 approval 계약을 우회하는
경로가 아닙니다. 내부적으로는 CLI ingress, ACTIVE JOB audit, Graph Workbench 표시부, runtime·Company·Knowledge
projection을 같은 local state authority 뒤에서 분리하고 있습니다. 이는 제품 surface 개편입니다. CLI, TUI, future GUI가
서로 다른 control path나 두 번째 Company state를 만들지 않고 같은 통제된 operation을 호출하게 하기 위한 것입니다.

Manager는 Work Order 해석, 실행 형태 선택, typed delegation, accepted artifact 통합, 사용자 보고를 수행하는 제한된
지속 Company 구성원으로 구현되어 있습니다. Manager는 스스로 권한을 늘리거나 external action을 승인하거나 durable
Company state를 직접 바꿀 수 없습니다.

## 근거와 비주장

provider-free regression suite, source integrity 검사, Worker type 검사, local Worker route integration test는 개발
과정의 일부입니다. 이들은 contract 보존과 좁은 integration behavior를 보일 뿐, 넓은 현실 가치의 증거는 아닙니다.

특히 Noruct는 현재 다음을 주장하지 않습니다.

- Manager-led Firm 또는 team이 강한 single Employee보다 일관되게 우수하다는 것
- 모든 graph mutation이 안전하거나 일반적으로 제공된다는 것
- Shared Network artifact가 임의의 publisher code 실행, 새 permission 획득, 실행 중 Job 변경을 할 수 있다는 것
- Shared Evolution이 일반 고객 서비스라는 것
- 모든 provider·운영체제·GUI surface·배포 경로·상용 release gate가 완성됐다는 것

Manager와 조직 실험은 negative-transfer 결과도 evidence로 보존합니다. 한 번의 제한된 평가에서 유용한 결과가 나와도
그것만으로 기본값, 재사용 Blueprint, Skill 또는 Roster 변경을 승격하지 않습니다.

## 현재 경계

| 영역 | 현재 위치 |
| --- | --- |
| Manager와 team 가치 | 기능 구조는 존재하지만 outcome qualification 진행 중 |
| 일반 semantic replanning | 제한된 typed path는 있으나 광범위한 자율 재작성은 주장하지 않음 |
| Graph mutation과 recovery | revision lineage와 좁은 receipt-bound continuation은 있으나 in-flight/effectful replay는 자동 재개하지 않음 |
| Knowledge | local-first raw-source intake와 bounded evidence 사용은 존재하지만 extraction은 자동 진실이 아님 |
| Network | signed artifact lifecycle과 제한된 deployed registry path가 있으나 고객 운영과 broad executable adapter는 주장하지 않음 |
| Capability integrity | 외부 version/digest는 명시적으로 활성화해야 함. 사용자 `always-approve`, 권한 검사와 static 계약 호환성을 통과한 non-Network local derivative만 다음 Job에 승격 가능하며 실행 중 Job pin과 이전 activation rollback은 유지됨 |
| Platform과 release | 개발 검증은 있으나 Windows 폭, packaging, legal/provenance review, commercial release authorization은 별도 gate |
| Runtime 선택 | 실행 가능한 Noruct runtime은 하나이며 historical state compatibility는 rollback engine이 아닌 read/backup 경로 |
| Operator surface | CLI와 TUI가 현재 로컬 surface다. loopback Graph Workbench는 좁은 GUI-ready projection과 future-Job constraint 경로를 검증하지만, 범용 desktop 또는 hosted GUI는 아니다. |

## 검증 가능한 제품 질문

Noruct의 핵심 가설은 의도적으로 검증 가능해야 합니다.

> 같은 model access, authority, total budget에서, 가장 작은 유용한 구조를 admission·staffing·validation·revision하는
> firm이 강한 single Employee보다 더 많은 검증된 가치를 만들 수 있는가?

많은 요청의 정답은 direct 또는 solo일 수 있습니다. 더 눈에 띄는 조직이 성공은 아닙니다. 추가 구조는 측정·설명·rollback
가능할 때에만 정당화됩니다.
