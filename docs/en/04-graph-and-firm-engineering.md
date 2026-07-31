# 04 — Graph & Firm Engineering

> [← Knowledge, Intent & Firm](03-knowledge-intent-firm.md) · [Index](README.md) · [Next: Governed Evolution & User Graphs →](05-governed-evolution-and-user-graphs.md) · [한국어](../ko/04-graph-and-firm-engineering.md)

Graph Engineering is not a visual graph product and not simply spawning several copies of one agent. It is the design of a dependency-aware execution structure. Firm Engineering extends it by making nodes persistent, heterogeneous capability units governed by one firm's authority and learning boundaries.

## Abstract

This paper distinguishes five layers of agent-system design. Graph Engineering concerns dependencies and artifact flow. Firm Engineering adds durable capability supply, authority, outcome history, and controlled evolution. The purpose is not maximal parallelism; it is to create only the structure whose marginal value can be stated and evaluated.

```mermaid
flowchart LR
  H[Harness Engineering\nreliable tool use] --> L[Loop Engineering\nreliable iteration]
  L --> G[Graph Engineering\ndependencies and parallel paths]
  G --> F[Firm Engineering\nheterogeneous persistent capability]
  F --> N[Network Engineering\nportable, governed capability]
```

## Why ordinary multi-agent graphs underperform

If every node uses the same tools, permissions, skills, model assumptions, and evaluation method, changing node names does not create meaningful independence. It often creates correlated error, role-play overhead, duplicated research, and false verification.

Firm Engineering asks a harder question for each node: what distinct capability, authority boundary, input contract, or verification method does this node contribute?

## A graph earns its complexity

The runtime adds a node only when one of these is true:

- work can proceed independently and a shorter critical path matters;
- a distinct capability or tool boundary is genuinely required;
- a diagnostic probe can reduce an otherwise unresolved uncertainty;
- an independent verification method materially changes confidence;
- a user-requested graph template has a valid operating purpose.

Otherwise, the correct graph is one node or no job graph at all.

## Admission test

```mermaid
flowchart TD
  W["Work order"] --> Q{"Is material value beyond direct or solo established?"}
  Q -->|"No or unknown"| S["Direct or solo path"]
  Q -->|"Yes"| D{"State the added node's value"}
  D -->|"Distinct capability"| T["Heterogeneous employee"]
  D -->|"Independent partition"| P["Bounded execution replicas"]
  D -->|"Different validator"| V["Independent verification"]
  D -->|"No concrete value"| S
  T --> A["Declared artifact handoff or aggregation"]
  P --> A
  V --> A
  A --> K["Kernel validates budget and authority"]
```

The test is intentionally stricter than “can we run agents in parallel?” A node earns admission only when the runtime can name the affected task, the expected benefit, the input/output contract, the aggregation owner, the stopping condition, and the budget/authority boundary. An unknown benefit returns to the direct or solo path; uncertainty is not itself evidence for a larger graph.

## Homogeneous execution can still have bounded value

Different nodes do not always need different Employee profiles. A single selected Employee may be instantiated more than once when the assignment itself contains safe parallel structure. This is a narrow execution optimization, not the source of Firm-level diversity.

```mermaid
flowchart TD
  T["One broad task"] --> Q{"Marginal value can be stated?"}
  Q -->|No| O["One execution instance"]
  Q -->|Partition| P1["Instance A\nscope 1"]
  Q -->|Partition| P2["Instance B\nscope 2"]
  Q -->|Candidate| C1["Instance A\ncandidate 1"]
  Q -->|Candidate| C2["Instance B\ncandidate 2"]
  Q -->|Diagnostic| D1["Instance A\nprobe 1"]
  Q -->|Diagnostic| D2["Instance B\nprobe 2"]
  P1 --> A["Declared aggregation task"]
  P2 --> A
  C1 --> A
  C2 --> A
  D1 --> A
  D2 --> A
  A --> R["One accepted result"]
```

The guardrails are structural:

- two to four run-only instances from one frozen Employee capability snapshot;
- non-overlapping partition scopes or explicitly comparable candidates or probes;
- the same authority and hard job budget, not a hidden budget multiplier;
- no instance may mutate the Employee, roster, Blueprint, or Playbook;
- all members must converge through a declared aggregation task;
- a distinct reviewer must use a materially different validator or capability, not merely another copy.

## Minimum-sufficient planning, performance-preserving execution

Managed jobs seek the minimum sufficient organization: the smallest structure that can preserve the accepted outcome and risk boundary. This is not cost-minimum planning. A concrete independent partition, candidate comparison, diagnostic probe, or materially different validator can justify more structure even when one run could technically finish. But possibility alone does not. Direct or solo remains the baseline until the additional value is stated.

Every additional execution instance needs an exact scope, expected gain, aggregation owner, hard ceiling, and stopping condition. The hard budget remains a ceiling, not a spending target. The preferred proposal is the smallest group that captures a concrete quality, coverage, recovery, or latency gain; a fourth run needs an explicit scope or candidate-set reason. If safe scopes and aggregation cannot be stated, the provider fails, the Kernel rejects admission, or the user requests single/no-parallel execution, the graph stays solo.

## Prove value under the same total budget

Replica count is not a success metric. A fair evaluation compares a single-instance run and a replicated run on the same workload, environment, Employee capability revision, and total hard budget. Communication, coordination, integration, verification, and human review burden all count against the replicated run.

The comparison observes accepted quality, coverage, lower-tail behavior, complete failure, safety and validation regressions, latency, total resource usage, and how much effort a person needs to understand and accept the result. One attractive result is not enough. Reusable eligibility requires matched evidence under a predefined gate, while any safety or validation regression is a reason to stop or roll back the candidate structure.

The evaluator produces evidence and a recommendation. It does not silently edit a Blueprint or promote a Playbook. That preserves the distinction between measuring an execution shape and granting it organizational authority.

## Manager attention is scarce

The Manager does not review every token or simulate conversations between employees. It intervenes at semantic boundaries: ambiguous goals, conflicting evidence, missing capability, authority escalation, a major graph revision, or a meaningful learning proposal.

This turns the Manager into a decision and routing capability rather than a permanent bottleneck.

## Evaluate the lower tail

A graph should be judged by more than average output quality. Its operating quality also includes failure predictability, cost, latency, correctionability, evidence quality, human review burden, and the ability to explain why a particular structure was used. Its terminal delivery should make the assignment rationale, AI contribution, required review points, excluded alternatives, comparative improvement, and human-readable conclusion inspectable without replaying raw logs. A design that is occasionally brilliant but often impossible to recover may be worse than a simpler, bounded path.

## Non-claim

Firm Engineering does not assume every domain needs narrowly human-like specialization. An Employee is differentiated only when a material operating difference exists; otherwise the system should retain one capability identity and, where justified, use temporary execution replication instead of inventing organizational theater.
