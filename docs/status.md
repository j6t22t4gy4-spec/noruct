# Current Status

Last updated: **2026-07-25**

This is a public status summary for the separate Noruct development codebase. The implementation is not included
in this documentation-only repository, so these statements are project status claims rather than independently
reproducible release evidence from this repository.

## Implemented in the development codebase

- One company CLI/TUI with persistent sessions and Company state.
- Versioned COMPANY, ROSTER, PLAYBOOK, Employee Skill, and ACTIVE JOB audit state.
- Company Front Door with graphless DIRECT, managed SOLO JOB, and validated TEAM JOB paths.
- Minimum staffing, capability-gap temporary specialists, dependency-ready concurrency, and one final writer.
- Production RETRY, REROUTE, and capability-driven INSERT paths with bounded graph mutation.
- Skill, Workflow, and Roster Patch proposal, approval, application, observation, and rollback lifecycles.
- Local Knowledge Asset intake and Knowledge / Intent / Decision / Firm bridges.
- Provider profiles, user-managed OpenAI account login, API/local connection settings, and model selection.
- Approval-gated workspace and external action boundaries.
- Local Shared Evolution client and versioned artifact lifecycle, disabled by default.

## Partial or not yet qualified as a commercial release

- General semantic replanning is not complete. The default production replan is currently the bounded
  `CAPABILITY_MISSING` insertion lane; broader assumption, constraint, stalled-graph, validation, and user-correction
  planners remain limited or fixture-level.
- SPLIT, JOIN, MERGE, and graph-level CANCEL exist as bounded primitives or fixtures but are not all enabled as
  general production mutation policies.
- Long-term evidence that organizational adaptation consistently outperforms a strong single agent remains an
  evaluation target.
- Windows abstractions exist, but native OCR and clean Windows qualification remain incomplete.
- The Shared Evolution network is not yet a generally available customer service.
- Release signing, publisher, legal/provenance, upgrade-channel, and clean-release gates remain before commercial
  distribution.

## Near-term qualification order

1. Stabilize the general-purpose Employee runtime and terminal operator experience.
2. Qualify DIRECT, SOLO JOB, and TEAM JOB on the same model, authority, and budget.
3. Measure team value, unnecessary-team rate, repair rate, wall time, calls, cost, and safety violations.
4. Extend semantic replanning only where a typed trigger and measurable gain justify it.
5. Complete Windows and packaging qualification.
6. Deploy the opt-in Shared Evolution service with provenance, privacy, version pinning, and rollback controls.

## What Noruct does not claim yet

- Fully autonomous, unbounded organizational self-modification.
- Automatic truth from OCR or model extraction.
- Automatic upload of local knowledge or private work.
- Production parity on every operating system and provider.
- A completed public commercial release.
