# Noruct product concepts and design

<a id="noruct-a-concept-paper-series"></a>

> **Role:** Product-document guide, not a research-paper index.
> [Full document map](../README.md) · [Research and literature](../literature/README.md) · [한국어 번역](../ko/README.md)
> **Canonical language for product documents:** English. Research documents are Korean originals.

<a id="abstract"></a>
<a id="central-thesis"></a>
<a id="how-to-read-this-series"></a>
<a id="concepts-purpose-and-principles"></a>
<a id="design-components-and-operating-boundaries"></a>
<a id="scope-how-to-interpret-the-claims"></a>

## What Noruct is intended to do

Noruct is designed to find relevant evidence, divide work where useful, perform permitted actions, and verify the outcome of a user's request. Validated procedures and necessary records may survive a task; its temporary plan must not become authority for future work.

For example, checking which orders a delayed delivery affects may require only one lookup. Conflicting inventory and production records may justify separate checks followed by reconciliation. Changing a delivery date requires different permission from reading it. This illustrates intended behavior, not measured field performance.

<a id="reading-paths"></a>
## Reading paths

Start with **00 → 09 → 01**: purpose, the case for delegation, then execution design. Reading the entire series numerically is unnecessary.

| Question | Guides | Scope |
| --- | --- | --- |
| What is being built, and why delegate? | [00 Purpose](00-north-star.md) → [09 Delegation benefits and costs](09-organization-as-decision-architecture.md) | Product concepts |
| How do work and capabilities persist? | [01 Execution control](01-dynamic-firm-runtime.md) → [02 Reusable workers](02-persistent-employee.md) → [04 Dependencies](04-graph-and-firm-engineering.md) | Execution design |
| How does evidence differ from permission? | [03 Evidence, decisions, authority](03-knowledge-intent-firm.md) → [06 Outcome checks](06-epistemic-control-and-outcome.md) | Evidence and verification |
| When can experience or external procedures be reused? | [05 Changes and reuse](05-governed-evolution-and-user-graphs.md) → [07 External tools](07-network-engineering.md) | Review and activation |
| Which models may receive which data? | [10 Model selection](10-model-intelligence-and-multi-provider-orchestration.md) | Model and transfer boundaries |
| What has actually been established? | [08 Public scope](08-current-development-status.md) | Concepts, implementation, results |

Numbers remain stable document identifiers. The [research manuscript](../literature/organization_governance_manuscript.md) contains the theoretical argument; the [experiment path](../README.md#experiments) contains planned measurements. This guide does not duplicate either.

<a id="terms"></a>
## Shared terminology

Function-oriented terms come first. Implementation names below support cross-reference to detailed design and code; their presence is not evidence of implementation.

| Reader-facing term | Design name | Meaning |
| --- | --- | --- |
| Company operating rules | `Company` | Persistent goals, permissions, and budget boundaries; not a legal company. |
| Job | `Job` | A task with completion criteria, which may outlive one execution. |
| Reusable worker | `Employee` | A reusable unit with approved tools, procedures, and bounded memory; not a human employment relationship. |
| Execution attempt | `EmployeeRun` | One attempt by a worker to perform an assignment. |
| Planning and coordination | `Manager` | Divides work and proposes actions; a proposal does not grant permission. |
| Permission and state checks | `Kernel` | Checks whether an action is permitted under current rules and state. |
| Work order | `Work Order` | Records the goal, scope, constraints, and completion criteria. |
| Task dependencies | `Graph` | Represents prerequisites, handoffs, and potential parallel work. |
| Reusable plan | `Blueprint` | A plan available for review; storing it grants no execution authority. |
| Reusable procedure | `Skill` | A versioned procedure for tool use or verification. |
| Proposed change | `Patch` | A proposed change, distinct from review and activation. |
| Execution record | `Receipt` | Records actions and observations; the existence of a record does not prove success. |
| Outcome criterion | `Oracle` | A test criterion or external outcome source, not an infallible AI. |
| Evidence summary | `Evidence Brief` | A task-relevant summary with sources, freshness, and uncertainty. |
| Model provider and route | `Provider / route` | The service, model, and execution configuration; data-transfer permission is checked separately. |
| Version pin | `Pin` | Fixes use to an exact reviewed version; updates and approval are separate. |
| Finalizing a change | `Commit` | Applies a proposal to an authoritative artifact or external state; not only a Git operation. |
| Operating contract | `Contract` | Defines inputs, permitted actions, outputs, and responsibilities; distinct from legal contracts. |

## Public-document boundary

This repository publishes product concepts, operating boundaries, and architectural principles. It does not publish customer data, private development records, internal operational configuration, security-sensitive implementation detail, or a promise of autonomous authority.

The [scope guide](08-current-development-status.md) defines non-claims, not a current implementation inventory.
Guides 01 and 10 contain development statements, but this public collection does not provide their underlying private evaluation records. Concept and design descriptions do not establish general availability or commercial qualification.
