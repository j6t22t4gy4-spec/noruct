# Contributing to Noruct

Noruct's first-party public Core is licensed under the MIT License. Vendored
and third-party material keeps the license and notice identified in its own
source directory and in `THIRD_PARTY_NOTICES.md`.

By submitting a contribution, you certify the Developer Certificate of Origin
1.1 for that contribution. Add a `Signed-off-by: Name <email>` trailer to each
commit. The trailer states that you have the right to submit the work under
the project's applicable license; it is not a transfer of copyright.

Contributions must not contain customer data, credentials, private Company or
Knowledge state, private hosted-service implementation, internal release
evidence, unregistered third-party source, or external brand assets. Keep
external Tools, Skills, Plugins, and MCP packages exact-versioned and do not
modify their installed originals.

Before opening a change, run the relevant provider-free tests and
`python dev/verify_component_budgets.py --project-root .`. Public-boundary
changes must also run `python dev/verify_public_monorepo.py --project-root .`.

See `PUBLIC_CORE_BOUNDARY.md` for the public/private product boundary.
