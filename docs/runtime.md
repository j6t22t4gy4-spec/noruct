# Dynamic Firm Runtime

## Company Front Door

Every request first becomes a bounded Work Order containing the objective, expected outcome, explicit context
references, constraints, acceptance criteria, frozen authority identity, budget, and request time. The Work Order
is not a workflow. It is the stable input boundary from which coordination is admitted.

| Mode | Use when | Deliberately absent |
|---|---|---|
| **DIRECT** | one persistent employee can answer in one bounded run | compiler, team, graph, replanner |
| **SOLO JOB** | one employee needs managed tools, approval, budget, or audit | unnecessary team and parallelism |
| **TEAM JOB** | independent work, capability separation, or independent review creates measurable value | user-authored role topology |

## Dynamic Workflow Compiler

The compiler proposes; deterministic code decides.

1. Normalize the Work Order and freeze Company, Roster, authority, and budget identities.
2. Reserve the worst-case planning and Employee provider-call closure.
3. Request a bounded SOLO or GRAPH proposal when planning is justified.
4. Parse the typed proposal.
5. Validate task count, acyclicity, dependency reachability, capabilities, staffing, review separation, final owner,
   action authority, concurrency, calls, cost, and wall time.
6. Reject invalid or wasteful graphs without leaving partial execution state.
7. Dispatch only dependency-ready tasks.
8. Reconcile terminal results and accepted graph patches.
9. Integrate one final response with evidence and unresolved issues.

## Graph and loop engineering

One Employee runtime is a bounded loop:

```text
observe → reason → act → verify → repair or stop
```

The Firm Runtime connects those loops into a graph:

```text
route → assign → fan out → collect → review → fan in → integrate
```

The graph is execution state, not durable company knowledge. A successful graph can contribute evidence to a
Workflow Patch, but it is not copied directly into the Playbook.

## Runtime rewrites

Graph mutation is typed and bounded. The design vocabulary includes:

- **RETRY** — run a recoverable task again within its mutation limit.
- **REROUTE** — assign a task to a better eligible employee.
- **INSERT** — add a required task, commonly for a newly identified capability gap.
- **SPLIT / JOIN / MERGE / CANCEL** — structural graph operations admitted only under explicit triggers and limits.

An invalid, stale, or over-budget patch is rejected while the current valid graph remains authoritative.

## Staffing

Persistent employees are selected before temporary roles. A temporary specialist is created only for a typed
capability gap, is limited by the Job budget, and receives run-only session retention. Repeated need can become
evidence for a Roster Patch; it does not silently hire a permanent employee.

## Authority and cost

- Non-final research tasks receive only the minimum read authority they need.
- Mutation and external action authority remains with the final action owner.
- Mandatory independent review uses a different employee identity from the final writer.
- If the review boundary cannot be admitted, the fallback may report the constraint but has no action tools.
- Composite providers expose physical call ceilings for planning and Employee turns.
- Timeout, cancellation, or unknown provider cost cannot release reserved Company budget as observed zero.
- Non-finite costs are rejected.

## Product surface

The terminal interface keeps company status, current assessment, active work, approval, and one composer visible.
Settings separates account authentication from API/local connections. OpenAI account sign-in hands the terminal
to the user-managed provider CLI; Noruct does not read or store the credential. Model selection uses a bounded
local/discovered picker, and configuration changes apply to future Jobs only after explicit completion.
