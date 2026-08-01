# Noruct Public Core Boundary

This repository is the **MIT-licensed local-first public Core monorepo**. Source
publication is authorized. This does not claim that a signed package release,
hosted service, or web installer is available.

## Included in the public Core

- Company, Persistent Manager, Employee Runtime, Firm Kernel, and local state
- CLI/TUI, Knowledge, Graph execution, local learning and evolution proposals
- user-configured model providers, MCP endpoints, tools, skills, plugins, and
  ordinary web transports
- exact-version/digest intake, permission checks, static compatibility checks,
  active-Job pinning, explicit activation, and rollback contracts
- client-side validation of signed Network artifacts without server authority
- public tests, build metadata, notices, and the reproducible export verifier

The public Core must remain useful without a Noruct-hosted service. User-owned
provider credentials, customer content, prompts, Company state, Knowledge state,
and private artifacts remain outside Noruct's hosted service by default.

## Excluded from the public Core

- the Noruct-operated Shared Evolution and Network server implementation
- registry publisher, private-team credential verification, server-side
  benchmark promotion, catalog signing, and remote coordination control planes
- subscription, billing, tenant administration, and hosted operations systems
- hosted database migrations, deployment configuration, private release
  evidence, service credentials, and raw provider or customer data

The concrete excluded service root in the development monorepo is
`services/evolution-network-worker/`. Absence is enforced by
`public-monorepo.toml`, `dev/export_public_monorepo.py`, and
`dev/verify_public_monorepo.py`.

## Deferred release work

- release signing and a stable supported release identity
- installation through the future Noruct website

The source projection uses `publication_state = "SOURCE_PUBLICATION_AUTHORIZED"`.
That state does not authorize package upload, signing, tagging, hosted-service
deployment, or customer data processing.
