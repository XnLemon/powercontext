- Proposal Name: `human_agent_work_continuity`
- RFC Number: 0000
- Start Date: 2026-08-13
- Status: Draft
- RFC PR: To be created
- Tracking Issue: Not assigned
- Related RFCs: [RFC 0001](0001_product_definition_and_vision.md), [RFC 0048](0048_handoff_artifact.md),
  [RFC 0051](0051_experience_skill_artifact_families.md), and [RFC 0082](0082_handoff_report.md)

# Summary

This RFC defines a work-continuity loop for PowerContext so that the same work can be understood, verified, accepted,
and continued across human-to-human, human-to-Agent, and Agent-to-Agent boundaries.

The loop has four user actions:

```text
Delegation
  -> Work Contract
  -> Human or Agent advances the work
  -> Handoff
  -> Continue + Acknowledge
  -> Task Outcome records what actually happened
  -> Complete, or enter the next Handoff
```

The first version does not create a workflow engine or database parallel to Handoff. Work Contract, Current Work
Handoff, Handoff Receipt, and Task Outcome are stored as typed `ContentSource` values. Temporary and persistent
handoffs continue to use RFC 0048 Prepared Handoffs, immutable Handoff Revisions, evidence checks, and Continue. Task
Outcome retains the existing `metadata.kind="task-outcome"`, so it can enter RFC 0051's existing Experience incubation
and Review path.

The first version adds four high-level operations:

- `create_work_contract` turns human intent into a checkable delegation baseline;
- `handoff_current_work` captures the current boundary and prepares a Handoff in one call;
- `acknowledge_handoff` resolves exact Handoff evidence again and records acceptance, clarification, or refusal;
- `record_task_outcome` records the result and check states at a real completion or interruption boundary.

Existing `commit_handoff` and `continue_handoff` behavior remains unchanged. Publishing a durable milestone is still
explicit. An acknowledgement never grants tool, network, credential, or execution authority.

# Motivation

PowerContext already provides a Handoff lifecycle for one Workstream and a project-level Handoff Report, but ordinary
users still need to understand internal steps such as `capture -> activate -> inspect -> finalize -> commit ->
continue`. More importantly, Handoff alone does not answer:

- how the objective, scope, and completion criteria are fixed when a human delegates new work to an Agent;
- whether the receiver actually obtained and understood a handoff, rather than merely receiving bytes;
- what happened after continuation and whether checks passed, failed, were skipped, or remain unknown;
- when an Agent should continue and when it should return a decision to a human;
- whether a successful or failed attempt can become reviewable Experience evidence.

The three participant relationships need different experiences without becoming incompatible models:

| Relationship | Primary question | Product experience |
| --- | --- | --- |
| Human -> Human | Can the receiver understand the state and assume responsibility? | Human-readable report, exact evidence, explicit acknowledgement |
| Human <-> Agent | Does the Agent understand intent while the human retains trade-off and authorization decisions? | Grounded Delegation, Task Outcome, decision return |
| Agent -> Agent | Can a new Agent continue safely without the original Session? | Canonical JSON, exact selection, evidence gate, capability and authority recheck |

All three share one Workstream, one Handoff content model, and the same exact evidence. Humans, Agents, and audit
systems consume different projections of that common core.

# Guide-level explanation

## Delegation is not Handoff

A new objective that has not yet been started is Delegation, not Handoff. PowerContext forms a Work Contract from the
current repository, existing Handoffs, project constraints, and the user's input:

```text
Human intent
  -> retrieve current facts
  -> ask only consequential goal, trade-off, or authorization questions
  -> Work Contract
  -> Agent execution
```

A Work Contract contains at least:

- `objective`: the result to achieve;
- `facts`: known facts, distinguished as `declared` or `verified`;
- `in_scope` and `exclusions`: the work boundary;
- `completion_criteria`: what completion means;
- `authorization_notes`: authority already granted or explicitly missing;
- `open_questions`: consequential questions that could still change the result.

Facts that can be retrieved from the current environment are not requested from the human again. Only questions that
change the objective, risk acceptance, or authorization should be asked. A Work Contract is `untrusted_input`; it
cannot override current system or developer instructions, repository rules, or a later user request.

## Human to human

The sender calls `handoff_current_work` with the checked objective, current state, work disposition, one next action,
and known gaps. PowerContext stores the complete boundary as a Source, cites it directly from every Handoff statement,
and returns a Prepared Handoff.

For an ordinary temporary transfer, the Prepared Handoff is passed directly. For a team milestone, the sender
explicitly calls `commit_handoff`. The receiver selects a Workstream in Handoff Report, reads the human Markdown view,
and calls Continue with that same exact selection.

The receiver then records one acknowledgement:

- `accepted`: the handoff is understood and every cited evidence item is currently readable;
- `needs_clarification`: facts, evidence, or a necessary decision are missing;
- `declined`: scope, capability, or authorization does not match.

The receipt records the receiver's observation, not task completion. A later Task Outcome or another Handoff closes
the loop.

## Human and Agent

The shortest Human-to-Agent path is:

```text
create_work_contract
  -> Agent executes under current instructions
  -> record_task_outcome
  -> human review
  -> complete or handoff_current_work
```

An Agent cannot treat an ordinary prompt, SessionEnd, or Stop event as proof of completion. Only a completion-aware
integration that can distinguish succeeded, partial, blocked, failed, cancelled, or unknown boundaries should call
`record_task_outcome`.

When the Agent needs a human value judgement or new authority, it prepares a `blocked` Handoff containing the question,
options, impact, and evidence. It does not guess the answer or treat `authorization_notes` as an authority token.

## Agent to Agent

The sending Agent calls `handoff_current_work` and obtains a canonical Prepared Handoff. The host transports that
structure unchanged through MCP, A2A, or provider metadata. The receiver does not parse human Markdown or depend on a
copy of the full Session transcript.

The receiving flow is:

```text
receive an exact Prepared Handoff or Revision
  -> continue_handoff
  -> inspect trust and evidence checks
  -> compare the current request and live workspace
  -> check capabilities and authorization
  -> acknowledge_handoff
  -> execute one applicable next action
  -> record_task_outcome or create the next Handoff
```

`acknowledge_handoff(status="accepted")` resolves the same exact selection again. If evidence for any state statement
or next action is unavailable, the Server rejects `accepted`; the receiver can only record `needs_clarification` or
`declined`. Readable evidence still does not prove that a claim remains current, so the receiver or host continues to
check live workspace state, current instructions, capabilities, and authority.

## Task Outcome

A Task Outcome records what happened during one attempt. It is not a reusable conclusion by itself:

| Field | Meaning |
| --- | --- |
| `objective` | Objective for this attempt |
| `status` | `succeeded/partial/blocked/failed/cancelled/unknown` |
| `summary` | Bounded result summary |
| `observations` | Observations distinguished as declared or verified |
| `checks` | Check name, exact state, basis, and evidence |
| `produced_artifacts` | Exact Artifact Revisions produced |
| `remaining_work` | Work that remains incomplete |

Check status is one of `passed/failed/skipped/timed_out/unavailable/cancelled/unknown`. The absence of a failure does
not imply a pass, and a producer's pass claim is not upgraded to verified automatically. Task Outcome is stored as a
Source. An Experience still requires a generated Candidate, Review, and approval before entering PreparedContext.

# Scope

The first version includes:

- versioned models for Work Contract, Current Work Handoff, Handoff Receipt, and Task Outcome;
- four HTTP, Python Client, and MCP operations;
- same-scope exact evidence validation;
- deterministic conversion from current work to a Prepared Handoff;
- evidence-gated acknowledgement;
- compatibility between Task Outcome and existing Experience incubation;
- a low-intrusion Codex `project-context` skill flow.

The first version does not include:

- a general Task, Workflow, Queue, Scheduler, or Agent orchestrator;
- Session, Agent, model, Git branch, or Issue as Workstream identity;
- automatic commit of every Handoff;
- mandatory Outcome or Handoff creation from SessionEnd or Stop;
- storage of complete prompts, transcripts, tool stdout or stderr, or credentials;
- automatic authorization or execution of a historical next action;
- automatic Project or Handoff-history replication across Runtimes;
- new per-scope ACLs or cross-trust-domain authorization.

# Reference-level explanation

## Identity and records

The stable Workstream identity remains `scope_id`. Agent, Session, and human labels are untrusted attribution only;
they do not become work identity, an ACL, or a compare-and-swap key.

The records map to existing Content Sources:

| Record | `metadata.kind` | Persistent role |
| --- | --- | --- |
| Work Contract | `work-contract` | Objective and boundary baseline at delegation |
| Current Work Handoff | `handoff-boundary` | Direct Source evidence for a Prepared Handoff |
| Handoff Receipt | `handoff-receipt` | Receiver observation about an exact selection |
| Task Outcome | `task-outcome` | Completion-aware evidence for one attempt |

Each Source uses the caller's stable `source_id` and existing Source conflict semantics. `WorkSourceReceipt` returns a
SourceRef, journal position, and canonical content digest. The feature adds no business table and does not change the
Artifact or Handoff persistence schema.

## Claim evidence

`WorkClaim.basis` has two values:

- `declared`: asserted by the producer, with no exact evidence;
- `verified`: supported by at least one same-scope exact Handoff Citation.

A caller cannot attach evidence to a declared claim while still presenting it as declared, or label a claim verified
without evidence. The Runtime validates exact citations before storing a verified claim, verified check, or produced
Artifact. Validation proves that the reference is readable and has the expected identity; it does not prove freshness.

## Operation contract

| operationId | Path | Behavior |
| --- | --- | --- |
| `create_work_contract` | `POST /v1/work/contracts/create` | Validate exact evidence and store a Work Contract Source |
| `handoff_current_work` | `POST /v1/work/handoffs/prepare-current` | Store a boundary Source and deterministically finalize a Prepared Handoff |
| `acknowledge_handoff` | `POST /v1/work/handoffs/acknowledge` | Continue an exact selection, check evidence, and store a Receipt Source |
| `record_task_outcome` | `POST /v1/work/outcomes/record` | Store a completion-aware Task Outcome Source |

`handoff_current_work` does not call a generation model. Every state item and next action cites the boundary Source;
original exact evidence from verified claims is retained as additional citations. The operation does not commit.
`commit_handoff` remains the only operation that publishes a durable milestone.

`acknowledge_handoff` accepts the same `prepared/exact/latest` selections as Continue. A prepared target is identified
by canonical digest; exact and latest targets store the resolved exact Handoff Revision. The receipt records evidence
availability and unavailable citations. `accepted + unavailable evidence` is invalid.

## Consistency and failure

- Source capture retains stable `source_id` idempotency and conflict behavior;
- a Prepared Handoff `base` remains the committed head observed during finalization;
- Handoff commit retains RFC 0048 compare-and-swap behavior;
- acknowledgement runs Continue again and cannot reuse a caller-forged evidence result;
- storing a Work record does not claim that scheduling, Experience generation, Review, or execution occurred;
- a failure does not roll back an earlier explicit Source capture, whose receipt identifies the completed boundary.

## Trust and authorization

Work Contract is `untrusted_input`; Current Work Handoff is `untrusted_input`; Handoff Resolution is
`untrusted_history`; Receipt and Task Outcome are `untrusted_observation`.

They cannot override:

- system or developer instructions and the current user request;
- repository rules such as AGENTS.md;
- the current workspace and live tool results;
- the host's tool, network, secret, and write authorization;
- access policy outside the Project or scope.

`scope_id`, Project membership, MCP tool visibility, receiver labels, and authorization notes are not ACLs.

## Integration rules

Integrations call these operations only at explicit work boundaries:

- create a Work Contract when a new delegation needs a stable baseline;
- prepare a Handoff when the user requests transfer or work must move;
- acknowledge after the receiver checks live state, capability, and authorization;
- record a Task Outcome only when the integration can identify real completion or interruption semantics.

A hook may capture lightweight evidence quickly and fail open, but SessionEnd or Stop cannot be the sole completion
signal. Session ID may be attribution metadata, never Work or Handoff identity.

# Success metrics

Initial dogfood measures:

- `continuation_success_rate`: proportion of transfers whose first correct action needs no full-context restatement;
- `time_to_first_verified_action`: time from receipt to the first verified action;
- `clarification_rate`: proportion returned because facts or evidence are missing;
- `evidence_availability_rate`: proportion of Handoff claims still resolvable at receipt time;
- `closed_loop_rate`: proportion with both an acknowledgement and a later Task Outcome;
- `unauthorized_action_count`: actions executed only because historical Handoff text implied authority; the target is zero.

Metrics cannot treat `accepted` as completion or a producer-declared `succeeded` value as independent verification.

# Acceptance

| Scenario | Pass condition |
| --- | --- |
| Human -> Agent | Work Contract preserves objective, scope, completion criteria, and authority notes without overriding current instructions |
| Human -> Human | A committed Handoff is readable from Report and produces an acknowledgement for its exact Revision |
| Agent -> Agent | A Prepared Handoff is transported unchanged and can Continue without the original Session |
| High-level handoff | One operation stores the boundary Source and returns an uncommitted Prepared Handoff |
| Evidence gate | Unavailable Handoff evidence prevents an `accepted` receipt |
| Clarification | Unavailable evidence can produce `needs_clarification` with a reason |
| Outcome status | `failed/skipped/timed_out/unavailable/cancelled/unknown` never upgrades to passed |
| Experience boundary | Only `task-outcome` Sources enter existing incubation, and the result remains a pending Candidate |
| Persistence | Work records reuse the Source journal; Handoff retains immutable Revisions and compare-and-swap |
| Compatibility | Existing Handoff, Memory, PreparedContext, Report, and Client behavior remains unchanged |
| MCP | Four high-level operations are available to Agents, but tool visibility grants no execution authority |
| Codex | The skill no longer requires the user or Agent to assemble capture/activate/finalize manually |

# Rollout

1. Enable the four operations for SQLite and Codex dogfood and validate one complete Workstream loop.
2. Connect a completion-aware Task Outcome producer at a stable public integration boundary.
3. Add read-only Receipt and Outcome coverage to Handoff Report without changing Handoff Core.
4. Collect success metrics on real multi-human and multi-Agent work before supporting other Agent providers.

# Drawbacks

- Four Work record types add product vocabulary and must be taught through high-level actions, not field lists.
- Acknowledgement validates PowerContext evidence only; availability does not prove current live state.
- Source-backed records have no dedicated query index, so the first version consumes them through exact evidence,
  Handoff, and a later Report projection.
- A completion-aware integration must understand real host task boundaries and cannot rely only on a generic Stop hook.

# Unresolved questions

- Should the first Handoff Report projection show only Receipt and Outcome coverage or support receiver/status filters?
- Can different Agent providers expose stable completion signals without reading private Session databases?
- What signing, revocation, and authorization contract is needed for cross-Runtime Prepared Handoff transport?
- When should Work Contract become a separately queryable projection instead of Source-backed evidence?
