# 08 — 현재 개발 상태

> 정본: [English](../en/08-current-development-status.md) · [← Network Engineering](07-network-engineering.md) · [문서 인덱스](README.md) · [다음: 조직은 직책이 아니라 의사결정 구조다 →](09-organization-as-decision-architecture.md)

**마지막 검토: 2026-08-01.** 영문판이 정본입니다.

## 목적

이 문서는 Noruct의 개념과 현재 개발 구현을 구분합니다. 상용 출시 발표, benchmark 성능 주장, 또는 모든 설치 환경에서
선택 기능이 활성화된다는 약속이 아닙니다.

## Noruct 0.0.80 공개 developer preview

Noruct 0.0.80은 하나의 공개 **unsigned developer-preview** wheel로 제공됩니다. 무결성 경계는 공개된
SHA-256과 독립 public readback이며 artifact signature 또는 notarization을 주장하지 않습니다. 검증된 범위는
macOS arm64와 CPython 3.11의 provider-free local runtime contract입니다. Windows는 unsupported 또는
experimental-disabled이고, 이 문구는 Linux·WSL·다른 architecture·hosted operation·production/enterprise 사용을
qualified로 만들지 않습니다.

이 preview는 frozen routing, receipt-bound continuation, bounded single-Job multi-route/provider contract와
외부 Tool·Skill·Plugin의 exact version/digest pinning을 포함합니다. 자체 receipt가 없는 live provider/model은
qualified가 아닙니다. multi-provider 실행은 `EXPERIMENTAL / PROVIDER_DEPENDENT / NOT_LIVE_QUALIFIED`이며,
heterogeneous 또는 cross-provider 품질 주장을 하지 않습니다. signed network Model Intelligence Snapshot은
게시·활성화하지 않으며 bundled conservative default와 explicit local route가 기본으로 남습니다.

이 첫 공개 release에는 prior signed public artifact가 없습니다. human release owner의 대응 순서는 신규 설치
중지, 필요 시 release asset 제거, future snapshot/route 사용 disable입니다. 이는 완료된 rollback rehearsal 주장이
아닙니다. 자세한 내용은 [0.0.80 preview release note](../../releases/noruct-0.0.80-developer-preview.md)를
참조하십시오.

## 현재 개발 기준선

로컬 개발 runtime은 현재 다음을 제공합니다.

- 지속 Company·Roster·Playbook·Employee·session·audit 상태를 가진 하나의 Company CLI/TUI 진입점
- deterministic authority·approval·budget Kernel 아래의 direct, managed solo, bounded team 실행
- role 이름만 다른 전문화 대신 material Employee capability profile, 그리고 partition·candidate·diagnostic을 위한
  제한된 same-Employee execution replica
- typed task handoff, 하나의 final result owner, 제한된 retry/reroute/insert 경로, append-only Job audit
- operator review를 위한 bounded terminal execution-summary 기반과 content-free receipt projection
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
| 조직 적합성과 검토 가능한 전달 | Capability 기반 배치, bounded 실행, terminal summary와 receipt 기반은 존재합니다. Immutable fit profile, frozen organization plan, 완전한 여섯 질문 전달과 사람 review-burden 연구는 아직 완성된 제품 주장이 아닙니다. |
| 일반 semantic replanning | 제한된 typed path는 있으나 광범위한 자율 재작성은 주장하지 않음 |
| Graph mutation과 recovery | revision lineage와 좁은 receipt-bound continuation은 있으나 in-flight/effectful replay는 자동 재개하지 않음 |
| Knowledge | local-first raw-source intake와 bounded evidence 사용은 존재하지만 extraction은 자동 진실이 아님 |
| Network | 공개 Core에는 signed-artifact client lifecycle이 포함됨. Noruct 운영 Shared Evolution/Network server, publisher/auth, remote coordination, billing·tenant operation은 비공개 hosted-service 범위이며 일반 제공을 주장하지 않음 |
| Capability integrity | 외부 version/digest는 명시적으로 활성화해야 함. 사용자 `always-approve`, 권한 검사와 static 계약 호환성을 통과한 non-Network local derivative만 다음 Job에 승격 가능하며 실행 중 Job pin과 이전 activation rollback은 유지됨 |
| Platform과 release | 개발 검증은 있으나 Windows 폭, packaging, legal/provenance review, commercial release authorization은 별도 gate |
| Runtime 선택 | 실행 가능한 Noruct runtime은 하나이며 historical state compatibility는 rollback engine이 아닌 read/backup 경로 |
| Model Intelligence와 실행 라우팅 | 여러 adapter, explicit bounded fallback과 bounded advisory fan-out은 Job 전역 provider composition으로 존재합니다. Signed shared intelligence, local compatibility/outcome 보정, Employee/task별 frozen route, provider별 egress와 production multi-provider qualification은 개발 작업으로 남아 있습니다. |
| Operator surface | CLI와 TUI가 현재 로컬 surface다. loopback Graph Workbench는 좁은 GUI-ready projection과 future-Job constraint 경로를 검증하지만, 범용 desktop 또는 hosted GUI는 아니다. |

## 검증 가능한 제품 질문

Noruct의 핵심 가설은 의도적으로 검증 가능해야 합니다.

> 같은 model access, authority, total budget에서, 최소 충분 구조를 admission·staffing·validation·revision하는
> firm이 강한 single Employee보다 더 많은 검증된 가치를 만들 수 있는가?

많은 요청의 정답은 direct 또는 solo일 수 있습니다. 더 눈에 띄는 조직이 성공은 아닙니다. 추가 구조는 측정·설명·rollback
가능할 때에만 정당화됩니다.
