# Operator Mode for Hermes GPT

Operator Mode is the policy-gated control plane for trusted MCP clients such as ChatGPT. This document describes the current v0.13.0 behavior, including the durable Mission lifecycle, unified delegation lineage, live-event bus, and Fabric-backed cross-machine Swarm execution, plus the vNext slice-1 additive surfaces (MissionPlan DAG, derived capability-manifest / mission-ledger views, budget envelope, placement scoring, failure classification + recovery matrix, and the shadow/observe mission controller) and the vNext slice-2 gated execution rungs (budget D3 hard-block enforcement behind `HERMES_GPT_BUDGET_HARD_BLOCK=1`, and the controller L2 rung behind `HERMES_GPT_CONTROLLER_EXECUTE=1`). The slice-1 surfaces are decision-only; the slice-2 rungs are default-off, add no tools, and keep every existing surface byte-identical until their gates are armed. They are documented further in [vnext-capability-manifest-and-mission-ledger.md](vnext-capability-manifest-and-mission-ledger.md) and [design/](design/).

For documentation authority and historical-artifact rules, see [docs/README.md](README.md).

## Core rule

**Tool visibility is not authority.** A tool can be present in the MCP schema and still refuse to mutate because the required policy gates are not satisfied.

Hermes GPT is designed to run on the user's machine, bound to loopback. Remote clients require a deliberately configured private or authenticated boundary in front of that loopback service. Public unauthenticated Operator hosting is unsupported.

Operator Mode is defense-in-depth, not an OS sandbox. Use OS-level isolation for untrusted input.

## Authority model

Operator authority is determined by four things:

1. `HERMES_GPT_OPERATOR_ENABLED`
2. `HERMES_GPT_OPERATOR_LEVEL`
3. `HERMES_GPT_OPERATOR_APPLY_MODE`
4. the individual tool call's mutation gates, normally `dry_run` and sometimes `confirm`

Owner operations add two more gates:

```text
HERMES_GPT_OWNER_ACTIVE=1
HERMES_GPT_OWNER_ACK=I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE
```

### Operator levels

Higher levels include the capabilities of lower levels.

| Level | What it unlocks |
| --- | --- |
| `read_only` | status, policy, audit, list/view/diff operations, Mission Control |
| `cron` | cron run, pause, copy, move |
| `skills` | skill create, edit, patch, write, copy, sync, delete |
| `skills_config` | non-secret config and environment writes |
| `workspace` | scoped workspace read/write/test, bounded binary export, gateway restart, Codex jobs, contract/swarm dispatch |
| `owner` | break-glass raw command/file operations and final swarm approval; secret paths remain denied |

`skills_config` is a reasonable ceiling for routine configuration work. Use `workspace` only when a task requires scoped workspace operations or delegated execution. Treat `owner` as break-glass.

## Four operating postures

### 1. Read-only default

No Operator environment variables are required.

```powershell
hermes-gpt
```

Expected behavior:

- read/status/list tools work where enabled;
- mutating Operator tools refuse;
- Mission Control remains read-only.

### 2. Dry-run Operator Mode

Recommended for an always-on trusted connector.

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="skills_config"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="dry_run"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default"

python server.py --http --host 127.0.0.1 --port 4750
```

Mutating tools return plans or previews but do not apply changes.

### 3. Direct Operator Mode

Use only for a deliberate maintenance session.

```powershell
$env:HERMES_HOME="C:\Users\<YOU>\AppData\Local\hermes"
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="workspace"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OPERATOR_ALLOWED_PROFILES="default"
$env:HERMES_GPT_OPERATOR_ALLOWED_PATHS="C:\path\to\approved-workspace"
```

Direct mode only permits mutation. It does not force mutation. A mutating call still needs `dry_run=false`, and tools with an explicit confirmation gate also need `confirm=true`.

### 4. Owner Mode

Break-glass only.

```powershell
$env:HERMES_GPT_OPERATOR_ENABLED="1"
$env:HERMES_GPT_OPERATOR_LEVEL="owner"
$env:HERMES_GPT_OPERATOR_APPLY_MODE="direct"
$env:HERMES_GPT_OWNER_ACTIVE="1"
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

Configured `owner` authority is clamped unless the Owner activation and exact acknowledgement are present. Owner Mode still cannot access denied secret paths.

Do not use Owner Mode for a public, shared, or always-on connector.

## Dry-run and confirmation semantics

For a normal mutating Operator tool, actual mutation requires:

```text
HERMES_GPT_OPERATOR_APPLY_MODE=direct
```

plus:

```json
{
  "dry_run": false
}
```

Tools that create or dispatch external work also require `confirm=true` when their schema includes that gate.

A refusal caused by a missing gate is expected behavior, not a failure to execute the request.

## Mission Control

Mission Control is the read-only `hermes_mission_*` operational view of the Hermes deployment.

Surfaces:

- `overview`
- `health`
- `profiles`
- `fleet`
- `codex`
- `cron`
- `delegations`
- `failures`
- `approvals`
- `vault`
- `usage`
- `audit`

Mission Control is structurally read-only:

- SQLite sources are opened read-only;
- no Mission tool accepts write/apply arguments;
- no mutating shell action is performed;
- every Mission call is audited as unchanged/read-only;
- missing optional sources return a bounded unavailable reason instead of crashing the whole view.

### Data boundary

Mission Control never returns raw message, memory, transcript, request-dump, credential, token, vault-secret, or profile-secret bodies.

Prompt-like text is represented by bounded metadata such as `prompt_len` and `prompt_sha256`. Free-text operational fields such as failures, audit summaries, cron names, and delegation summaries receive conservative secret and PII stripping before they leave the host.

### Surface allowlist

`HERMES_GPT_MISSION_ALLOWED_SURFACES` is **restrictive when configured**.

- **unset:** all read-only Mission Control surfaces are available;
- **comma-separated list:** only listed valid surfaces are available;
- **empty value:** every Mission Control surface is denied.

Do not describe the unset state as deny-by-default. The implementation deliberately makes all read-only Mission surfaces available when the variable is absent.

Mission Control requires only `read_only` authority and never needs direct apply mode.

## Missions lifecycle (v0.9)

Beyond the read-only Mission Control overview, v0.9 adds a first-class durable Mission object as the bounded parent record for a larger objective. A Mission groups an objective, acceptance criteria, bounded context references, an explicit skills manifest, Swarm/work/delegation attachments, lifecycle state, and a final Owner approval that defaults on.

Lifecycle: `draft -> running -> awaiting_approval -> completed`, plus `paused` / `blocked`. `final_approval_required` defaults to true and is immutable after creation; disabling it is an Owner-only creation-time decision.

Tools: `hermes_mission_create`, `hermes_mission_get`, `hermes_mission_list`, `hermes_mission_update`, `hermes_mission_attach`, `hermes_mission_transition`, `hermes_mission_reconcile`, `hermes_mission_approve`.

Reads are read-only. Mutations preserve the normal workspace/direct/confirm gates; direct completion is Owner-gated, and approval-required Missions complete only through explicit Owner approval. A public attachment call can never assert `succeeded`. Completion stays based on coordinator-observed evidence and explicit approval, preserving the v0.8 Fabric safety model.

See [Missions (v0.9)](missions.md).

## Delegations (v0.9)

v0.9 adds a durable, normalized delegation lifecycle above Work Contracts and existing runner/Fabric execution. The delegation record is lineage and state metadata, not a second execution authority; runner, Fabric, Work Contract validation, and Operator policy remain authoritative.

Tools: `hermes_delegation_dispatch`, `hermes_delegation_get`, `hermes_delegation_list`, `hermes_delegation_reconcile`, `hermes_delegation_cancel`. Normalized states are `queued`, `running`, `reconciling`, `blocked`, `succeeded`, `failed`, and `cancelled`.

A delegation never marks work successful from a worker's claim: terminal backend success stays `reconciling` until the matching immutable Work Contract lineage has a `SATISFIED` verdict; missing, unreadable, or `UNVERIFIED` evidence fails closed. `opencode` is a first-class local runner backend.

See [Delegations (v0.9)](delegations.md).

## Live events (v0.9)

v0.9 adds a durable, bounded live-event bus for clients and parent orchestrators that need completion/wake-up delivery without polling every underlying store.

MCP tools: `hermes_live_events_cursor()` and `hermes_live_events_since(cursor, mission_id, topic, kind, limit, wait_ms)`. The same durable stream is available over `/events/ws` (Operator mode must be enabled; OAuth-only deployments fail closed for WebSocket). Reads are non-creating.

Live events are notifications, not proof. Mission, Swarm, Work Contract, runner, and Fabric journals remain authoritative; a missing, delayed, duplicated, or reconnected event never advances work. See [Live events (v0.9)](live-events.md).

## OpenAI MCP Events extension (draft, experimental)

The SDK 2.x Streamable HTTP extension advertises `server/discover`,
`events/list`, `events/subscribe`, and `events/unsubscribe`. Its sole event,
`hermes.live_event`, projects wake-up notifications from the durable
`hermes_live_events_*` journal above. Notifications are not proof of completion;
consumers must re-read authoritative state and validate the Work Contract.

Live Events is the only durable business-event journal. The adapter's
`mcp-events/subscriptions.sqlite3` stores subscription state, including callback
credentials, cursors, leases, and callback-verification state; it is not a
second event or delivery journal. Projection checkpoints its cursor after
callback acceptance, uses bounded at-least-once delivery with stable message
IDs, and never republishes callback responses into Live Events. Event RPCs use
the existing authenticated `/mcp` boundary and grant no Operator or Owner
authority. See [MCP compatibility](mcp-compatibility.md#openai-mcp-events-extension)
for the canonical protocol, cursor, retention, callback-security, and SDK-version
details.

## Binary file export

`hermes_export_file(path)` is a read-only raw-byte transfer surface gated at Operator `workspace` level. It requires a non-empty `HERMES_GPT_OPERATOR_ALLOWED_PATHS`, resolves paths before authorization so symlink escapes are refused, preserves all denied secret/credential paths even in Owner Mode, enforces a 4 MiB default and 16 MiB hard maximum, and supports an optional `HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS` suffix allowlist. Successful bytes are returned as `EmbeddedResource(BlobResourceContents)` with safe metadata; client download/attachment rendering is client-controlled. See [Binary file export](file-export.md).

## Work Contracts

Work Contracts add a structured, verifiable work-order layer through `hermes_contract_*`.

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_contract_define(contract_json)` | read-only | Validate and canonicalize a contract. |
| `hermes_contract_dispatch(contract_json, confirm, dry_run)` | workspace | Dispatch a validated contract through the existing fleet authority model. |
| `hermes_contract_validate(contract_json)` | read-only by default | Validate completion from observed evidence. |
| `hermes_contract_status(contract_json)` | read-only | Link the contract to bounded observed run/delegation state. |

For contracts that carry `capability_req`, dispatch revalidates the requested
skills against the assigned profile's effective Hermes Agent loader immediately
before invoking the runner. This is a live guard against profile changes after
planning; rejection is non-mutating. Fabric eligibility remains separate from
logical profile skill ownership.

### Validation model

A worker's claim that work is complete is never proof by itself. Validation inspects observed state such as runs, artifacts, tests, audit evidence, and required review evidence.

Verdicts are bounded to:

- `SATISFIED`
- `NOT_SATISFIED`
- `INCONCLUSIVE`
- `INVALID_CONTRACT`

Missing evidence fails closed. A valid contract with no observed run cannot become `SATISFIED`.

Retry selection is deterministic. Forbidden-action audit evidence is scoped to the contract's task identity so unrelated concurrent work does not contaminate the verdict.

### Review limitation in v0.6.0

v0.6.0 has no production review-accept writer. If a contract requires review, the necessary evidence must already exist through an authorized external reviewer/audit path or human approval reference. If it does not exist, validation returns `NOT_SATISFIED`.

**v0.7 closes this gap**: `hermes_review_accept` (owner-gated, distinct
reviewer enforced) writes review-acceptance records that the validator reads
as evidence. See [Review evidence](#review-evidence-hermes_review_accept).

Required test checks execute only through the workspace test allowlist and inherit the workspace/direct policy gates.

## Swarm Orchestration

Swarm Orchestration is the `hermes_swarm_*` DAG workflow layer built on Work Contracts.

Canonical workflow shape:

```text
research -> architecture -> implementation/tests/docs
         -> integration review -> Codex review
         -> acceptance validation -> HUMAN APPROVAL
```

Tools:

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_swarm_workflow_validate(workflow_json)` | read-only | Validate schema, owners, caps, contracts, dependencies, and cycles. |
| `hermes_swarm_workflow_create(workflow_json, confirm, dry_run)` | workspace | Register a workflow instance. |
| `hermes_swarm_workflow_list()` | read-only | List bounded workflow status. |
| `hermes_swarm_workflow_status(workflow_id)` | read-only | Return one bounded stage/owner/verdict/handoff view. |
| `hermes_swarm_stage_dispatch(workflow_id, stage_id, confirm, dry_run)` | workspace | Dispatch one ready stage as a Work Contract. |
| `hermes_swarm_stage_advance(workflow_id, stage_id, confirm, dry_run)` | workspace | Validate observed completion, record handoff, and promote ready children. |
| `hermes_swarm_approve(workflow_id, confirm, dry_run)` | owner | Record the final human approval. |

Default caps, unless explicitly overridden by the supported environment variables:

- 3 concurrent stages per workflow;
- 4 concurrent stages per board;
- 12 stages per workflow.

A Swarm stage may carry an optional `capability_req` with the logical profile and
required skills. The generated Work Contract preserves that requirement, and
dispatch revalidates it against the live Hermes Agent loader before invoking a
runner. The probe uses `skill_view(..., preprocess=False)`, matching Hermes
preload, so validating a required skill does not execute `skills.inline_shell`
snippets. Removing a required skill after workflow creation therefore rejects the
dispatch without starting work; Fabric remains a separate physical placement
question.

Failed validation can return a stage for one bounded rework retry. A second failure blocks the stage for human attention.

Codex may provide a bounded review verdict, but Codex is never an implementation owner. Final workflow approval is human and Owner-gated.

## Fabric execution (v0.8)

v0.8 extends Swarm execution across authenticated Hermes machines without moving completion authority to the worker. A stage using `execution.backend=auto` can be placed on an eligible local or remote runtime from the current capability snapshot; explicit backend/placement choices remain available where the workflow supports them.

The packaged remote endpoint is `hermes-gpt-fabric-peer`. Loopback HTTP is permitted for same-machine development. Non-loopback peer serving requires both `--cert` and `--key`; the peer refuses insecure remote transport.

Fabric routing is fail-closed. Placement considers node health and capability freshness, backend support, profile/workspace policy, and the server-controlled authority ceiling. A remote peer cannot widen the coordinator's authority. Remote worker self-report is transport data, not Work Contract completion evidence.

Remote evidence and artifacts are admitted through coordinator-controlled paths. Required unavailable evidence cannot become `SATISFIED`; artifact bytes are verified before admission. Restart, timeout, and cancellation reconciliation preserve the original attempt where recovery is possible rather than silently creating a replacement writer.

Fabric MCP Operator tools provide read-only node, placement, attempt, evidence, and routing data. The selected-route record carries authoritative health, capability-freshness, eligibility, transport-backend, and authority-ceiling fields used to explain placement.

See [v0.8.0 release notes](release-notes-v0.8.0.md) for the two-machine acceptance boundary and the known historical-timeout presentation limitation.

## Historical Flight Deck browser UI (removed)

The browser-only Flight Deck presentation and its legacy Operations Bridge routes were removed. MCP Operator, Fleet, Fabric, Mission, event, and session-control tools remain available under their existing gates; this historical name does not refer to a current browser surface.

### Review evidence (`hermes_review_accept`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_review_accept(contract_sha256, task_id, assignee, reviewer, verdict, evidence_refs, approval_reference, dry_run, confirm)` | **owner** + direct + confirm | Write a review-acceptance record for a Work Contract. Distinct reviewer is enforced at write time (`reviewer != assignee`); verdicts are bounded to `SATISFIED` / `NOT_SATISFIED`; evidence is referenced, never copied. |

The Work Contract validator reads the review-evidence store as an additional
evidence source while keeping the v0.6 audit + human-approval paths. A
`SATISFIED` acceptance by a reviewer distinct from the assignee satisfies the
review check; a self-review record never does.

### Structured event history (`hermes_events_*`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_events_query(source, subject_id, kind, since, until, limit)` | read_only + allowlist | Query the normalized, redacted event timeline (audit / swarm / codex / cron / kanban). |
| `hermes_events_tail(limit)` | read_only + allowlist | Recent events across all allowed sources. |

The event surface is a derived read-model over existing durable stores; it
never writes. Allowlist: `HERMES_GPT_EVENTS_ALLOWED_SOURCES` (unset = all
read-only sources; list = only listed; empty = none). Retention:
`HERMES_GPT_EVENTS_MAX_AGE_DAYS` (default 90). Redaction invariants match
Mission Control; prompts appear only as length/sha when present in the source.

### Durable token storage (`hermes_oauth_*`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_oauth_status()` | read_only | Durable token store presence/expiry only; never exposes token material. |
| `hermes_oauth_revoke(confirm, dry_run, rotate_key)` | **owner** + direct + confirm (pending legal scope decision) | Retire every durable token + advance the revocation epoch in one transaction; optionally rotate the active master key. |

OAuth access/refresh tokens are persisted through `token_store` (a
transactional SQLite store at `<hermes_data>/secrets/hermes_gpt_tokens.db`,
0600; per-row AES-256-GCM ciphertext; keyring → key file → env key
precedence) so a server restart does not invalidate credentials. No token
material is ever written to the audit log or any MCP response. The
`secrets/` directory is a denied path for all tools.

### Restart reconciliation (`hermes_swarm_reconcile`)

| Tool | Authority | Purpose |
| --- | --- | --- |
| `hermes_swarm_reconcile(apply)` | workspace/owner + direct (dry-run first) | Mark swarm stages stuck in `running` as `blocked` with `reason: interrupted_by_restart`; reload the durable token envelope; report a bounded summary. |

Reconciliation is fail-closed: it never auto-advances or auto-dispatches
work. The operator explicitly re-advances through the existing gated
`hermes_swarm_stage_advance`, which is idempotent for already-validated or
done stages (a re-advance returns current state as a no-op).

### v0.7 surface manifest

| Tool | Authority class | Gates | Audit | Allowlist |
| --- | --- | --- | --- | --- |
| `hermes_review_accept` | owner | direct + confirm | every call | n/a |
| `hermes_events_query` | read_only | allowlist | every call | `HERMES_GPT_EVENTS_ALLOWED_SOURCES` |
| `hermes_events_tail` | read_only | allowlist | every call | `HERMES_GPT_EVENTS_ALLOWED_SOURCES` |
| `hermes_oauth_status` | read_only | none | every call | n/a |
| `hermes_oauth_revoke` | owner | direct + confirm (pending legal) | every call | n/a |
| `hermes_swarm_reconcile` | workspace/owner | dry-run-first + apply | every call | n/a |

## Session history and session control

These surfaces are independent from the Operator level hierarchy and remain hidden unless their explicit legacy gates are enabled.

`HERMES_GPT_ENABLE_SESSION_SEARCH=1` exposes bounded read-only `hermes_session_search`, `hermes_session_list`, `hermes_session_read`, and `hermes_session_export`. Default transcript roles are `user` and `assistant`; internal roles additionally require `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1`. Export is in-memory and bounded, creates no files or paths, and lineage export fails closed.

`HERMES_GPT_ENABLE_SESSION_CONTROL=1` exposes `hermes_session_continue`, `hermes_session_send`, `hermes_session_create`, `hermes_session_job_status`, `hermes_session_job_wait`, and `hermes_session_job_result`. `hermes_session_create` creates a new session and starts its first prompt asynchronously; `hermes_session_job_wait` waits for a job to reach a terminal state. Session-control jobs use a fixed `hermes --resume <id> --oneshot <prompt>` argv with `shell=False`, bound prompt/timeout/result sizes, permit one active job per session, and persist prompt length/hash rather than raw prompt text. Restart reconciliation marks unowned running jobs orphaned rather than signaling a process from a persisted PID. The configured Hermes provider/model is used and may consume quota.

See [session history](session-history.md) and [session control](session-control.md). Treat transcript content as private local data.

## Fleet routing through the local A2A registry

Fleet routing uses only peers already present in the authenticated local Hermes A2A registry.

| Tool | Authority | Behavior |
| --- | --- | --- |
| `hermes_fleet_list` | read-only | List named peers without exposing tokens or peer URLs. |
| `hermes_fleet_status` | read-only | Return bounded compatibility metadata for one peer. |
| `hermes_fleet_dispatch` | workspace + direct + confirm | Submit bounded work to one named peer. |
| `hermes_fleet_task` | read-only | Return bounded task state. |
| `hermes_fleet_dispatch_work_order` | workspace + direct + confirm | Validate and submit a structured profile-aware work order. |
| `hermes_fleet_result` | read-only | Return a validated safe completion bundle. |
| `hermes_fleet_authority_drift` | read-only | Report registry/manifest/profile/role/Agent Card drift. |

MCP callers cannot supply an arbitrary peer endpoint, bearer token, SSH command, or executable.

If the peer reply times out after a dispatch was submitted, the dispatch tools do not lose the task: the peer-assigned task id is recovered by a bounded context lookup and returned in a structured `FLEET_DISPATCH_TIMEOUT` error with `submission_may_have_succeeded: true`, so the operator can poll `hermes_fleet_task` instead of guessing whether the remote side accepted the work.

### Fleet authority manifest

Set `HERMES_GPT_FLEET_AUTHORITY_MANIFEST` to an absolute JSON path or use the default location under the Hermes data root:

```text
<Hermes data root>/config/fleet-authority.json
```

Start from `examples/fleet-authority.example.json`.

The manifest defines expected peer identity/role/profile authority. It must not contain URLs, credentials, or tokens. Before direct structured dispatch, Hermes GPT rechecks live peer identity and role against this server-controlled manifest. A mismatch stops dispatch.

High-impact structured work additionally requires bounded approval metadata. Public-action detection and role authority are enforced locally before work is sent.

## Codex CLI jobs through the Operator server

This is different from installing Hermes GPT as an MCP server inside Codex.

The normal Operator server exposes asynchronous delegated Codex tools:

- `hermes_codex_status`
- `hermes_codex_plan`
- `hermes_codex_start`
- `hermes_codex_review_start`
- `hermes_codex_jobs`
- `hermes_codex_job_status`
- `hermes_codex_job_result`
- `hermes_codex_cancel`

Real execution requires:

- Operator Mode at `workspace` or acknowledged `owner` level;
- an allowed work directory;
- `HERMES_GPT_ENABLE_CODEX_RUNNER=1`;
- direct apply mode;
- `confirm=true`;
- `dry_run=false`.

`HERMES_GPT_ALLOW_CODEX_WRITE=1` is required only for `workspace-write`. Read-only Codex jobs do not need it.

Jobs default to `execution_mode="normal"`. An explicit per-job `execution_mode="nolo"` adds Codex's `-a never` approval policy while retaining the requested `read-only` or `workspace-write` sandbox. NOLO therefore removes Codex approval interruptions without granting `danger-full-access` or bypassing Hermes path, direct-mode, confirmation, or write gates. It ends with the job and creates no persistent global approval-bypass state.

`HERMES_GPT_CODEX_EXE` can pin an absolute standalone Codex CLI executable. Hermes GPT rejects protected WindowsApps shims and executables that fail a version probe.

For Codex acting as an MCP client, use [docs/codex.md](codex.md). For the Windows ChatGPT -> Hermes GPT -> Codex CLI deployment, use [windows-chatgpt-codex.md](windows-chatgpt-codex.md).

## Audit behavior

Preferred audit path on Windows:

```text
%USERPROFILE%\AppData\Local\hermes\logs\hermes_gpt_operator_audit.jsonl
```

Fallback:

```text
<hermes-gpt>\logs\hermes_gpt_operator_audit.jsonl
```

Audit records contain bounded operational metadata such as tool, level, apply mode, dry-run state, changed/success state, relevant IDs, and length/hash metadata for content-bearing operations.

Audit records do not intentionally persist raw prompts, `.env` values, vault contents, credentials, or full secret-bearing config values.

## Diagnostics and recovery

### `hermes_operator_doctor`

Read-only deep health check across the Operator surface. Checks include gateway state, config/env readability, cron/skills, policy, audit readability, and connector capability.

Gateway state is fail-closed: `hermes_operator_doctor` never reports the gateway as healthy on a heartbeat file alone. A heartbeat with no live gateway PID fails with `GATEWAY_PID_MISSING`; a dead PID fails with `GATEWAY_DEAD_PID`; an unreachable gateway fails with `GATEWAY_UNREACHABLE`. Stale heartbeat files surface as `GATEWAY_STALE_HEARTBEAT` warnings.

Status vocabulary:

- `PASS`
- `WARN`
- `FAIL`
- `UNSUPPORTED`

### `hermes_operator_snapshot`

Returns one bounded current-state summary with a recommended next action.

### `hermes_operator_recover`

Conservative recovery planner. Dry-run is the default. Use `apply=false` first.

Actual recovery mutation requires `apply=true` plus the normal direct/workspace policy gates.

### `hermes_release_doctor`

Use before preparing a release. It checks repository state, secret-file hygiene, package version/docs consistency, import/compile health, and that the server is not left in direct mode. `full_tests=true` also runs the test suite.

Release statuses are `PASS`, `WARN`, or `BLOCKED`.

## Profile and path normalization

Typical Hermes data roots:

- Windows: `C:\Users\<YOU>\AppData\Local\hermes`
- Unix/macOS: `~/.hermes`

If `HERMES_HOME` points at a named profile or the Hermes Agent source directory, Operator profile operations normalize back to the Hermes data root. The default profile maps to that root; named profiles map under `profiles/<profile-name>`.

`HERMES_GPT_OPERATOR_ALLOWED_PATHS` should contain only the workspaces the Operator server is expected to touch.

## Remote access posture

Keep the MCP server bound to `127.0.0.1`.

For a remote client:

1. keep Hermes GPT on loopback;
2. put an authenticated/private boundary in front of it;
3. use `dry_run` for routine always-on access;
4. switch to `direct` only for a deliberate maintenance session;
5. switch back after the session;
6. never leave Owner Mode enabled on an always-on connector.

Operator Mode itself does not add public-network authentication.

## Secret-path policy

The server refuses or redacts access to secret-looking locations even at high authority, including categories such as:

- `.env` files;
- auth/token/cookie stores;
- vault secrets;
- SSH keys;
- AWS credential paths;
- MCP token files;
- other secret-looking filenames.

Owner Mode does not disable this policy.

## Troubleshooting

### Connector shows an old or incomplete tool list

- Verify the running server process is the one you expect.
- Inspect the MCP tool list directly.
- Reconnect or recreate the client connector if it cached an older schema.
- Do not rely on a fixed expected tool count; the count changes as gated surfaces evolve.

### A profile appears missing

- Check `HERMES_HOME`.
- Confirm root normalization.
- Remember that `default` resolves to the data root while named profiles live under `profiles/<profile-name>`.

### A mutating tool refuses

Check the exact tool's requirements plus:

- `HERMES_GPT_OPERATOR_ENABLED`
- `HERMES_GPT_OPERATOR_LEVEL`
- `HERMES_GPT_OPERATOR_APPLY_MODE`
- `dry_run`
- `confirm` when the tool defines that gate

A policy refusal is often the correct result.

### Owner tools refuse

Verify both Owner activation and the exact acknowledgement string:

```powershell
$env:HERMES_GPT_OWNER_ACTIVE="1"
$env:HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"
```

## Related docs

- [Documentation map](README.md)
- [Missions (v0.9)](missions.md)
- [Delegations (v0.9)](delegations.md)
- [Live events (v0.9)](live-events.md)
- [Codex integration](codex.md)
- [Windows ChatGPT -> Codex](windows-chatgpt-codex.md)
- [Updating](updating.md)
- [Retention policy](retention-policy.md)
- [v0.8.0 release notes](release-notes-v0.8.0.md)
- [v0.7.0 release notes](release-notes-v0.7.0.md)
- [v0.6.0 release notes](release-notes-v0.6.0.md)


## Runner backend trust boundaries

Runner backends are execution transports for already-authorized work contracts;
they are not completion authorities. Contract validation, expected artifacts,
review requirements, and forbidden-action checks remain outside the backend.

### Filesystem confinement

Every `pi_rpc` session is gated on **OS-level filesystem confinement**, not on
the process working directory. CWD is not a sandbox: absolute paths, `..`
traversal, shell `cd`, and outward symlinks can escape a plain current working
directory.

Confinement uses bubblewrap (`bwrap`) on Linux and `sandbox-exec` on macOS to
make `allowed_scope.workspaces` a physical read boundary. Read-only Pi sessions
receive the authorized workspace read-only. Write-capable sessions receive that
workspace read-write only after the independent authorization and sandbox gates
have passed. Only the minimal read-only runtime trees required to execute Pi are
exposed outside the workspace.

Configuration:

- `HERMES_GPT_ENABLE_RUNNER_CONFINEMENT=1` — opt in to confinement (off by
  default; when off, `pi_rpc` dispatch fails closed, including read-only runs,
  because workspace read scope cannot otherwise be enforced).

Every `pi_rpc` contract requires an allowed workspace and a successful bounded
capability probe for its exact posture. A write-capable contract additionally
requires **all** of:

1. a write-authorized class (`reversible_write` or `high_impact`); `none` and
   `read_only` can never enable `bash`, `edit`, or `write`,
2. `execution.options.sandbox=workspace-write`,
3. `HERMES_GPT_ENABLE_RUNNER_CONFINEMENT=1` with the OS confinement binary
   installed **and a bounded capability probe succeeding on the current host**.

Binary presence alone is not treated as availability. The capability probe
launches the same confinement posture used for the runner child. Both postures
prove that the workspace is readable while absolute, `..`, and outward-symlink
reads cannot reach a sibling host file. Writable posture additionally proves an
in-workspace host write succeeds while an out-of-scope host write cannot modify
the corresponding host path. Read-only posture proves the workspace itself
cannot be mutated. Probe failure, timeout, or an unusable namespace/profile
causes dispatch to fail closed with a `PermissionError` before Pi starts.

Before any Pi child is wrapped, the workspace boundary is validated for
path/inode/mount aliases. Pre-existing outward symlinks, hard links with any
alias outside the workspace, special filesystem entries (device nodes, FIFOs,
sockets, etc.), and nested mount points/filesystems are rejected. This matters
for reads as well as writes: any of those aliases/channels can expose
out-of-scope host data through an in-workspace pathname even when the workspace
itself is read-only. Artifact/path validation also rejects absolute paths, `..`
traversal, and symlink escapes where Hermes itself resolves paths
(`runner_confinement.confine_path`). Pi's built-in read tool is protected by
the OS confinement boundary rather than by that helper.

On Linux, the wrapper exposes only the read-only system/runtime trees needed
to launch the runner, plus a narrowly selected read-only runtime tree when the
runner executable is installed outside system prefixes (for example Pi below
`~/.local/.../node_modules`). The rest of the user's home directory is not
mounted into the sandbox. `/tmp` and `/run` are private sandbox filesystems, so
host temporary files and Unix-domain service sockets are not exposed. The
authorized workspace is bound read-only for read-only sessions and read-write
only for authorized write sessions. All Linux capabilities are dropped so a
privileged launch cannot remount the boundary writable. On macOS, the
`sandbox-exec` profile likewise denies host reads outside the workspace and a
narrow runtime allowlist. System code is limited to required system/runtime
roots, while host configuration trees such as `/Library`, `/etc`, and
`/private/etc` are not exposed wholesale; only concrete runtime files needed
for name resolution, service lookup, devices, and time data are allowed. A
non-system Pi/Node installation is exposed only through its narrowly selected
runtime tree. Writable posture adds a workspace write exception and nothing
broader.

Install `bubblewrap` (e.g. `apt install bubblewrap`) and verify it works in your
environment; some containerized hosts restrict user namespaces. In any case,
capability-probe failure disables `pi_rpc` entirely rather than silently
falling back to an unconfined read-only child.


### OpenCode remote attach (read-only only)

The native `opencode` runner has an optional remote mode. It is **off by default**. When enabled, Hermes invokes the installed OpenCode CLI with `run --attach` against the fixed private endpoint `http://hermes-opencode:4097`; it does not accept a caller-supplied URL. The existing local OpenCode mode is unchanged and still requires the local OS confinement probe, including for read-only work. Remote mode does not use local `bwrap`, and a remote failure never falls back to local execution.

Remote mode accepts only an approved `read_only` Work Contract with a read-only sandbox. It forces the server agent `hermes-readonly` and the exact model `hermes-proxy/openai/gpt-6-luna`; write, shell, task, web, MCP, external-directory and other tools must remain denied. Hermes checks the effective default model, configured default agent, actual server agent, model and permissions on each bounded preflight. It also requires the exact provider/model allowlist entries below. A missing setting or failed check makes `hermes_runner_list` report the backend unavailable and blocks dispatch.

#### Topology and templates

The files in [`deploy/opencode/`](../deploy/opencode/) are templates for manual installation. They do not deploy or modify a server. The topology is:

- Keep OpenCode bound to `127.0.0.1:4096` inside `hermes-opencode`; do not set `OPENCODE_SERVER_PASSWORD` or publish port 4096.
- Run the supplied Nginx sidecar as a non-root process with a read-only root filesystem, no capabilities, `no-new-privileges`, bounded resources, no published port and `network_mode: service:opencode`. It shares the OpenCode server network namespace, listens on 4097, and proxies only to `127.0.0.1:4096`.
- Nginx enforces Basic auth, disables access logging, and removes both `Authorization` and `Proxy-Authorization` before proxying. The Gateway sends Basic auth only from its client environment. Store only a verifier in the sidecar's mounted `htpasswd` file; the OpenCode process and Hermes Agent must not receive the Gateway password.
- Keep Gateway, Hermes Agent, `hermes-opencode`, and the proxy on the existing private `hermes-opencode-int` network. Do not add a published host port or proxy environment setting. The runner rejects DNS results that are not private addresses.
- Mount one dedicated host directory read-only in both containers: Gateway `/opt/data/opencode-workspaces` and OpenCode `/workspaces` must resolve to the same verified Docker bind source or named volume. Do not assume the existing `/workspaces` mount is shared with Gateway `/opt/data`; the current sources may differ. Do not mount `/opt/data`, the full Gateway or OpenCode home, or a parent that contains unrelated data. The server root filesystem stays read-only; do not weaken seccomp/AppArmor or container capabilities.

The supplied Compose file defines both containers. It preserves the server image `node:22-bookworm`, command `opencode serve --hostname 127.0.0.1 --port 4096 --pure --log-level ERROR`, UID:GID `1000:1000`, read-only root filesystem, dropped capabilities, `no-new-privileges`, 2 GiB RAM, 1 CPU, 256 PIDs, and restart policy; it publishes no ports and sets no server password or provider key. It sets the non-secret runtime values `HOME=<opencode-home>`, `XDG_CONFIG_HOME=<opencode-config-home>`, `XDG_DATA_HOME=<opencode-data-home>`, `XDG_CACHE_HOME=/tmp/cache`, `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_CONFIG=/opencode.json`, and `OPENCODE_CONFIG_DIR=<opencode-home>/.config/opencode`. The config directory is the existing writable tmpfs; only `${OPENCODE_AGENT_CONFIG_DIR}` is bind-mounted read-only at `<opencode-home>/.config/opencode/agents`, so OpenCode can create its required `.gitignore` without making the agent mount writable. It has no `env_file` and passes no provider, WebUI, or Operator secrets to the server. It reuses external named volumes `hermes-opencode-app` (read-only at `/opt/opencode`) and `hermes-opencode-state` (at `<opencode-home>/.local`). `/opencode.json`, the agent directory, and `/workspaces` are read-only mounts; only `${OPENCODE_WORKSPACE_SOURCE}` is mounted at `/workspaces`, read-only. The service attaches only to external network `hermes-opencode-int`, which must already exist and have Docker's `Internal=true` property. Verify the named volumes contain the live server installation/state before starting; do not let Compose create replacement empty volumes.

Set the non-secret Compose inputs in the deployment environment: `OPENCODE_CONFIG_FILE` to the existing server config file, `OPENCODE_AGENT_CONFIG_DIR` to the directory directly containing `hermes-readonly.md`, `OPENCODE_WORKSPACE_SOURCE` to the dedicated common workspace source, and `OPENCODE_PROXY_HTPASSWD_FILE` to the verifier file path. Set `OPENCODE_NGINX_IMAGE` to an approved immutable Nginx image reference (version plus digest). Create an `htpasswd` verifier outside this repository with a strong generated password and the same username/password that will be supplied only to the Gateway; use a hash format supported by the selected Nginx image. Mount the verifier read-only and make it readable only by the proxy's non-root UID. Do not place a plaintext password in Compose, Nginx config, a command line, or this repository. `docker compose -f deploy/opencode/docker-compose.yml up -d` starts the loopback-only server and separate proxy; verify there are no published ports and the only proxy upstream is `127.0.0.1:4096` before connecting the Gateway.

Place `deploy/opencode/hermes-readonly.md` directly in the host directory configured by `OPENCODE_AGENT_CONFIG_DIR`; the read-only bind makes it available as `<opencode-home>/.config/opencode/agents/hermes-readonly.md`. Its model is pinned to `hermes-proxy/openai/gpt-6-luna`. The server service applies the non-secret JSON overlay through `OPENCODE_CONFIG_CONTENT`, setting `default_agent` to `hermes-readonly` and `model` to `hermes-proxy/openai/gpt-6-luna` as specified by `deploy/opencode/opencode.default-agent.fragment.json`, on top of the mounted live `OPENCODE_CONFIG=/opencode.json`. The fragment file is a reference and is not loaded automatically.
Do not replace provider configuration or copy credentials into this repository. This default is required because OpenCode's `run --attach` CLI can fall back to the configured default agent if it cannot resolve `--agent`. At runtime, the authenticated `/config` and `/agent` probes verify the effective default agent/model, the primary agent's pinned model, and its deny rules; any mismatch keeps the remote backend unavailable. Do not add plugins, MCP servers, tools, or extra permissions.
The profile grants only `read`, `glob`, `grep`, and `list`; it denies `bash`, `edit`, `write`, `patch`, `apply_patch`, `webfetch`, `websearch`, `task`, `external_directory`, and other tools. Its path rules deny `..`, `/proc`, and the secret-looking path patterns checked by Hermes. Do not broaden these rules to make preflight pass. If the OpenCode version or config format does not produce the expected permissions in `/agent`, remote mode stays unavailable.

Before setting `HERMES_GPT_OPENCODE_REMOTE_HARDENED=1`, run the deployment verifier from the Docker host after starting the Gateway, Agent, OpenCode server, and proxy. Provide the actual Gateway and Agent container names; also provide the WebUI container when one is deployed:

```bash
OPENCODE_WORKSPACE_SOURCE=/host/data/opencode-workspaces OPENCODE_CONFIG_FILE=/host/config/opencode.json OPENCODE_AGENT_CONFIG_DIR=/host/config/opencode python deploy/opencode/verify_deployment.py --gateway-container hermes-gateway --agent-container hermes-agent --webui-container hermes-webui
```

The verifier reads environment-variable names and whether each value is empty from Docker inspect; it never reads or prints environment values or mounted config contents. It requires `HERMES_GPT_OPENCODE_SERVER_PASSWORD` to be present by name in the Gateway and absent by name from the Agent, optional WebUI, OpenCode server, and proxy. It also requires `HERMES_WEBUI_PASSWORD` and `SERVICE_PASSWORD_HERMESWEBUI` to be absent or empty in Gateway and Agent. If WebUI is supplied, `HERMES_WEBUI_PASSWORD` must be present and non-empty, while `SERVICE_PASSWORD_HERMESWEBUI` must be absent or empty; no OpenCode auth variable may be present. The verifier inspects only names and empty/nonempty flags. It verifies that the Gateway's single `/opt/data` bind has the dedicated `opencode-workspaces` child as its source and that this exact source is mounted read-only at `/workspaces` in OpenCode.
The Gateway's own `/opt/data` mount permissions are not evidence of the remote mount's permissions. The verifier also checks the proxy UID, shared network namespace, no published ports, internal network, allowed mount destinations, tmpfs settings, capability/rootfs/hardening controls, and the live `/proc/1/status` values `Seccomp: 2` and `NoNewPrivs: 1` for both OpenCode and proxy. It emits only PASS/FAIL and safe metadata. A failed or incomplete check means remote mode must remain disabled.

Create `/opt/data/opencode-workspaces` on the Gateway host and mount that exact directory read-only at `/workspaces` in OpenCode. Put only explicitly approved, non-secret workspaces beneath it. At the mount root, create the marker file `.hermes-gpt-opencode-workspace` with exactly this single line:

```text
hermes-gpt-opencode-workspace-v1
```

Each Work Contract must name a workspace inside that root. Hermes maps only its relative subdirectory to `/workspaces/<relative-path>` and verifies that the OpenCode directory and project worktree do not widen that scope. The remote root and selected workspace are scanned for secret-looking paths, symlinks, hard-link aliases, special files and nested mounts; any finding blocks all remote work. Do not place `.env` files, credentials, provider keys, or other secrets in this root. `external_directory` is denied by both the profile and preflight. `/proc` is outside the workspace and must not be mounted into the workspace.

#### Gateway configuration and health

Set these variables in the **Gateway only**, using the Gateway's secret facility for the password:

```text
HERMES_GPT_OPENCODE_REMOTE_ENABLED=1
HERMES_GPT_OPENCODE_REMOTE_HARDENED=1
HERMES_GPT_OPENCODE_SERVER_USERNAME=<proxy Basic-auth username>
HERMES_GPT_OPENCODE_SERVER_PASSWORD=<proxy Basic-auth password>
```

`HERMES_GPT_OPENCODE_REMOTE_HARDENED=1` is an explicit operator acknowledgement that the private, sidecar-authenticated layout above has been installed; it is not a substitute for those controls. Set `HERMES_GPT_OPENCODE_SERVER_PASSWORD` only in the Gateway secret environment. Do not set or narrow the global `HERMES_GPT_RUNNER_BACKEND_ALLOWLIST`, `HERMES_GPT_RUNNER_PROVIDER_ALLOWLIST`, or `HERMES_GPT_RUNNER_MODEL_ALLOWLIST` just to enable OpenCode. The remote runner pins `hermes-proxy/openai/gpt-6-luna` in its code and the read-only server profile; any global allowlists already configured still apply as additional restrictions, while unset allowlists retain their existing behavior for every backend. The Gateway's fixed target is `hermes-opencode:4097`; do not configure a custom URL, HTTP proxy, or provider API key for the remote runner. Hermes passes only a minimal child environment plus the Basic-auth username/password required by the OpenCode CLI. It does not pass Hermes provider keys, Hermes profile environment files, the Hermes root, or `PI_CODING_AGENT_DIR` to the remote client.

`hermes_runner_list` performs a bounded, authenticated readiness probe. It verifies that an unauthenticated request receives HTTP 401, an authenticated request succeeds, the health response reports OpenCode `1.18.35`, the fixed hostname resolves only to private addresses, the marker content is visible at `/workspaces`, and the server reports the exact `hermes-readonly` primary agent, `hermes-proxy` model and required deny rules. The mount root is verified by its exact API directory and marker; each contract separately requires its selected project directory and OpenCode worktree to match exactly, so a broad or stale project root still blocks that contract. A stale/missing marker, wrong mount, changed project root, unsupported version, configured global provider/model allowlist that excludes the pinned values, wrong Basic-auth setting, unhealthy proxy/server, or any permission mismatch makes the remote backend unavailable. A configured backend allowlist that excludes `opencode` blocks dispatch. Diagnose with `hermes_runner_list` and the sidecar/OpenCode health logs; do not bypass a failing probe.

#### Dispatch, sessions, cancel, and output handling

The runner sends the Work Contract objective to the OpenCode CLI's stdin and runs `opencode run --format json --pure --attach http://hermes-opencode:4097 --dir /workspaces/<workspace> --agent hermes-readonly --model hermes-proxy/openai/gpt-6-luna`. It returns the final assistant text, redacted and bounded to 8,000 characters, through `hermes_runner_status` as `runs[].result` for the caller's review alongside observed Work Contract state. This text is a job result, not proof that completion criteria passed; Work Contract gates still require independent observed-state validation. The runner stores this bounded result in task metadata and a redacted 500-character summary in the shared job record. Audit records do not contain this result. Raw objectives are kept only in the transient request envelope, removed after worker startup; raw JSON event payloads, raw stdout/stderr, and auth values are never written to runner logs, metadata, or audit records. The Basic password is supplied only in the CLI's minimal child environment, never argv or prompt. Runner event logs contain only bounded event types/timestamps and the session ID needed for control; stderr is discarded. The client and remote session use the existing Work Contract timeout and cancellation gates. On timeout/cancel, Hermes tries the OpenCode session abort endpoint through the authenticated proxy, then terminates the local CLI; if the remote abort cannot be verified, it does not claim that the remote computation stopped.

OpenCode session reuse is opt-in with `execution.options.session_id`. Hermes accepts only a `ses...` ID whose API record still matches the selected workspace, `hermes-readonly` agent, verified model/provider allowlists, and safe permissions, then passes it via `--session`. The session ID is returned in the bounded runner observation and can be supplied to a later Work Contract. Hermes does not use `--continue` or silently attach to a different session. If the server cannot prove the session metadata, resumption is refused; starting a new contract does not implicitly resume prior context. Reuse does not bypass Work Contract scope, authorization, or completion validation.

#### Upgrade, rollback, and troubleshooting

Upgrade OpenCode and Nginx only as a coordinated, reviewed change. Pin the candidate Nginx image, review the upstream image and config compatibility, and verify the OpenCode version and `/agent` response in a non-production instance before changing the running service. The current runner intentionally accepts only OpenCode `1.18.35`; a different version remains unavailable until its API, event format, agent permission output, and tests are reviewed. Re-run the unauthenticated-401, authenticated-health, marker, workspace-root, agent-policy, and model-allowlist checks after an upgrade.

To roll back, disable `HERMES_GPT_OPENCODE_REMOTE_ENABLED`, stop the sidecar, and restore the previously approved pinned images/configuration. Existing local OpenCode still requires its normal local confinement probe; there is no automatic local fallback from an attached job. Keep the proxy verifier outside the repo and revoke/replace it in the Gateway secret store if exposed. A 401 means compare the Gateway client credentials with the verifier username/hash. A 502 or failed version check means inspect the proxy upstream and loopback-only OpenCode health. A workspace or agent rejection means inspect the mount marker, read-only mount, project `worktree`, hard-link/symlink/mount scan, agent name/mode/model, permission rules, and exact allowlists; do not weaken any denial or containment check.


External runner plugins are trusted in-process code. Setting
`HERMES_GPT_ENABLE_RUNNER_PLUGINS=1` only enables discovery; each external entry
point must also be named in `HERMES_GPT_RUNNER_PLUGIN_ALLOWLIST`. Plugin code runs
inside the Hermes GPT process and should be installed only from sources trusted
as much as Hermes GPT itself.

Deployments can constrain autonomous routing with comma-separated allowlists:

- `HERMES_GPT_RUNNER_BACKEND_ALLOWLIST` for backend names such as `fleet`,
  `pi_rpc`, `omx`, `codex`, and the explicitly selected `openhands` backend.
- `HERMES_GPT_RUNNER_PROVIDER_ALLOWLIST` for provider names selected by Pi.
- `HERMES_GPT_RUNNER_MODEL_ALLOWLIST` for model names selected by Pi, OMX, or
  Codex.

Unset allowlists preserve compatibility and allow all values. Set allowlists are
enforced before dispatch so contracts cannot silently route work into an
unexpected backend, provider, or model.

Local runner request envelopes are transient. Workers unlink `*.request.json`
files immediately after loading them, and runner listing/status performs TTL
cleanup of stale envelopes left by a process that died before loading its
request. Durable runner metadata intentionally remains bounded state/exit data,
not prompt text or model output.

Local runner timeout and explicit-cancellation cleanup share one platform-aware
path. POSIX signals the runner process group. Windows uses `taskkill /T /F` for
the process tree and falls back to direct process termination if `taskkill` is
unavailable, times out, or reports failure.

## Autopilot (v0.13)

Autopilot is an optional, default-off runtime that drives one Mission through its MissionPlan. It is enabled by the machine gate `HERMES_GPT_AUTOPILOT=1`, which registers `hermes_autopilot_start`, `hermes_autopilot_status`, and `hermes_autopilot_stop`; with the gate unset none of them exist. Starting needs `workspace` level, direct apply mode, `dry_run=false`, and `confirm=true` (a dry run previews and writes nothing); stopping never needs the machine gate. It adds no authority: every dispatch goes through the existing placement, Work Contract, and delegation surfaces, it never dispatches or advances an approval or `high_impact` node, and it cannot approve a Mission. See [autopilot.md](autopilot.md).
