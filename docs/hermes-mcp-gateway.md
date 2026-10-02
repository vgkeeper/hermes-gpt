# Dedicated Hermes MCP Gateway

This document describes the dedicated-container deployment shape for Hermes GPT/Pilote. It is an architecture and rollback guide, not evidence that a particular host is currently deployed; live image, commit, volume names, tunnel ID, and secret locations are recorded in the deployment-specific Vault session note.

## Target topology

```text
ChatGPT Work
  -> OpenAI Secure MCP Tunnel (outbound-only client)
  -> hermes-mcp-gateway container
       - hermes-gpt MCP server + draft MCP Events extension
       - tunnel-client-runtime
  -> shared Hermes state and workspace mounts

Hermes Agent remains a separate container and owns Telegram/WebUI services.
hermes-work-bridge remains a separate service reached over the Coolify Docker network.
```

The gateway has its own PID and network namespaces. Do not use `pid: service:hermes-agent` or `network_mode: service:hermes-agent`. Do not publish the MCP or tunnel-health ports on the host; both bind to loopback inside the gateway, and the tunnel client forwards to that local MCP endpoint.

## Shared state and authority

Mount the existing `$HERMES_HOME` read-write at the same path used by Hermes Agent; it is the canonical source for `state.db`, profile `default`, and `session-jobs`. Mount the existing workspace root at the same path for workspace/operator tools. Use the exact live backing sources; never create a new empty home volume or copy the session database. The Hermes Work Bridge remains independent: configure its existing URL and mount its existing bearer-token file read-only, but do not mount the bridge's internal database/jobs volumes into the gateway.

Run as the existing unprivileged Hermes UID/GID. Preserve the live Pilote posture: Operator enabled, `workspace` level, `direct` apply mode, profile allowlist `default`, and the same safe workspace root. Keep Owner Mode disabled. Enable only the required session gates (`HERMES_GPT_ENABLE_SESSION_SEARCH=1`, `HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1`, `HERMES_GPT_ENABLE_SESSION_CONTROL=1`) and set `HERMES_GPT_SESSION_CONTROL_SHARED_STATE=1`. Shared-state mode keeps recent foreign running jobs readable across separate PID namespaces and refuses a parallel continue for the same profile/session; it marks an unowned foreign job `timed_out` only after its recorded maximum runtime (7,200 seconds) expires.

The MCP server process uses the same `HOME`, `HERMES_HOME`, and `HERMES_PROFILE` values as the primary Hermes runtime. The Infisical child uses the approved nested home within the shared Hermes volume for its Universal Auth facade/config. Inject the tunnel runtime key only into the tunnel-client process via Infisical Cloud; do not put it in Compose, arguments, logs, or source. The Work Bridge bearer remains a read-only secret-file mount. Provider credentials are not copied into the gateway Compose environment; Hermes CLI uses the existing shared profile auth/config.

## Process and health behavior

The gateway supervisor starts the MCP server on `127.0.0.1:17678`, waits for its root health endpoint, then starts the tunnel client. The client targets the same container's `/mcp` endpoint and exposes its readiness endpoint only on `127.0.0.1:17679`. Container health requires the MCP server to answer and, when tunnel mode is enabled, the tunnel client to report ready. Unexpected exit of either child stops the sibling so the container restart policy can recover the whole gateway consistently.

The initial local-validation mode may leave the tunnel client disabled while the old tunnel remains connected. Enable the new tunnel client only after the isolated gateway passes same-state MCP checks; never connect two tunnel clients to the same tunnel ID simultaneously.

## MCP Events maturity

The OpenAI MCP Events extension remains a draft prototype. It advertises only `hermes.test`; it does not emit Mission/session/job notifications and does not provide a durable outbox or production delivery guarantee. The event callback uses the existing MCP authentication boundary, HTTPS-only destinations, globally routable IP validation, and Standard Webhooks signing. Do not treat a tool listing or an event subscription as proof that work completed.

## Deployment and rollback

Coolify's durable Compose source is the service's `docker_compose_raw` row in Postgres; the generated on-disk YAML is regenerated and is not the source of truth. Back up the raw row before editing, validate the candidate in isolation, regenerate, then start/recreate only `hermes-mcp-gateway`. Read the resulting mounts and namespaces from the live container before running tests.

Keep the prior production MCP/tunnel path and the existing staging server/client available until the new gateway passes local same-session checks and a normal MCP call through ChatGPT. For rollback, stop only `hermes-mcp-gateway`, restore the saved Coolify raw Compose row, regenerate the service Compose, and reconnect the preserved old tunnel client to its prior backend. Do not delete or replace the shared Hermes home/workspace volumes; do not restart Hermes Agent, Telegram, WebUI, or the bridge as part of gateway rollback. Retain the staging files and original Compose backup until the new route has remained healthy and the rollback window is closed.

Deployment-specific identifiers, exact volume backing paths, image digest, source branch/commit, tunnel ID, secret locations (names/paths only), verification evidence, and rollback commands belong in the private Vault session note rather than this generic guide.
