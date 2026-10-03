# Hermes session control

Hermes GPT can send one bounded non-interactive turn to an existing Hermes session and expose its status and result as an asynchronous MCP job. This lets an MCP client use the model/provider already configured in Hermes without invoking Codex.

## Enable locally

Session control is off and hidden by default. Enable it only on a trusted local MCP server:

```powershell
$env:HERMES_GPT_ENABLE_SESSION_CONTROL="1"
python server.py
```

Read-only history remains separately controlled by `HERMES_GPT_ENABLE_SESSION_SEARCH=1`. Enable both when the client needs to list or inspect sessions before choosing one to continue. See [session history](session-history.md) for its four-tool read-only workflow and privacy defaults.

For a dedicated gateway that shares the same `HERMES_HOME/session-jobs` directory
with another Hermes process but has its own PID namespace, also set
`HERMES_GPT_SESSION_CONTROL_SHARED_STATE=1`. New session-control jobs are owned by
an independent worker process which holds an advisory lock in the shared Hermes
home and refreshes an owner-token lease/heartbeat there. A restarted MCP server
can therefore observe the job without inspecting a foreign `/proc` PID. Status
reads preserve a running owner lease and annotate it with
`process_visibility: external_pid_namespace`; lease expiry is not enough to
orphan while the shared lock is still held. An expired lease with no lock is
reconciled as `orphaned`. Legacy jobs without a lease keep their prior
shared-state timeout behavior. Every runtime that reads or reconciles these
records must use lease-aware code (or the legacy shared-state flag); an older
runtime with shared-state disabled can still write a false orphan marker.

## Workflow

1. Find a session ID with `hermes_session_list` when history is enabled.
2. Call `hermes_session_continue(session_id, prompt, max_job_runtime_seconds, mission_id)` or its `hermes_session_send` alias. `mission_id` is optional; `hermes_session_create` accepts it too because it also creates an asynchronous job.
3. Save the returned `job_id`.
4. Poll `hermes_session_job_status(job_id)` until the status is `completed`, `failed`, `timed_out`, or `orphaned`.
5. Call `hermes_session_job_result(job_id)` for the bounded, redacted final output.

The start call resolves exact or unique-prefix IDs through Hermes' existing read-only `SessionDB` API before launching anything. It starts `operator_session_worker.py` with a private configuration payload over stdin (the prompt is not stored in job metadata), then the worker invokes the CLI with a fixed argument array equivalent to:

```text
hermes --resume <resolved-session-id> --oneshot <prompt>
```

No shell is used. Hermes restores the resumed session's recorded working directory using its normal CLI behavior. The worker, not the MCP server process, holds the shared session lock, renews the lease, enforces the runtime deadline, captures output, and publishes the terminal job record. The optional `mission_id` is validated with the Live Events safe-reference bounds and persisted in the job metadata. After atomically writing a terminal status, the worker publishes one deterministic-ID `topic=session`, `kind=job.terminal` event for that job. The bounded payload contains only `job_id`, `session_id`, `status`, and `return_code` when available; it never contains the prompt or job output. Empty `mission_id` preserves historical behavior and emits no event. A restarted reader retries publication from terminal metadata, while the event ID prevents a duplicate durable row. Events are notifications only; consumers re-read job status/result as authoritative evidence.

## Creating a new session

`hermes_session_create` creates a genuinely new, distinct Hermes session in the target profile and runs its first prompt through the same asynchronous job machinery. Its maximum job runtime is 7,200 seconds, and the default runtime is also 7,200 seconds. The default profile is `default`.

1. A new session row is created in the profile's Hermes session store (id shape
   `{YYYYmmdd_HHMMSS}_{6-hex}`, explicit source `hermes-gpt`), before any CLI call.
   An optional `title` is recorded when provided.
2. The first work runs in that new session via the fixed CLI argument array:

   ```text
   hermes --resume <new-session-id> --oneshot <prompt>
   ```

3. The call returns immediately with `success`, `session_id`, `job_id`, `profile`
   and `status`.
4. Follow with `hermes_session_job_wait(job_id)` then `hermes_session_job_result(job_id)`.

The new session id is generated before the CLI starts, so the returned `session_id`
is always the id of the freshly created session. Profile is restricted through the
same policy as `hermes_session_continue` (no arbitrary profile names); a failed
session-store write returns `SESSION_CREATE_FAILED` without launching anything.

## Bounds and persistence

- Prompt: maximum 65,536 characters.
- Max job runtime: `max_job_runtime_seconds` clamped to 10–7,200 seconds; default 7,200. Independent of `hermes_session_job_wait` (max 120 s per poll, never kills).
- Returned result: clamped to 500–24,000 characters.
- Concurrency: an advisory lock under `session-jobs/session-leases/` admits one worker per profile/session across independent runtimes.
- Job metadata: stored under the Hermes data root in `session-jobs/`; owner token, instance/container identity, worker PID, heartbeat, and lease expiry are internal fields and are removed from MCP views.
- Prompt privacy: raw prompts are not stored in metadata; only length and SHA-256 digest are retained. The worker command configuration is sent over stdin, not written to disk.
- Output: the worker captures locally for later result retrieval and output is redacted before MCP exposure.
- Parent-process restart: the independent worker keeps its lease and job state alive; another lease-aware runtime can follow the same record without `/proc` access.
- Reconciliation: a matching fresh lease or held shared lock prevents orphaning, even if a legacy reader previously wrote `orphaned`; a missing lock is treated as grace until the lease expires, then the job becomes `orphaned`.
- Compatibility: records created before leases remain readable. A runtime running pre-lease code with shared-state disabled can still falsely mark a new job orphaned; upgrade that reader or enable its shared-state mode before using it to observe active jobs. PIDs are never trusted or signaled across namespaces.

Session control can consume the configured provider's quota or incur provider charges. Do not enable it on an unauthenticated public endpoint, and review returned content before sharing it.

## Validation without a real model call

The automated tests include fake-process contract checks and local worker-process integration tests. They cover shared lock ownership, an MCP parent handle disappearing while the worker continues, a reader process without the owner PID handle, lease grace/expiry, concurrent continues, redaction, tool registration gates, and coherent status/result reads from two runtime processes. They do not contact a live model provider; the live gateway smoke test is recorded separately in the deployment checkpoint.
