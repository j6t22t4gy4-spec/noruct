# Noruct 0.0.80 — Unsigned Developer Preview

## Integrity

- Artifact: `noruct-0.0.80-py3-none-any.whl`
- Bytes: `9,529,172`
- SHA-256: `912e67de9412b1ea19af27734d693f492708d62528cae2e07e1b9fb913ad258c`
- Release class: `UNSIGNED_DEVELOPER_PREVIEW`

This preview intentionally has no artifact signature or notarization. Verify the published wheel's byte length and
SHA-256 before installation. The public release asset and independent readback are the integrity boundary for this
preview; this is not a stable, GA, commercial-complete, or production-readiness claim.

## Scope and limitations

- Qualified local scope: provider-free runtime contracts on macOS arm64 with CPython 3.11.
- Windows is unsupported or experimental-disabled. Linux, WSL, other architectures, hosted operation, and
  production/enterprise use are not qualified by this preview.
- Frozen routing, receipt-bound continuation, bounded single-Job multi-route/provider contracts, and external
  Tool/Skill/Plugin exact version/digest pinning are included as provider-free contracts.
- Multi-provider execution is `EXPERIMENTAL / PROVIDER_DEPENDENT / NOT_LIVE_QUALIFIED`. No model ranking,
  cost-saving, heterogeneous-provider, or cross-provider quality claim is made.
- No signed network Model Intelligence Snapshot is published or activated. Bundled conservative defaults and explicit
  local routes remain the default.
- Python source included in the wheel is intentionally visible to preview recipients.

## First-release response posture

There is no signed prior public artifact. The human release owner may pause new installs, remove this GitHub Release
asset, and disable future snapshot/route use. That posture is not evidence of an executed rollback rehearsal.
