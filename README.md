# Noruct

> **Language:** [English — canonical](docs/en/README.md) · [한국어 번역](docs/ko/README.md)

Noruct is a runtime for a persistent AI firm: a user gives a goal, while the system keeps durable organizational capability and composes only the execution structure the goal needs.

The public documentation is a concept-paper series. It explains the product thesis, the operating model, the authority boundaries, and the evaluation questions without exposing private implementation or customer state.

> **Workflows are temporary. Organizational capability compounds.**

For managed work, Noruct plans performance-first rather than cost-minimum. One Employee remains the baseline, but technical feasibility alone does not suppress bounded replication: independent partitions, candidate paths, or diagnostic probes can justify two to four job-local instances of that same Employee under the existing hard budget. Those instances are not new employees, and reusable value must still be demonstrated against a single-run baseline under the same total budget.

Start with the [English canonical concept map](docs/en/README.md). The Korean documentation is a maintained translation for Korean readers.

For an explicit distinction between the concept and the current development implementation, read the
[current development status](docs/en/08-current-development-status.md). Noruct is not yet a completed commercial
release, and the status paper deliberately records both implemented paths and unqualified hypotheses.

Capability intake and recursive improvement use separate trust lanes. External Tools, Skills, Plugins, and other
Network artifacts remain pinned to an exact reviewed version and digest and are never replaced automatically. Only a
locally derived artifact with no Network provenance may advance for a future Job when the user has selected
`always-approve`, and only after authority and static runtime/capability compatibility checks. Running Jobs retain their
existing pins, prior activations remain rollback targets, and Noruct does not modify the imported package source.

## Scope

This repository publishes product concepts, operating boundaries, and architectural principles. It does not publish customer data, internal operational configuration, security-sensitive implementation detail, or private development records.
