# 08 — Current Development Status

> [← Network Engineering](07-network-engineering.md) · [Index](README.md) · [한국어](../ko/08-current-development-status.md) · [Next: Organization as Decision Architecture →](09-organization-as-decision-architecture.md)

**Last reviewed: 2026-08-01.** English is canonical.

## Purpose

This paper distinguishes the Noruct concept from the current development implementation. It is a bounded status
statement, not a commercial release announcement, benchmark claim, or guarantee that an optional capability is enabled
in every installation.

## Noruct 0.0.80 public developer preview

Noruct 0.0.80 has one public **unsigned developer-preview** wheel. Its integrity boundary is the published
SHA-256 plus independent public readback; it has no artifact signature or notarization claim. The qualified
scope is provider-free local runtime contracts on macOS arm64 with CPython 3.11. Windows remains unsupported or
experimental-disabled, and this statement does not qualify Linux, WSL, other architectures, hosted operation, or
production/enterprise use.

The preview qualifies frozen routing, receipt-bound continuation, bounded single-Job multi-route/provider contracts,
and exact version/digest pinning for external Tools, Skills, and Plugins. It does **not** qualify any live
provider/model without its own receipt. Multi-provider execution remains `EXPERIMENTAL / PROVIDER_DEPENDENT /
NOT_LIVE_QUALIFIED`; no heterogeneous or cross-provider quality claim is made. No signed network Model Intelligence
Snapshot is published or activated: bundled conservative defaults and explicit local routes remain the default.

This first public release has no prior signed public artifact. Its human-owned response posture is pause new installs,
remove the release asset when necessary, and disable future snapshot/route use; it is not evidence of a completed
rollback rehearsal. See the [0.0.80 preview release note](../../releases/noruct-0.0.80-developer-preview.md).

## Present development baseline

The local development runtime currently provides:

- one Company-facing CLI/TUI entry point with persistent Company, Roster, Playbook, Employee, session, and audit state;
- direct, managed solo, and bounded team execution under a deterministic authority, approval, and budget Kernel;
- material Employee capability profiles rather than role-name-only specialization, plus bounded same-Employee execution
  replicas for partitions, candidates, or diagnostics;
- typed task handoffs, a single final result owner, bounded retry/reroute/insert paths, and append-only Job audit;
- a bounded terminal execution-summary foundation and content-free receipt projections for operator review;
- user-governed Graph Blueprints with preview, constraints, revisions, forks, pins, and retained revision lineage;
- separate local Knowledge, Intent/Decision, and Firm state with bounded evidence bridges; and
- versioned Skill, Workflow, and Roster Patch proposals that require their own review and application lifecycle.

Noruct exposes one executable Employee Runtime. Historical employee-state compatibility can inspect a local state file and
create a verified backup receipt, but it is not an alternate engine, a runtime selector, or a way to bypass the current
authority and approval contract. Internally, CLI ingress, ACTIVE JOB audit, Graph Workbench presentation, and the runtime,
Company, and Knowledge projections are being separated behind the same local state authority. This is a product-surface
refactor: it lets the CLI, TUI, and a future GUI call the same governed operations rather than creating competing control
paths or a second Company state.

The Manager is implemented as a bounded persistent Company participant: it can interpret a Work Order, select an
execution shape, issue typed delegation, integrate accepted artifacts, and report. It cannot grant itself authority,
approve an external action, or directly mutate durable Company state.

## Evidence and non-claims

Provider-free regression suites, source integrity checks, Worker type checks, and local Worker route integration tests
are part of the development process. They demonstrate contract preservation and narrow integration behavior; they do
not establish broad real-world value.

In particular, Noruct does **not** currently claim that:

- a Manager-led firm or a team consistently outperforms a strong single Employee;
- every graph mutation is safe or generally available;
- a shared Network artifact may execute arbitrary publisher code, gain new permissions, or alter a running Job;
- Shared Evolution is a generally available customer service;
- every provider, operating system, GUI surface, deployment path, or commercial release gate is complete.

Manager and organization experiments retain negative-transfer results as evidence. A useful result in one bounded
evaluation is not sufficient to promote a default, a reusable Blueprint, a Skill, or a Roster change.

## Current boundaries

| Area | Current position |
| --- | --- |
| Manager and team value | Functional architecture, still under outcome qualification. |
| Organization fit and reviewable delivery | Capability-based assignment, bounded execution, terminal-summary, and receipt foundations exist. The immutable fit profile, frozen organization plan, complete six-question delivery, and human review-burden study are not complete product claims. |
| General semantic replanning | Bounded typed paths exist; broad autonomous rewriting is not claimed. |
| Graph mutation and recovery | Revision lineage and narrow receipt-bound continuation exist; in-flight or effectful replay is not silently resumed. |
| Knowledge | Local-first raw-source intake and bounded evidence use exist; extraction is not automatic truth. |
| Network | Signed artifact lifecycle and a limited deployed registry path exist; customer operation and broad executable adapters are not claimed. |
| Capability integrity | External versions and digests require explicit activation. Only non-Network local derivatives may advance for future Jobs under user-selected `always-approve`, authority checks, and static contract compatibility. Running Jobs remain pinned and prior activations remain rollback targets. |
| Platform and release | Development validation exists; Windows breadth, packaging, legal/provenance review, and commercial release authorization remain separate gates. |
| Runtime selection | One executable Noruct runtime; historical state compatibility is a read/backup path, not a rollback engine. |
| Model intelligence and execution routing | Multiple adapters, explicit bounded fallback, and bounded advisory fan-out exist as job-wide provider composition. Signed shared intelligence, local compatibility/outcome correction, Employee/task-specific frozen routes, provider-specific egress, and production multi-provider qualification remain development work. |
| Operator surfaces | CLI and TUI are the active local surfaces. A loopback Graph Workbench proves a narrow GUI-ready projection and future-Job constraint path; it is not a general desktop or hosted GUI. |

## The falsifiable product question

Noruct's central hypothesis remains deliberately testable:

> Under the same model access, authority, and total budget, can a firm that admits, staffs, validates, and revises the
> minimum sufficient structure produce more verified value than a strong single Employee?

The correct answer may be direct or solo execution for many requests. More visible organization is not success. The
runtime earns complexity only when its added structure can be measured, explained, and rolled back.
