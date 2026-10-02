# Hermes MCP Gateway migration checkpoint

Date: 2026-10-02 15:00 UTC
Status: CLEANUP VERIFIED — external ChatGPT MCP E2E passed; legacy routes removed safely.

> This section is the current operational truth. All sections below are historical snapshots, retained for audit; their older status lines are superseded here.

## Current verified final state — 2026-10-02 15:00 UTC

- External ChatGPT E2E job `86e0c1790f4e4b84b8cc05fe8c16c703` is `completed`, `return_code=0`; `hermes_session_job_result` contains `MCP_GATEWAY_E2E_OK` (the captured process output has one terminal LF, and stripping that line ending matches the exact requested response).
- Previous cleanup attempt `2e96a7ca5dff472e9427c817f632e425` reached its 7200-second limit, `timed_out`, `return_code=-15`, no final response. Current resumption job `76fc5cf378e2450bba6221cc38e307a1` is the sole active job for this request; no second continue was launched.
- New `hermes-mcp-gateway` remains healthy and was not restarted: MCP `/` HTTP 200, tunnel `/readyz` HTTP 200 (`ready`), `tools/list=153`; `HERMES_HOME=/home/hermes/.hermes`, profile `default`, session search/internal-content/control/shared-state all enabled. Session `20261001_211353_35841b` is readable; session search, job status/result, and operator status calls succeed.
- Coolify source of truth `services.docker_compose_raw` for UUID `anmcxcpk5warlnxhyj7yvasy` was updated with a compare-and-swap guard from SHA-256 `cec5ce28dc94dbc5e79c81bb928d31d4c89a5986af9373739e414c2c897c9028` to `cb4c531098bddb58cb56b95401c75c91a71cd0afa3790875638bc29bbe89bcbe` (10,667 bytes). The resulting service set is `hermes-agent`, `hermes-agent-src-sync`, `hermes-webui`, `hermes-mcp-gateway`; the old runtime service and direct-MCP init bind are absent. The WebUI/gateway definitions and top-level volume definitions are unchanged. `docker compose config --quiet` passed on the candidate that now matches the DB row.
- To avoid regenerating `.env` or restarting Hermes Agent, `saveComposeConfigs()` and deployment were deliberately not run. The Coolify DB row is the source of truth; the generated on-disk Compose file remains the last-deployed snapshot until a later normal Coolify render/deploy. No `.env` content was printed, copied, or written; the existing file was read internally only by Docker Compose for candidate validation.
- Safe cleanup completed: s6 service `hermes-gpt-mcp-main` is down and Agent network no longer serves 17677; old `hermes-gpt-runtime` exited cleanly and its container was removed without `-v`, so 7677/8080 are no longer served; the old tunnel-client launcher and staging `staging-control.py` were removed after root-only backup. The staging MCP process and PID file are stopped/absent. The current Agent container still has the old init-script bind mount until its next normal redeploy; its source bind is removed, the process is down, and `main-init.sh` is retained for rollback. Hermes Agent was not restarted.
- Staging state/history, the staging tunnel-client binary and logs, all live/historical volumes, and seven stopped Hermes GPT rollback containers were preserved. No volume prune/delete or image prune was run. The rollback containers were retained because they still reference shared Hermes home/workspace mounts and are useful rollback artifacts.
- Post-cleanup: Hermes Agent, WebUI, and Work Bridge remain `healthy`, with unchanged `StartedAt` and `RestartCount=0`. The Agent `gateway-default` and `main-hermes` s6 services are up; Hermes `status --all --deep` reports Telegram configured. No Telegram message, configuration change, or restart was performed; the status output did not expose an independent bot-connectivity flag. The only active `tunnel-client-runtime` is in the new gateway; no legacy client remains.
- Root-only backup directory: `/data/coolify/backups/hermes-mcp-gateway/20261002T132814Z/` (directory 0700, files 0600), including pre-mutation Coolify raw source, rendered Compose snapshot, validated cleanup candidate, and copies of the removed launcher/control/PID artifacts. No `.env` or credential values were copied.
- Security follow-up: coordinate rotation of the potentially exposed Coolify Redis and WebUI credentials during a planned maintenance window. No secret values are recorded here and no rotation was attempted in this task.


## Earlier session/job snapshot (07:51 UTC)

- Session to preserve: `20261001_211353_35841b`, profile `default`.
- Previous timed-out job: `1a01176a26b645728ffa86339582b331`, status `timed_out`, return code `-15`, no final response.
- Job `3b10bcb17ba242dabc53a2f3ead86740` also reached `timed_out` at its 7200-second limit.
- This resumption job: `15e14f647495441a987e440bc0028901`, active at 07:51 UTC at the time of that snapshot; its state is historical, not current.

## Pre-cutover live architecture snapshot

- Hermes Agent container `hermes-agent-anmcxcpk5warlnxhyj7yvasy` remains healthy and was not restarted. Telegram's `gateway-default` s6 service is up.
- Old Hermes Pilote on `127.0.0.1:17677` still answers HTTP 200 and serves its old MCP tools. Old sidecar/tunnel runtime `hermes-gpt-runtime-anmcxcpk5warlnxhyj7yvasy` and port `7677` remain running.
- Existing “Hermes MCP Events Staging” route is unchanged: MCP `17678` and tunnel health `17679/readyz` answer 200, with isolated HERMES_HOME under `/opt/data/mcp-events-staging-state`. Tunnel `tunnel_6abed8e0c29c819199ca0a6d459da501` has NOT been moved.
- Old production tunnel health `8080/readyz` returned 503; do not rely on it as a proven fallback.
- Hermes WebUI and `hermes-work-bridge` remain healthy and were not restarted.

## Dedicated gateway deployed in parallel

- Coolify service/container: `hermes-mcp-gateway-anmcxcpk5warlnxhyj7yvasy`, ID prefix `a64f442fa08f`, healthy.
- Image: `hermes-gpt-sidecar:operator-workspace-7759eab`, image ID `sha256:ae865e56202e62b7a5848c20b520efa6897765b2881e8c9d67ad3ba625ad4439`.
- User `1000:1000`; normal Docker network `anmcxcpk5warlnxhyj7yvasy`; no shared PID/network namespace; no published ports.
- Exact live data mounts, with no copy/new Hermes volume:
  - `/var/lib/docker/volumes/anmcxcpk5warlnxhyj7yvasy_hermes-home/_data` → `/home/hermes/.hermes`, read/write.
  - `/var/lib/docker/volumes/01775f0f2646018d413fdd64b2057d3d902e386c3b7b3bc744a38a429e37f361/_data` → `/opt/data`, read/write.
  - Existing bridge token file → `/run/secrets/hermes_work_bridge_token`, read-only; no value stored here.
- `HERMES_PROFILE=default`; session search/internal-content/control and `HERMES_GPT_SESSION_CONTROL_SHARED_STATE=1` enabled; Operator posture remains workspace/direct, Owner Mode disabled.
- The gateway uses the Hermes Agent image's Python environment (`/opt/hermes/.venv`), not the repo's development venv; the latter lacked `psutil` and caused session reads to fail before correction.
- `HERMES_MCP_GATEWAY_TUNNEL_ENABLED=0` intentionally. The gateway is healthy locally, but it does not own the staging tunnel yet.

## Validated from the new gateway

- `tools/list`: 153 tools; session, job, bridge, and workspace tools are present.
- `hermes_session_read` for the exact target session, `include_tool_messages=true`, `limit=3`: success, 3 messages; internal-content-disabled error absent. No transcript text was emitted.
- `hermes_operator_status`: profile `default`, workspace/direct, Owner Mode inactive.
- Shared job store visibility: current foreign job remains `running` with `process_visibility=external_pid_namespace`; the prior timeout job is visible as `timed_out`; a completed job result from the same session is readable. No output text was printed.
- A same-session continue request was safely refused with `SESSION_BUSY` because the resumption job itself is still active. No extra job was created.
- `server/discover` and `events/list`: HTTP 200; `hermes.test` listed. No event was emitted and no callback subscription was created.
- Bridge DNS works and a read-only `hermes_work_mission_get` succeeded for an existing test mission. No bridge mission is associated with the target session.
- Infisical names-only probe in the new container found `CONTROL_PLANE_API_KEY` present from Cloud path `dev` / `/mcp-events-staging`; no key value printed. Tunnel runtime binary `0.0.14` is present.

## Source, tests, backup

- Repo `/opt/data/hermes-gpt-source`, branch `feat/mcp-events-prototype`, remote head `000ecec4b2479627152948f1685b0c6632de296d`. The worktree was clean before creating this checkpoint; `docs/hermes-mcp-gateway-checkpoint.md` is now the only expected uncommitted file and should be committed/pushed after verifying the note.
- Full suite: 1345 passed, 7 skipped. Ruff passes on new Events/gateway files; legacy Ruff findings are unchanged from HEAD. `git diff --check`, package build, and package-hygiene checks pass.
- The branch was not merged to `main`: MCP Events remains experimental (`hermes.test` only, no durable delivery outbox/stable account principal), and actual ChatGPT E2E is unverified.
- Coolify's `services.docker_compose_raw` for UUID `anmcxcpk5warlnxhyj7yvasy` contains the new service; generated Compose parsed and `docker compose config --quiet` passed. Use explicit `:rw` bind mounts to avoid Coolify converting long-form `read_only: false` binds to `:ro`.
- Root-only original backup: `/data/backups/hermes-mcp-gateway/20261002T062512Z/docker_compose_raw.before.yml` and `docker_compose_rendered.before.yml`; further pre-correction snapshots are in the same directory. No `.env` or secret values were copied.

## Blockers — preserve old routes

1. Browser Use reports `chrome-not-running`; no supported Chromium, CDP endpoint, or `/opt/data/browser-stack` is available in the Hermes Agent container. Therefore a normal tool call through the actual ChatGPT/Work connector cannot be verified here.
2. The staging tunnel still points to the isolated staging server. Since client-side E2E is unavailable, do NOT stop that client, do NOT enable the new gateway tunnel, and do NOT disable 17677/7677. The old paths remain intact.
3. The active resumption job is still running in the target session. Do not attempt a second mini-continue or stop its parent 17677 process from this job.
4. Security follow-up: an earlier broad Docker inspection output accidentally included a `coolify-redis` `--requirepass` credential. Its temporary terminal capture file was overwritten and deleted, but conversation output may retain it. No value is stored here; the credential was not rotated because that requires a coordinated Coolify/Redis change. Treat it as potentially disclosed and coordinate rotation. The generated Coolify `.env` is mode `0644`; values were not inspected or changed.

## Rollback

To roll back only the new gateway and restore the original Coolify service source:

1. Restore `services.docker_compose_raw` from `/data/backups/hermes-mcp-gateway/20261002T062512Z/docker_compose_raw.before.yml`.
2. Run Coolify `Service::parse()` and `saveComposeConfigs()` for UUID `anmcxcpk5warlnxhyj7yvasy`.
3. Stop/remove only `hermes-mcp-gateway-anmcxcpk5warlnxhyj7yvasy`.

Do not prune/recreate Hermes volumes, change the root init launcher, stop the staging tunnel, stop 17677/7677, or restart Hermes Agent, Telegram, WebUI, or bridge during rollback.

## 2026-10-02 controlled cutover attempt

Status: BLOCKED; no tunnel mutation.

- Docker reports `hermes-mcp-gateway-anmcxcpk5warlnxhyj7yvasy` running/healthy. Environment checked without exposing secrets: profile default; shared state=1; internal content=1; tunnel enabled=0. Mounts are the live Hermes home volume to `/home/hermes/.hermes` and live `/opt/data` backing source to `/opt/data`, both RW.
- Local MCP probe from gateway to `127.0.0.1:17677` failed; that port is not reachable in its namespace. Do not infer MCP/session health from Docker health alone.
- Host-wide Docker inventory did not identify any active staging tunnel-client or its launcher. No client was stopped. Staging MCP 17678, old 17677/7677, and other named services were left untouched.
- Infisical Cloud names/presence-only probe succeeded for dev `/mcp-events-staging`, but no approved mapping to the gateway's exact tunnel-client launcher/config was identified. No secret was displayed or injected; no Compose/env mutation or tunnel activation was attempted.
- Rollback requirement for a future cutover: stop only the new gateway tunnel-client; relaunch the specifically identified old staging client with its existing approved Infisical configuration. The exact old client/launcher must first be discovered and restartability verified; until then rollback is not proven.
- No `hermes.test` event was emitted. No Hermes Agent, Telegram, WebUI, or bridge restart. No changes to 17677 or 7677.

Historique du précédent essai : `READY_FOR_CHATGPT_E2E` n'était pas satisfait alors, aucun tunnel n'avait été basculé. See Vault note `Sessions/2026-10-02-hermes-mcp-gateway-migration-checkpoint.md`.

## Cutover contrôlé — 2026-10-02 09:16 UTC

Résultat : READY_FOR_CHATGPT_PLUGIN_RETEST. Le tunnel partagé est maintenant attaché au nouveau gateway; aucun appel ChatGPT externe n'est affirmé ici.

### État et validation après bascule

- Le tunnel staging était dans le conteneur `hermes-agent-anmcxcpk5warlnxhyj7yvasy`; cgroup Docker `ae8297992b2dee858618d920b36b03a2dce99879c970e8dab11f09c2ba2bc5bb.scope`. Le client `tunnel-client-runtime`, tunnel ID `tunnel_6abed8e0c29c819199ca0a6d459da501`, était groupé avec ses deux wrappers Infisical, PGID 27274, PID launcher 27274 / client 27290, health 17679. Le PID group a été vérifié et seul ce groupe a reçu SIGTERM via son helper identitaire; staging MCP lui-même n'a pas été arrêté.
- Ancien tunnel staging arrêté; son serveur MCP isolé reste en marche et `/` sur 17678 répond encore HTTP 200.
- Coolify source de vérité `services.docker_compose_raw` du UUID `anmcxcpk5warlnxhyj7yvasy` passe à `HERMES_MCP_GATEWAY_TUNNEL_ENABLED=1`, même tunnel ID. `parse()` et `saveComposeConfigs()` exécutés; `docker compose config --quiet` passe. Le backup pré-mutation root-only est `/data/coolify/backups/hermes-mcp-gateway/20261002T090944Z/docker_compose_raw.before.yml` (répertoire mode 0700, fichier mode 0600, 12,948 octets). Le .env/aucun secret n'a été sauvegardé.
- Seul `hermes-mcp-gateway` a été recréé avec `docker compose up -d --no-deps --force-recreate hermes-mcp-gateway`. Aucun volume modifié. Hermes Agent, WebUI, Work Bridge gardent leur `StartedAt` antérieur; aucun restart Telegram.
- Le premier lancement a échoué car le flag était écrit `--log-format`; le tunnel-client installé accepte `--log.format`. Code corrigé dans `scripts/hermes_mcp_gateway.py`, test ajouté; 6 tests ciblés passent, Ruff et `git diff --check` passent. Commit poussé `b675ba390025bd8e7b9fda66caf984c6ffb7b934` sur `feat/mcp-events-prototype`.
- Vérification finale: conteneur gateway running/healthy; healthcheck du gateway passe, MCP `127.0.0.1:17678/` HTTP 200, `127.0.0.1:17679/readyz` body `ready` HTTP 200. `tools/list` retourne 153 outils; `hermes_operator_status` réussit; lecture bornée de la session `20261001_211353_35841b` avec `include_tool_messages=true` réussit sans contenu reproduit.
- Environment vérifié: `HERMES_HOME=/home/hermes/.hermes`, profile default, session search/internal-content/control=1, shared-state=1, Operator workspace/direct, tunnel enabled=1 et ID attendu. `hermes_operator_status` confirme le vrai Hermes home et non le staging path.
- Tunnel process actif; logs indiquent tunnel metadata fetched et poller started; readyz répond ready. Une ligne WARN `OAuth discovery failed` est présente, sans échec de readiness ni erreur de connexion du poller relevée. À signaler si le retest ChatGPT échoue.
- Aucun `hermes.test` émis. Les anciens endpoints 17677/7677 sont restés intacts. ChatGPT doit maintenant retester le connecteur `Hermes MCP Events Staging`.

### Rollback immédiat exact

1. Arrêter seulement le gateway nouveau (son superviseur termine également son tunnel-client) : `cd /data/coolify/services/anmcxcpk5warlnxhyj7yvasy && docker compose stop hermes-mcp-gateway`.
2. Restaurer la source persistante et régénérer le Compose : dans `docker exec coolify php artisan tinker`, définir `$s=App\Models\Service::where("uuid","anmcxcpk5warlnxhyj7yvasy")->firstOrFail(); $s->docker_compose_raw=file_get_contents(storage_path("app/backups/hermes-mcp-gateway/20261002T090944Z/docker_compose_raw.before.yml")); $s->save(); $s->parse(); $s->saveComposeConfigs();` (laisser le nouveau gateway arrêté).
3. Relancer le client staging existant uniquement, son MCP serveur restant actif : `docker exec --user 1000:1000 hermes-agent-anmcxcpk5warlnxhyj7yvasy python3 /opt/data/mcp-events-staging-state/staging-control.py start`.
4. Vérifier `staging-control.py status` et `http://127.0.0.1:17679/readyz` dans le conteneur Hermes Agent. Ne toucher ni volumes, ni 17677/7677, ni Hermes Agent/Telegram/WebUI/bridge.

## Session ownership correction — 2026-10-02 11:32 UTC

Status: patch committed/pushed and only the new gateway redeployed. The requested new mini-continue is paused safely because another legacy job in the exact same session is still running; wait for that job to become terminal before creating another.

- Reported job `1143c8b970994b68a6e8f006b1b3aee9` is completed, `return_code=0`, but its response did not equal `MCP_GATEWAY_E2E_OK`. Its `reconciliation` field is stale and not a successful E2E proof.
- Live diagnosis: gateway has shared-state enabled; old `hermes-gpt-runtime` has session control enabled but `HERMES_GPT_SESSION_CONTROL_SHARED_STATE` unset. The old code can mark a foreign PID-namespace job orphaned. Do not change/restart the old runtime or any legacy route in this task.
- Implemented in `operator_session.py` plus `operator_session_worker.py`: a detached worker holds a per-profile/session POSIX advisory lock in the shared Hermes home and renews an atomic heartbeat/expiry lease. Owner token, instance/container identity, worker PID and `/proc` start token are persisted internally and hidden from MCP views; PID is diagnostic only. A held lock or fresh matching lease prevents orphaning; an expired unlocked lease becomes orphaned only after grace. The worker handles timeout, output, and terminal status even if the MCP parent exits. The same lock prevents a second continue from another runtime.
- Tests simulate unavailable PID visibility and an actual short-lived MCP-like parent process exiting while its worker continues; they also verify two-runtime status/result agreement, lease grace/expiry, terminal result, and only one owner for concurrent continues. `pytest -q -rA test_operator_session.py test_job_result_pagination.py test_server.py`: 104 passed, 1 skipped (HTTP smoke requires a live server). Ruff, `git diff --check`, and `py_compile` pass.
- Commit `3007c52e381bad8189d2fd0eb0dca631d40f2b78` is pushed to `feat/mcp-events-prototype` and was read back from `origin`.
- Only `hermes-mcp-gateway` was recreated at 11:10 UTC (new container ID `28eec0f37463b9a46c8869562b940771bf6049ec4caaf3c893f74d6966003e67`). Hermes Agent, old runtime, WebUI, and Work Bridge retained their prior container IDs/StartedAt. Gateway is healthy; MCP `17678` lists 153 tools; `hermes_operator_status` is not an error; tunnel `17679/readyz` is `ready` and the tunnel-client process is present. Existing tunnel ID/config and mounted volumes were unchanged.
- Important inventory correction: the earlier host-side active-job scan checked `/home/hermes/.hermes`, which does not exist on the VPS host and therefore falsely reported zero active jobs. The real HERMES_HOME is the Docker named-volume mount; inventory it inside `hermes-mcp-gateway` or directly under that volume.
- First post-deploy `hermes_session_continue` attempt returned `SESSION_BUSY` and created no job. Actual shared-store inspection found legacy job `d98153189fc4485ab8d18bdcc4bb6743` in session `20261001_211353_35841b`: `running`, started `2026-10-02T10:22:44Z`, 7200-second max, no lease/return code. Its prompt metadata length is 4191 and does not match the minimal E2E prompt; prompt content is not stored here. PID 128530 is still a `hermes` process in the legacy/Agent shared namespace; its process start time matches the job start to milliseconds. Do not stop, claim, or duplicate it. Repeated 120-second `job_wait` checks from the new gateway still return `running`, with no result yet.
- Next action: continue waiting on `d98153189fc4485ab8d18bdcc4bb6743` until terminal; then, only after it releases the session, launch the exact requested prompt `Réponds uniquement MCP_GATEWAY_E2E_OK` through the new gateway, wait for terminal, and require `completed`, return code 0, and exact response. Current task remains incomplete until that check passes or the existing job reaches a real blocking terminal state.

