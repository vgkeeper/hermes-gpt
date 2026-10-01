# Hermes Work Bridge adapter

Hermes GPT owns only this small MCP client. The service implementation, DB, job watch, Slack outbox, and deployment are in the independent `vgkeeper/hermes-work-bridge` repository.

## Tools exposed in Pilote

- `hermes_work_mission_register(project_id, mission_id, session_id, job_id)`
- `hermes_work_mission_update_job(mission_id, job_id)`
- `hermes_work_mission_get(mission_id)`
- `hermes_work_mission_cancel(mission_id)`

These operations call the fixed `HERMES_WORK_BRIDGE_URL` with a bearer token from `HERMES_WORK_BRIDGE_TOKEN` or a narrowly mounted `HERMES_WORK_BRIDGE_TOKEN_FILE`; Work does not need arbitrary HTTP access or the token. The adapter rejects malformed IDs, caps response size/time, avoids printing token or response internals on errors, requires TLS except for explicitly private service DNS, and fails closed when unconfigured.

## Work/Sheet lifecycle

Work owns spreadsheet `Registre missions Hermes`, one tab per stable `project_id`. Each row maps `mission_id=<project_id>_mNNN` to one dedicated Hermes `session_id` and its latest `current_job_id`. Required columns: mission_id, title/objective, mission status (`EN_COURS`/`BLOQUÉ`/`TERMINÉ`/`ANNULÉ`), session_id, current_job_id, branch/PR, dates, last verification/result/blocker.

1. Serialize project-local ID allocation, create an EN_COURS row with objective.
2. Start a dedicated Hermes session and initial job; persist returned IDs in the row.
3. Call `hermes_work_mission_register`. If response is ambiguous, use `get` before retrying.
4. On terminal Slack event, dedupe the stable `event_id` in durable Sheet metadata; validate mission/session association and retrieve exactly that job's result.
5. On each session continuation, call `update_job` with its returned job ID and update the Sheet's `current_job_id`. Retry the same update on a lost response.
6. On mission close/cancel, write final status/evidence, then call `cancel`. A job timeout does not set mission status.

The Slack trigger is only a wake-up. Do not send secrets or PII. Slack may repeat an event after an uncertain delivery; `event_id` is the consumer dedupe key.

## Runtime

This module registers four additive Pilote tools. Configure `HERMES_WORK_BRIDGE_URL` in the runtime, and pass the API credential only to that service through `HERMES_WORK_BRIDGE_TOKEN_FILE`; avoid a bridge token environment variable shared by Hermes Agent/WebUI. The token file is outside source control and mode-restricted. Full independent service API, Slack setup, and Coolify volumes/healthcheck are maintained in the bridge repository README.
