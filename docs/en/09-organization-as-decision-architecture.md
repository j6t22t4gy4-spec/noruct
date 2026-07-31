# 09 — Organization as Decision Architecture

> **Canonical:** English · [한국어](../ko/09-organization-as-decision-architecture.md) · [← Current Development Status](08-current-development-status.md) · [Documentation index](README.md)

## Central claim

An organization is not a list of titles. It is a runtime arrangement of work, information, authority, coordination,
verification, and learning.

Calling several model instances a manager, researcher, or reviewer does not create an organization. Those names become
structural only when the instances receive materially different assignments, information, capabilities, permissions,
artifacts, validation duties, and escalation paths.

Noruct therefore treats organization design as **decision architecture**:

> **Distribute reversible cognition, make shared state explicit, centralize irreversible commitment, keep verification
> independent, and change only the job-local execution organization dynamically.**

This paper is a conceptual model. It does not claim that every mechanism is generally available or that a managed team
is universally better than a strong single Employee.

## Four levels that should not be confused

“Multi-agent” often combines systems with very different organizational depth.

| Level | Structure | Meaning in Noruct |
|---|---|---|
| 0 — Ensemble | Independent samples followed by voting or selection | A bounded candidate replica can be an ensemble without becoming a team |
| 1 — Workflow | A predetermined sequence of model or tool nodes | A frozen Job Graph may be a workflow rather than an organization |
| 2 — Organization | Different local state, information, authority, and assignments with bounded runtime reconfiguration | A heterogeneous Team and typed semantic replan belong here |
| 3 — Institution | Identity, admission, audit, rule change, and learning persist across Jobs | Company, Roster, persistent Employee, and governed Patch lifecycle belong here |

Noruct aims at the institutional level, but not every Noruct execution is an organization. Direct work remains direct,
a replica remains a replica, and a fixed graph does not become firm intelligence merely because its nodes use models.

## The seven structures of an AI organization

A single organization chart or communication graph is insufficient. A governed agent organization contains at least
seven related but distinct structures.

| Structure | Question | Noruct concept |
|---|---|---|
| Task dependency | Which output becomes which input? | Job tasks, dependencies, final task, and revision lineage |
| Assignment | Which execution identity owns each task? | Staffing, delegation, Employee snapshot, and attempt |
| Information access | Which source, memory, and tool result may each Employee see? | Bounded Context, Knowledge scope, private Memory, and Tool projection |
| Communication | Who may send which typed artifact to whom? | Dependency artifact, evidence handoff, and Manager integration lane |
| Decision and execution authority | Who may propose, admit, approve, execute, and commit? | User authority, Company policy, Firm Kernel, ActionPolicy, and effect owner |
| Verification | Who checks which result by what independent method? | Acceptance, validator, reviewer edge, evaluator, and receipt |
| Memory and learning | Which observation may alter future capability? | Episode, evidence, Patch, revision, future snapshot, and outcome |

Budget and reconfiguration rules constrain all seven structures. They are not another model's discretionary state.

~~~mermaid
flowchart LR
  W["Frozen Work Order"] --> T["Task dependencies"]
  W --> X["Assignment"]
  W --> I["Information access"]
  W --> C["Communication"]
  W --> D["Decision and execution authority"]
  W --> V["Verification"]
  W --> M["Memory and learning lineage"]
  T --> P["Read-only organizational projection"]
  X --> P
  I --> P
  C --> P
  D --> P
  V --> P
  M --> P
~~~

These structures should be inspectable together but must not become one mutable authority. Editing a task topology must
not silently grant data access, change a permission, approve an effect, or rewrite organizational memory.

## Constitution and execution organization

The most robust general form is not a fixed corporate pyramid and not an unrestricted peer network. It is a stable
constitutional control layer combined with a dynamic execution organization.

| Stable across work | Composed for one Job |
|---|---|
| Mission interpretation principles | Task decomposition |
| Authority and data-access ceilings | Employee and replica selection |
| Prohibited actions | Job-local information projection |
| Budget and approval rules | Communication topology |
| Audit requirements | Execution order and concurrency |
| Rule-change and rollback procedure | Verification depth and replan conditions |

The Manager or organization compiler may choose and narrow the Job-local structure. It may not invent new authority.
The deterministic Firm Kernel validates the proposal against the frozen constitution and seals the executable snapshot.

~~~text
Company constitution
→ Manager / organization proposal
→ deterministic authority, budget, and risk admission
→ bounded Employee execution
→ typed artifacts and independent verification
→ one final owner and limited effect execution
→ audit and optional evidence-bound future Patch
~~~

## Diagnose the task before composing the organization

No topology is universally best. Organization choice depends on the task.

| Task property | Structural implication |
|---|---|
| High decomposability, low mutual dependence | Independent parallel work with one integrator |
| Strong sequential dependence | Pipeline with typed stage contracts and checkpoints |
| High mutual dependence or context coupling | One strong Employee or a very small shared-state team |
| Highly distributed information | Put bounded judgment near the information |
| High verifiability | Prefer deterministic validation before another general model |
| High risk or irreversibility | Distributed proposals, independent verification, single effect authority |
| High error correlation | Change source, model, tool, context, or validation method rather than adding labels |
| High environmental volatility | Bounded reallocation and explicit replan conditions |
| High latency sensitivity | Fewer handoffs and more local reversible discretion |

Noruct is intentionally stronger at risk, authority, and verifiability than at estimating error correlation, information
dispersion, context coupling, and coordination latency. A safe graph admission mechanism is not yet a complete
organization optimizer.

## Use the minimum sufficient organization

The task diagnosis should not select the most elaborate structure it can justify. It should select the **smallest
structure that can satisfy the Work Order with acceptable risk**.

Strong Solo is the default when evidence is weak or important task properties are unknown. Additional Employees,
replicas, Manager steps, or reviewers require a material reason such as independently decomposable work, genuinely
distributed information or capability, a different verification path, or a latency gain that exceeds handoff and
integration cost.

Organization selection should therefore distinguish four states:

| State | Meaning |
|---|---|
| `SOLO_REQUIRED` | Evidence is weak, negative, or too coupled for additional structure |
| `EXPERIMENT_ELIGIBLE` | A bounded comparison is justified, but the structure is not a default |
| `OBSERVE_ONLY` | Existing episodes may be evaluated but must not drive automatic staffing |
| `AUTO_REUSE_ELIGIBLE` | A matched outcome gate supports reuse only in the same evidence-bound context |

Repeated execution is not sufficient for automatic reuse. Promotion requires a matched baseline that includes
lower-tail quality, complete and safety failure, total cost, and human review burden. If the comparison cannot establish
an outcome, the honest state is `OUTCOME_NOT_ESTABLISHED`.

The resulting organization plan is a frozen, read-only projection of task, assignment, information, artifact,
authority, verification, and learning routes. It references the original Work Order, Roster, ActionPolicy, Job Graph,
budget, and validator revisions rather than becoming a new mutable authority.

## Bound each execution unit before increasing reasoning

A safer execution unit is not a large prompt that asks a model to be careful. It is a closed contract that identifies:

- one task and one completion goal;
- exact input revisions or evidence digests;
- the files, artifacts, and tools that are in scope;
- the authority, features, and targets that are out of scope;
- one primary output and observable acceptance condition;
- one focused verification; and
- explicit failure, stop, and escalation conditions.

One Work Order or implementation objective may require several sequential execution units. Each unit should own one
authority boundary, one primary output, and one focused check. A failure outside that boundary is reported rather than
used as permission to explore or modify adjacent systems.

This prevents an unclear task from being hidden behind more context, more retries, or more model reasoning. The firm
should first improve the task contract, fixture, and verification boundary. It should escalate only when the same
bounded problem remains difficult, when authorities conflict, when the work cannot be split safely, or when a person
must exercise external, legal, financial, or release authority.

## Deliver an explanation, not a replay

Append-only receipts and audit events are necessary for reconstruction, but they are not a useful default review
surface. Every terminal delivery should derive a short, honest explanation from bounded evidence and answer six
questions:

1. Why was this Employee or execution structure selected?
2. What did AI actually author, propose, select, execute, and integrate?
3. What are the maximum three things a person must review?
4. Which material alternatives were excluded, and why?
5. Did the organization improve on a matched baseline, or is that still unknown?
6. What conclusion and next safe action should a person take?

The explanation should resemble a good pull-request description: requested scope, selected approach, actual
contribution boundaries, review focus, validation performed, material alternatives, comparative evidence, remaining
risk, and the next action. It should not reproduce raw prompts, transcripts, unbounded tool output, or hidden reasoning.

If evidence is absent, the explanation says `UNKNOWN`. If a check was not executed, it says `NOT_RUN`. If no valid
comparison exists, it says `OUTCOME_NOT_ESTABLISHED`. A plausible retrospective story is not a substitute for a receipt.

Review burden is part of organizational performance. Review wait, reopened evidence, rework, unused sub-artifacts,
duplicate source reads, false rejection, and defects uniquely found by an independent method belong in the same total
cost view as execution, communication, and integration.

## Manager as exception compression

A Manager is useful only when it transforms organizational state.

~~~text
routine dependency and retry
→ deterministic Kernel

novel ambiguity, capability gap, conflict, or changed assumption
→ Manager semantic judgment

repeated exception
→ evaluated rule, Skill, Workflow, or Roster Patch candidate
~~~

A Manager that merely relays messages is a delay layer. A Manager that reviews every tool event is a central bottleneck.
Manager value should be understood as avoided graph work, better staffing and integration, resolved exceptions, and
future exception reduction minus planning, supervision, queueing, and compression loss.

This is why adding another management layer is not the default response to scale. First reduce repetitive exceptions,
make handoffs explicit, and measure the remaining exception load.

## Communication is lossy compression and a security boundary

Typed artifact exchange is preferable to unrestricted agent conversation because it limits role-play cost, context
infection, and unclear ownership. A useful handoff preserves:

- claims and cited evidence;
- assumptions and their scope;
- unresolved questions and uncertainty;
- artifact and source revision;
- validation status and required downstream action.

But any handoff is compression. Information may be available upstream, transmitted correctly, and still fail to be
integrated into the final decision. Noruct must distinguish:

~~~text
information available
≠ information transmitted
≠ information integrated
≠ conclusion verified
~~~

Communication topology also defines the blast radius of invalid or hostile information. Information access,
communication permission, and execution authority must remain separate. A highly connected hub must not automatically
become the most informed node, the final decision maker, and the effect executor at the same time.

## Verification should target error structure

Adding a “critic” label does not create independent verification. Independence can come from a different source,
model, tool, context boundary, validation algorithm, success criterion, or execution permission.

Verification should also be selective. Repeating every task twice is usually wasteful. Independent computation is most
valuable at:

- assumptions whose failure propagates through the whole graph;
- conflicting or stale evidence;
- outputs with weak semantic acceptance;
- irreversible external effects;
- expensive calculations or changes with limited rollback;
- final selection among materially different candidates.

The relevant measure is not reviewer count. It is whether a different error path detected a defect without creating
excessive false rejection and coordination cost.

## Organizational surplus and total cost

Multiple Employees are justified only when they create surplus over a strong single-execution baseline under the same
authority and total budget.

~~~text
organizational surplus
= quality, coverage, safety, recovery, or useful latency gain
- execution, communication, integration, verification,
  governance, exception, and failure cost
~~~

Model calls and average quality are not enough. Useful evidence also includes unused artifact rate, duplicate source
reads, integration rejection, reviewer detection and false-refusal rates, exception escalation, Manager queueing,
error propagation depth, and whether the best specialist contribution reached the final result.

One aggregate score hides tradeoffs. Quality, cost, latency, safety, and auditability should be read as a Pareto
relationship.

## What Noruct should improve conceptually

The next organizational advance is better observation before greater autonomy.

1. Derive a conservative fit profile and frozen read-only organization plan from existing authorities.
2. Record why each Employee was selected, what AI actually contributed, and which material alternatives were excluded.
3. Produce the six-question delivery with a maximum of three human review priorities.
4. Treat handoff as a meaning-preservation contract and observe missing context without retaining hidden reasoning.
5. Move from static capability difference to measured error diversity and verification contribution.
6. Evaluate the Manager as an exception economy rather than a central intelligence.
7. Attribute communication, integration, governance, exception, and human review cost alongside execution usage.
8. Add freshness and confidence decay to long-lived capability evidence and preserve explicit evaluation lineage.
9. Extend broad execution-shape comparisons with controlled ablations of information, communication, verifier, and selector.

This direction does not require a single mutable organization graph. The projection observes; the existing authority
owners continue to decide.

## What this concept rejects

- adding titles or personas as a substitute for material capability;
- making Team the default for every request;
- treating free-form agent meetings as the primary coordination primitive;
- allowing a Manager to approve its own authority or irreversible effects;
- merging task, data, permission, verification, and learning into one mutable graph;
- exposing hidden reasoning as organizational memory;
- compensating for an undefined task with more context, retries, or reasoning;
- presenting raw logs as the human-readable conclusion;
- promoting a workflow or Employee from one favorable campaign;
- calling every graph, replica, or pipeline an organization.

## Source literature reviews

This concept paper is derived from two Korean-language long-form reviews. They are published separately to preserve
their original arguments and literature maps without turning source claims into product authority:

- [AI Agent Organization Design Principles (Korean)](../literature/organization-as-decision-structure.ko.md)
- [LLM Multi-Agent Operating Structures and Design (Korean)](../literature/from-human-organization-to-ai-agent-organization.ko.md)

## Final position

Noruct already has the conceptual ingredients of a decision-structured firm: persistent identities, frozen assignments,
bounded information, typed artifacts, deterministic authority, independent verification paths, and evidence-gated
evolution. Its weaker area is not the absence of more agents or hierarchy. It is the ability to explain, in one view,
where information was lost, where exceptions accumulated, whether verification was genuinely independent, and whether
the organization created surplus after total coordination cost.

The next step is therefore not a larger organization. It is a firm that selects the minimum sufficient organization,
observes its decision architecture, changes only the job-local structure that evidence justifies, and explains the
result in a form a person can review.
