# Noruct

> **Language:** [English — canonical](docs/en/README.md) · [한국어 번역](docs/ko/README.md)

Noruct is a runtime for a persistent AI firm: a user gives a goal, while the system keeps durable organizational capability and composes only the execution structure the goal needs.

The public documentation is a concept-paper series. It explains the product thesis, the operating model, the authority boundaries, and the evaluation questions without exposing private implementation or customer state.

> **Workflows are temporary. Organizational capability compounds.**

For managed work, Noruct plans for the **minimum sufficient organization**, not the cheapest run and not the largest
parallel structure. One Employee remains the baseline when additional value is unknown. Independent partitions,
materially different candidate paths, diagnostic probes, or independent verification may justify bounded additional
execution under the same hard budget, but the proposal must state its expected value, integration path, and stopping
condition. Reuse still requires matched evidence that includes coordination and human review burden.

Start with the [English canonical concept map](docs/en/README.md). The Korean documentation is a maintained translation for Korean readers.

For an explicit distinction between the concept and the current development implementation, read the
[current development status](docs/en/08-current-development-status.md). Noruct is not yet a completed commercial
release, and the status paper deliberately records both implemented paths and unqualified hypotheses.

The [organization-as-decision-architecture paper](docs/en/09-organization-as-decision-architecture.md) explains why
titles, agent counts, and communication graphs are insufficient descriptions of an AI firm. It separates task,
assignment, information, communication, authority, verification, and learning. It also explains why Noruct should use
the minimum sufficient organization and deliver a short, evidence-bound explanation instead of treating more agents or
more logs as progress.

The [model-intelligence and multi-provider paper](docs/en/10-model-intelligence-and-multi-provider-orchestration.md)
explains how shared, signed benchmark evidence can inform local routing without becoming Company authority. It separates
central priors, local compatibility and outcomes, freezes an exact route per EmployeeRun, limits provider-specific data
egress, and preserves one artifact owner and commit path even when one Job uses multiple providers.

The Korean-language [source working paper](docs/literature/README.md) behind that paper is published separately in its
original long-form structure. Its citations and recent research claims remain source-paper claims rather than
independently verified Noruct product evidence.

Capability intake and recursive improvement use separate trust lanes. External Tools, Skills, Plugins, and other
Network artifacts remain pinned to an exact reviewed version and digest and are never replaced automatically. Only a
locally derived artifact with no Network provenance may advance for a future Job when the user has selected
`always-approve`, and only after authority and static runtime/capability compatibility checks. Running Jobs retain their
existing pins, prior activations remain rollback targets, and Noruct does not modify the imported package source.

## Scope

This repository publishes product concepts, operating boundaries, and architectural principles. It does not publish customer data, internal operational configuration, security-sensitive implementation detail, or private development records.
