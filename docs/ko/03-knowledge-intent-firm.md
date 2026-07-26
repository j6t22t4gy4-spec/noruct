# 03 — Knowledge, Intent & Firm

> 정본: [English](../en/03-knowledge-intent-firm.md) · [← Persistent Employee](02-persistent-employee.md) · [문서 인덱스](README.md) · [다음: Graph & Firm Engineering →](04-graph-and-firm-engineering.md)

Noruct는 지식 저장소와 실행 회사를 섞지 않습니다. 무엇을 아는지, 무엇을 원하는지, 지금 무엇을 실행할 수
있는지는 서로 다른 권한을 가진 세 plane입니다.

사용자의 PDF, 문서, 이미지, 링크, 메모처럼 날것으로 들어온 자료는 Knowledge Runtime의 원천 자료입니다.
그 자체가 모든 작업에 자동으로 주입되는 prompt나 실행 명령이 되지 않습니다. 필요한 경우에만 근거·최신성·모순·불확실성을
포함한 짧은 Evidence Brief로 만들어 Intent와 Firm에 전달합니다.

```mermaid
flowchart LR
  K["Knowledge\nclaims · sources · conflict · freshness · unknown"] --> B["Bounded Evidence Brief"]
  I["Intent & Decision\ngoals · priorities · constraints · review dates"] --> D["Decision context"]
  B --> D
  D --> F["Firm Runtime\nManager · Employees · Kernel"]
  F --> O["Result and observed outcome"]
  O -->|"reviewed evidence only"| K
```

## 명시적 Bridge

세 plane의 연결은 자동 혼합이 아니라 명시적 bridge입니다. 예를 들어 “이 PDF를 가격 전략 지식에 넣어”는 Knowledge
작업이고, “8월에 가격 결정을 재검토해”는 Intent의 일정화된 결정 기록이며, “그때 필요한 조사만 회사에 맡겨”가 Firm
Runtime의 실행 요청입니다. 실행 결과는 곧바로 지식이나 정책이 되지 않고, 각 plane에서 검토할 후보가 됩니다.

## Knowledge — What do we know?

Knowledge는 사용자가 가진 문서·근거·조사와 그 상태를 다룹니다. 중요한 것은 단순히 많은 문서를 context에 넣는
것이 아닙니다. claim, source, conflict, freshness, assumption과 unknown을 구분하고, 현재 task에 필요한 작은
Evidence Brief만 제공합니다.

Knowledge는 사용자 목표, 실행 권한 또는 최종 결정을 자동으로 만들지 않습니다.

## Intent & Decision — What do we want to do?

Intent & Decision은 목표, 우선순위, 제약, 결정, 보류, 책임자, 재검토 시점을 다룹니다. 예를 들어 자료는
“경쟁사가 가격을 올렸다”를 말할 수 있지만, 가격을 유지할지 바꿀지는 사용자·조직의 방향과 책임 문제입니다.

## Firm Runtime — What should we execute now?

Firm Runtime은 Manager, Employee와 Firm Kernel이 현재의 bounded task를 실행하는 plane입니다. Knowledge에서 필요한
근거를 받고 Intent의 제약을 존중하지만, 전체 지식이나 목표를 자기 권한으로 바꾸지 않습니다.

## 사용자 권한

사용자는 Mission, Authority, Accountability를 계속 소유합니다.

- 무엇을 우선할지와 어떤 trade-off를 받아들일지
- 어느 권한과 비용까지 허용할지
- 외부 약속, 비가역 action, 새로운 권한을 승인할지
- 결과를 현실의 성공으로 인정할지

Noruct는 명확하고 검증 가능하며 되돌릴 수 있는 영역에서는 자율 실행을 지향합니다. 가치 충돌, 불명확한 성공
기준, 큰 비용과 비가역 행동의 경계에서는 사용자의 판단을 대체하지 않습니다.
