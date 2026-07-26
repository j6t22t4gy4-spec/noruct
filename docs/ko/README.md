# Noruct 공개 문서

> 정본: [English canonical documentation](../en/README.md) · 이 문서는 한국어 번역본입니다.

Noruct의 공개 문서는 제품이 무엇을 목표로 하고, 어떤 권한 경계와 운영 원칙을 지키는지 설명합니다. 구현 단계,
내부 설계 기록, 평가 결과 또는 운영 절차를 공개 계약으로 취급하지 않습니다.

| 순서 | 문서 | 질문 |
|---:|---|---|
| 00 | [North Star](00-north-star.md) | 왜 단일 agent가 아니라 회사 runtime을 만드는가? |
| 01 | [Dynamic Firm Runtime](01-dynamic-firm-runtime.md) | 회사의 지속 상태와 요청별 실행 구조는 어떻게 분리되는가? |
| 02 | [Persistent Employee](02-persistent-employee.md) | Employee는 무엇을 받고·사용하고·반환하는가? |
| 03 | [Knowledge, Intent & Firm](03-knowledge-intent-firm.md) | 지식, 사용자 방향, 실행 권한은 어떻게 연결되는가? |
| 04 | [Graph & Firm Engineering](04-graph-and-firm-engineering.md) | Graph Engineering은 단순 multi-agent와 무엇이 다른가? |
| 05 | [Governed Evolution & User Graph Control](05-governed-evolution-and-user-graphs.md) | 실행 구조는 어떻게 재사용·수정·공유하되 권한을 잃지 않는가? |
| 06 | [Epistemic Control, Oracle & Outcome](06-epistemic-control-and-outcome.md) | 무엇을 알고 모르며, 결과의 성공을 어떻게 판정하는가? |
| 07 | [Network Engineering](07-network-engineering.md) | 재사용 가능한 능력을 어떻게 공유하되 사용자 권한을 지키는가? |

## 공개 문서의 경계

이 문서는 제품 방향과 사용자에게 약속하는 동작 원칙을 설명합니다. source intake, runtime implementation,
보안 운영 세부, 벤치마크, 고객 데이터와 개발 이력은 포함하지 않습니다.

영문판과 번역판의 표현이 다를 경우 영문판이 정본입니다.
