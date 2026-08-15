# 06 — Epistemic Control, Oracle & Outcome

> [← Governed Evolution & User Graphs](05-governed-evolution-and-user-graphs.md) · [Index](README.md) · [Next: Network Engineering →](07-network-engineering.md) · [한국어](../ko/06-epistemic-control-and-outcome.md)

The central limitation of an AI firm is not that it cannot generate ideas. It is that it often cannot independently establish what is true, what should be valued, or whether a long chain of action genuinely succeeded.

## Abstract

This paper defines the control model for an AI system operating under incomplete information. Noruct does not solve the oracle problem by asserting confidence. It records the difference between evidence, inference, assumption, authorized decision, executable check, and later observed outcome.

## Epistemic status is first-class

Noruct should distinguish at least these states:

| Status | Meaning |
| --- | --- |
| Observed | Directly supported by a source or tool result. |
| Inferred | Reasoned from available evidence. |
| Assumed | Used provisionally because evidence is incomplete. |
| Decided | Chosen by an authorized owner. |
| Disputed | Contradicted or contested. |
| Stale | May no longer describe the current world. |
| Unknown | Not established. |

An answer that hides these distinctions is more dangerous than an answer that admits uncertainty.

## Four control artifacts

```mermaid
flowchart LR
  S[Sources and tool observations] --> E[Evidence Brief]
  E --> D[Decision Context]
  D --> O[Oracle / acceptance contract]
  O --> R[Outcome and feedback ledger]
  R --> E
  R --> D
```

- **Evidence Brief:** bounded claims, sources, freshness, contradictions, and uncertainty.
- **Decision Context:** the goal, constraints, owner, alternatives, and review date.
- **Oracle / acceptance contract:** what can be checked, by which method, and who accepts residual uncertainty.
- **Outcome and feedback ledger:** what happened after the decision or action, including corrections and delayed signals.

## What knowledge can help with—and cannot replace

A strong knowledge runtime can reduce missing context, preserve source provenance, surface contradictions, and remind the firm of prior decisions. It cannot create an objective answer for value conflicts, grant authority to make a legal or irreversible commitment, or guarantee independent verification when the same assumptions shape both execution and review.

For high-impact actions, Noruct should prefer deterministic tests, external data checks, sandboxed trials, independent evaluation methods, or explicit human acceptance. The right response to an absent oracle is often to narrow the claim or escalate—not to generate a more confident paragraph.

## Outcome is part of learning

The firm should separate “the output looked good” from “the action produced the intended effect.” Delayed feedback, user correction, real-world metrics, and failed acceptance should influence future recommendations. They should not be converted blindly into a permanent skill or policy without attribution and review.

## Mechanism, delivery, and outcome are different

| Claim layer | What establishes it | What it does not establish |
| --- | --- | --- |
| Mechanism proof | The declared contract and failure paths operated as designed. | That the artifact met acceptance or helped in the real world. |
| Delivery proof | The artifact and evidence passed the declared acceptance and verification methods. | That a delayed external outcome occurred. |
| Outcome proof | A matched external observation or later real-world signal supports the intended effect. | That the same structure will generalize to another context. |

A terminal delivery is itself an epistemic artifact. It should state what is `UNKNOWN`, what was `NOT_RUN`, and what remains `OUTCOME_NOT_ESTABLISHED`. It should answer the assignment, contribution, review, alternative, comparison, and conclusion questions defined in [Organization as Decision Architecture](09-organization-as-decision-architecture.md) using artifacts, evidence, validation, and bounded receipts—not hidden reasoning or a dump of raw prompts, transcripts, and tool logs. A successful mechanism is not automatically a successful delivery, and a successful delivery is not automatically a successful outcome.

## A limit-and-response matrix

| Limitation | What the runtime can contribute | What it must not claim |
| --- | --- | --- |
| Missing recorded context | Retrieve cited material and surface gaps | Recover unrecorded tacit knowledge |
| Competing evidence | Mark conflict, freshness, and uncertainty | Declare a winner without a basis |
| Value conflict | Produce options and consequences | Own the user's preference |
| No reliable acceptance test | Narrow the claim or escalate | Manufacture verification from self-review |
| Delayed real-world result | Keep an outcome ledger and review date | Treat terminal task success as real success |

This matrix is a product boundary. It explains why knowledge and verification improve a firm without making it an autonomous moral or legal actor.
