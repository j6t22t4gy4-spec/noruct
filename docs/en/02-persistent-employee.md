# 02 — Persistent Employee

> [← Dynamic Firm Runtime](01-dynamic-firm-runtime.md) · [Index](README.md) · [Next: Knowledge, Intent & Firm →](03-knowledge-intent-firm.md) · [한국어](../ko/02-persistent-employee.md)

An Employee is a persistent capability unit, not a persona prompt and not merely a differently named copy of the same agent.

## Abstract

This paper treats an Employee as a durable, inspectable capability boundary. Persistence does not mean unlimited autobiography. It means that identity, approved procedures, private bounded memory, session continuity, permissions, and outcome references can be revised deliberately rather than reconstructed from a role name on every request.

```mermaid
flowchart LR
  I[Scoped assignment] --> E[Employee]
  K[Knowledge brief and authority] --> E
  E --> T[Allowed tools and skills]
  T --> O[Result artifact]
  E --> X[Evidence, assumptions, confidence, unresolved items]
  O --> H[Handoff or final synthesis]
  X --> H
  X -. reviewed learning only .-> S[Approved employee skill or memory]
```

## What makes an employee distinct

An employee has a bounded operating identity:

- a capability contract and task suitability;
- a deliberate set of skills and tool permissions;
- a bounded memory and correction history;
- an input/output and handoff contract;
- evidence and quality expectations;
- explicit limits on authority, cost, and external action.

This is more substantive than assigning labels such as “researcher” and “reviewer” to identical instances. Specialization should change what an employee can reliably do, what it receives, how it evaluates work, or what it is allowed to affect.

## Employee contract

| Receives | Uses | Produces |
| --- | --- | --- |
| A scoped assignment, constraints, relevant evidence, and acceptance criteria | Approved skills, permitted tools, bounded memory, and a workspace | An artifact, evidence references, assumptions, confidence, limits, and a receipt |

Employees do not own the company mission, final external commitments, unrestricted credentials, or unbounded long-term memory. Those belong to the user and the Firm Kernel.

## Private state is not the user's knowledge base

An Employee may have personal operating state, but it is not a second copy of the user's files. The system should keep these stores separate because they answer different questions and have different retention rules.

```mermaid
flowchart TB
  K["User Knowledge Runtime\nsource material · facts · evidence"] -->|"bounded cited brief"| R["Frozen Employee Run"]
  I["Employee identity\nrole · capability contract"] --> R
  M["Employee private memory\napproved corrections · local operating facts"] -->|"task-relevant selection"| R
  S["Employee skills\nversioned procedures · verification steps"] --> R
  H["Employee session\nrecent task history"] -->|"bounded projection"| R
  R --> O["Artifact, receipts, uncertainty"]
  O -. "reviewed candidate only" .-> M
  O -. "reviewed procedure change only" .-> S
```

The separation matters. User Knowledge is evidence about the world. Employee memory is private operational context. A Skill is a reusable procedure. A session is continuity for interaction, not a permission to retain everything forever. None is a source of Company authority.

## A memory admission test

Before information becomes durable Employee memory, the firm should be able to answer four questions: Is it useful beyond the current job? Is it attributable to an observable correction or repeated result? Is it safe for this employee alone to retain? Can it be versioned, reviewed, and rolled back? A raw transcript, an untrusted document, or a convenient model inference fails this test by default.

## The Manager is also an employee

The Manager is a persistent employee with a different contract, closer to a chief-of-staff than a theatrical supervisor. It can invest more reasoning in problem framing, capability selection, escalation, and cross-job learning. It should not narrate fake meetings or duplicate task work already owned by a specialist.

## Growth without uncontrolled accumulation

Most outputs should end as job artifacts. A durable employee change requires stronger evidence: repeated usefulness, a clear correction signal, compatible authority, and a reversible versioned update. That update may improve a skill, memory policy, or capability contract, but it should not silently turn a temporary behavior into permanent identity.

## Handoffs over role-play

Collaboration is valuable when an artifact crosses a real interface: research becomes a cited brief, a diagnosis becomes a patch proposal, or an implementation becomes a testable change. The handoff should include what was found, how certain it is, what remains unknown, and what the next owner must verify. Conversation without an interface is usually cost, not coordination.

## One Employee, several execution instances

An Employee identity and an Employee execution instance are different objects.

| Object | Persists? | Owns distinct capability? | Purpose |
| --- | --- | --- | --- |
| Employee | Yes, until deliberately revised or retired | Yes | Reusable capability, tools, skills, bounded memory, authority, and outcome history |
| Execution instance | No; job- or attempt-scoped | No; it receives a frozen snapshot of the Employee | Perform one bounded assignment inside a job graph |

The firm may place several instances of the same Employee in one job only when the graph can name the marginal value:

- **Partition:** non-overlapping scopes can shorten the critical path or increase coverage.
- **Candidate:** bounded alternatives can be compared by a declared acceptance method.
- **Diagnostic:** independent probes can reduce a specific unresolved uncertainty.

For managed work, this check is performance-first: one instance being technically able to finish does not prove it is sufficient. The comparison includes expected accepted quality, coverage, diagnostic recovery, and useful latency within the unchanged hard ceiling.

The instances remain read-only with respect to Employee identity, skills, memory policy, permissions, and roster state. They do not converse to manufacture diversity. Their artifacts flow into a declared aggregation task, and aggregation cost is part of the job budget.

A replicated run is therefore not a new Employee, a promotion signal, an independent reviewer, or evidence that the roster should grow. Only repeated outcome evidence can justify a later Skill, Workflow, or Roster proposal.

## Evaluation implication

Employee differentiation is credible only when a capability difference is observable at the input or output boundary: a different permitted tool, procedure, memory scope, validator, environment, or measured outcome profile. A renamed instance with the same frozen inputs is an execution replica, not a second employee.
