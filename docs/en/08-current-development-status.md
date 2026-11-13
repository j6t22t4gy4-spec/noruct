# 08 — Public scope and unverified claims

<a id="08--development-scope--non-claims"></a>

> **Document role:** Development scope and non-claims · **Status:** Public scope statement; not a current implementation inventory.
> [Document map](../README.md) · [Role reading path](../README.md#scope) · [Research manuscript](../literature/organization_governance_manuscript.md)

> [← Network Engineering](07-network-engineering.md) · [Index](README.md) · [한국어](../ko/08-current-development-status.md) · [Next: Organization as Decision Architecture →](09-organization-as-decision-architecture.md)

**Last reviewed: 2026-08-15.** English is canonical.

## Question addressed

**What can and cannot be established from this documentation?**

Distinguishes concepts, implementation evidence, and measured results. The original review date is retained; this is not a live feature inventory.

[Shared terminology](README.md#terms) · [Reading paths](README.md#reading-paths)

## Purpose

This guide keeps an important distinction clear: Noruct's public concepts describe a target operating model and
its safety boundaries. They are not a software artifact, release announcement, benchmark result, availability
promise, or commercial-readiness claim.

## Product-surface principle

Noruct treats the product application and the execution engine as separate but cooperating layers:

- the product application owns the user's visible conversation, project choice, drafts, local workspace views,
  terminal layout, and command admission;
- the execution engine owns model/tool execution, checkpoints, effect transactions, policy enforcement, and
  verifiable execution receipts; and
- a command that can cause work must retain one reviewable intent and one execution record across interactive,
  headless, and editor-connected surfaces.

This separation does not mean that every screen has execution authority. A local terminal may support user-mediated
shell work, while a managed Job begins only through the product's governed command path. An externally observed
coding session is neither a managed Job nor an approval channel: `blocked`, disconnected, or missing external status
is only information for the user, never proof that a Job should be resumed, approved, retried, or marked successful.

## Development and qualification boundary

Implementation and qualification may proceed at different rates. A local feature, fixture, or internal receipt can
show that a narrow contract was exercised; it cannot by itself establish broad reliability, customer availability,
commercial suitability, or superior organizational performance.

In particular, these public documents do **not** claim that:

- a Manager-led firm or a team consistently outperforms a strong single Employee;
- every graph mutation, continuation, tool, provider, or platform is safe or generally available;
- an external terminal, provider, plugin, Network artifact, or shared service may gain authority over a local firm;
- Shared Evolution, account sync, marketplace, relay, telemetry, or automatic update is a customer service;
- a specific package, operating system, desktop surface, deployment path, legal review, or commercial release gate
  has been completed.

## Conceptual maturity model

| Area | Public position |
| --- | --- |
| Minimum sufficient organization | More structure requires a declared partition, candidate comparison, diagnostic, verification, or recovery value. Solo remains the baseline when that value is unknown. |
| Employee capability | Employees receive bounded assignments, permitted capabilities, relevant evidence, and acceptance conditions; they cannot grant themselves authority or rewrite durable organizational identity. |
| Graphs and Blueprints | A Blueprint is a user-visible reusable hypothesis. A Job graph and Run record preserve what was actually admitted and executed. |
| Knowledge and outcome | Extraction, inference, delivery, and delayed outcome remain distinct. A plausible answer or completed mechanism is not automatically verified value. |
| Recursive improvement | External artifacts are explicitly reviewed, pinned, and future-Job scoped. Locally derived candidates need compatibility and authority checks before any permitted advancement. |
| Network | Distribution is optional availability, not remote execution or local authority. Private content, credentials, active work, and unstated context remain local. |
| Product and terminal surfaces | Product views may organize work, but they do not invent execution results. External terminal observation remains display-only until an independently governed capability is introduced. |

## The falsifiable product question

Noruct's central hypothesis remains deliberately testable:

> Under the same model access, authority, and total budget, can a firm that admits, staffs, validates, and revises the
> minimum sufficient structure produce more verified value than a strong single Employee?

The correct answer may be direct or solo execution for many requests. More visible organization is not success. The
runtime earns complexity only when its additional structure can be measured, explained, and rolled back.
