# 08 — Current Development Status

> [← Network Engineering](07-network-engineering.md) · [Index](README.md) · [한국어](../ko/08-current-development-status.md)

**Last reviewed: 2026-07-29.** English is canonical.

## Purpose

This paper distinguishes the Noruct concept from the current development implementation. It is a bounded status
statement, not a commercial release announcement, benchmark claim, or guarantee that an optional capability is enabled
in every installation.

## Present development baseline

The local development runtime currently provides:

- one Company-facing CLI/TUI entry point with persistent Company, Roster, Playbook, Employee, session, and audit state;
- direct, managed solo, and bounded team execution under a deterministic authority, approval, and budget Kernel;
- material Employee capability profiles rather than role-name-only specialization, plus bounded same-Employee execution
  replicas for partitions, candidates, or diagnostics;
- typed task handoffs, a single final result owner, bounded retry/reroute/insert paths, and append-only Job audit;
- user-governed Graph Blueprints with preview, constraints, revisions, forks, pins, and retained revision lineage;
- separate local Knowledge, Intent/Decision, and Firm state with bounded evidence bridges; and
- versioned Skill, Workflow, and Roster Patch proposals that require their own review and application lifecycle.

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
| General semantic replanning | Bounded typed paths exist; broad autonomous rewriting is not claimed. |
| Graph mutation and recovery | Revision lineage and narrow receipt-bound continuation exist; in-flight or effectful replay is not silently resumed. |
| Knowledge | Local-first raw-source intake and bounded evidence use exist; extraction is not automatic truth. |
| Network | Signed artifact lifecycle and a limited deployed registry path exist; customer operation and broad executable adapters are not claimed. |
| Platform and release | Development validation exists; Windows breadth, packaging, legal/provenance review, and commercial release authorization remain separate gates. |

## The falsifiable product question

Noruct's central hypothesis remains deliberately testable:

> Under the same model access, authority, and total budget, can a firm that admits, staffs, validates, and revises the
> smallest useful structure produce more verified value than a strong single Employee?

The correct answer may be direct or solo execution for many requests. More visible organization is not success. The
runtime earns complexity only when its added structure can be measured, explained, and rolled back.
