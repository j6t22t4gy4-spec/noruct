# 10 — Model Intelligence and Multi-Provider Orchestration

> **Canonical:** English · [한국어](../ko/10-model-intelligence-and-multi-provider-orchestration.md) · [← Organization as Decision Architecture](09-organization-as-decision-architecture.md) · [Documentation index](README.md)

## Central claim

A model catalog should inform organizational judgment without becoming organizational authority.

Noruct should not make every installation re-benchmark every model. A controlled evaluation environment can publish a
signed, data-only intelligence snapshot containing task-specific observations, uncertainty, latency, cost availability,
and error patterns. A local firm combines that shared prior with its own compatibility status, authority and data
boundaries, observed outcomes, and user policy. The Firm Kernel then freezes one exact execution route for each
EmployeeRun or task attempt.

~~~text
shared benchmark prior
+ local compatibility
+ local observed outcomes
+ authority, privacy, and task constraints
→ frozen execution route for the next Job
~~~

The benchmark service does not receive live Job content by default, dispatch Employees, install capabilities, or change
a running Job.

## Separate four kinds of evidence

“This model is good” collapses different questions. Noruct separates them.

| Evidence | Question | Boundary |
|---|---|---|
| Provider declaration | What does the provider say the route supports? | Useful metadata, not proof of local compatibility or quality |
| Shared benchmark prior | How did the route perform under a recorded harness and task distribution? | Reusable evidence with uncertainty, not a universal ranking |
| Local compatibility | Does this route satisfy the required wire and runtime contract here? | A bounded smoke, not another performance benchmark |
| Local outcome | What happened on this firm's matched tasks? | Context-specific correction, not permission for unrestricted self-optimization |

A shared benchmark should preserve task classes, harness and dataset revisions, sample size, dispersion, complete
failures, evaluator conditions, known limitations, and possible sponsorship or contamination. A single leaderboard
score is insufficient for organizational routing.

## Model identity should state what is actually known

A remote model identifier may not be a digest of immutable weights. Noruct should record the strongest identity claim
that can actually be supported:

- a local content digest;
- an immutable provider revision;
- a versioned model identifier whose weights are not independently verified;
- a floating alias that may change behind the same name; or
- unknown identity assurance.

This is not a requirement to inspect every provider's weights. It prevents a requested alias from being described as a
content-addressed artifact when it is not. Material drift may invalidate a route for future selection, but it does not
silently reroute an active Job or rewrite prior evidence.

## Intelligence snapshots are data, not executable capability

A model-intelligence snapshot may contain bounded metrics, uncertainty, provenance, an orchestration-weight profile,
expiration, a payload digest, and a signature. It must not contain executable code, prompts, Tools, Skills, Plugins,
credentials, or an instruction that can mutate Company state.

~~~text
downloaded
→ signature, schema, and expiry verification
→ local candidate
→ activation for future Jobs
→ retirement or rollback
~~~

Invalid, unknown, expired, or unavailable intelligence fails to a retained last-known-good snapshot or conservative
local defaults. It must not block offline startup, trigger an unbounded background retry, or install another package.

## Route by task and state, not by title alone

A persistent Employee owns a capability profile, private bounded state, and an allowed execution class. It does not own
a provider account. A Job resolves the required execution class to an exact route after considering the Work Order,
Employee capability, information boundary, organization state, user policy, and current evidence.

Different states may justify different routes:

| State | Typical requirement |
|---|---|
| Frame | Strong requirement and risk interpretation |
| Explore | Bounded independent candidates where diversity has expected value |
| Select | Evidence comparison and reliable structured output |
| Integrate | One context-capable owner |
| Verify | A materially different error path, source, tool, or model |
| Commit | Deterministic Kernel and Executor, not a model |
| Learn | A future-change candidate, never direct self-modification |

A reusable Blueprint therefore binds required capability and execution class rather than a provider brand. Each new Job
resolves and freezes its own route without modifying the Blueprint or an already running Job.

## One Job may use multiple providers without creating multiple authorities

Multi-provider operation has several distinct meanings:

1. Different tasks or Employees use different exact routes.
2. Advisory routes produce bounded, no-tool candidate evidence.
3. A read-only verifier follows a materially different error path.
4. A pre-approved fallback route handles a retryable availability failure before output or effect.

Fallback is not independent verification. A reference model is not an acting Employee. Advisory output is untrusted,
source-labelled evidence rather than a system instruction. Each provider receives only the context projection allowed
by its data-egress grant; the full prompt is not broadcast merely because multiple routes are available.

The final artifact still has one owner, one acting integrator, and one commit path. A route cannot be substituted after
partial user-visible output, a tool or external effect has started, or a commit has occurred.

## Routing policy is eligibility first, optimization second

The resolver first rejects routes that do not satisfy authority, data-egress, required capability, availability, or
continuation constraints. It then compares eligible routes using task-specific quality, complete-task reliability,
specialization, verification independence, latency, local outcomes, and cost.

Cost is one optimization input, not the purpose of the firm. Hard call, time, or cost ceilings remain safety controls
against runaway execution. User-facing policies can express intent such as quality-first, balanced, efficient, or
private-local-first without pretending that one global scalar represents every trade-off.

When score differences are smaller than their uncertainty, the simpler route and strong Solo remain the conservative
choice.

## Every physical call needs an immutable receipt

Multi-provider execution is auditable only if each physical call records its route and context projection, attempt or
fan-out lineage, terminal or indeterminate state, safe provider metadata, usage, cost availability, latency, error code,
and output digest. Mutable fields on a shared provider object are not durable execution evidence.

Cancellation must propagate to every child call and local process. A provider-native thread is not Company state.
Continuation uses the frozen route or an explicit rebound with a fresh session and receipt; it does not silently move
old context to a different endpoint.

## Human control remains concentrated at meaningful boundaries

Ordinary reuse of an already approved route should not create repeated approval prompts. Human review belongs at the
boundaries that can materially change authority or exposure:

- adding a provider or credential;
- widening a data-egress class;
- approving benchmark corpus, license, and public claims;
- authorizing paid live qualification;
- signing and publishing a snapshot or product release; and
- deciding whether to keep, pause, roll back, or replace a release.

The default architecture is download-only. Uploading local Job outcomes requires a separate opt-in data-minimization
contract; prompts, artifacts, workspace identities, credentials, and customer content are not default telemetry.

## Current boundary

The present development runtime has multiple provider adapters, explicit bounded fallback, and bounded advisory
fan-out. Those mechanisms are currently job-wide provider composition, not yet complete Employee- or task-specific
routing. Signed intelligence snapshots, local compatibility caching, exact per-run route resolution, provider-specific
egress, and production multi-provider qualification remain development work. Their description here is an intended
architecture, not a performance or release claim.
