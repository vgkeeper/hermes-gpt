# MCP Compatibility

- Status: current source (SDK 1/2 compatibility; not a release announcement)

This manifest describes the MCP surface exposed by both the main server and
curated Codex server. Package support is `mcp[cli]>=1.28.1,<3`. SDK 1.x remains
supported; installations may select SDK 2.x without changing Hermes source.

## Protocol compatibility

The SDK package version and negotiated MCP protocol revision are separate.
The compatibility tests perform real HTTP `initialize` requests for legacy
revisions **2024-11-05** and **2025-11-25**, checking the exact negotiated
revision and Hermes GPT application version on both server surfaces. The
existing subprocess stdio test also exercises **2025-06-18**.

SDK 2 introduces the **2026-07-28** stateless protocol while retaining legacy
client support. New-protocol clients do not use the legacy initialization
handshake. These are SDK transport semantics, not a change to Hermes Operator
authority. See the [SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/).

### OpenAI MCP Events extension

Hermes implements a minimal prototype of OpenAI's draft MCP Events extension
for protocol `2026-07-28` alongside the core MCP transport. This extension is
separate from core `subscriptions/listen`; it adds `server/discover` with
`capabilities.events`, `events/list`, `events/subscribe`, and
`events/unsubscribe`, and uses verified Standard Webhooks callbacks. The source
contract is [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)
and its linked [draft design sketch](https://github.com/modelcontextprotocol/experimental-ext-triggers-events/blob/main/docs/design-sketch-proposal.md).

The prototype exposes only `hermes.test` (`test_id` filter and `test_id` /
`message` payload), on the same authenticated `/mcp` endpoint, and only for
SDK 2.x Streamable HTTP modern protocol requests. SDK 1.x, legacy initialize,
stdio, SSE and existing `tools/*` remain on their existing paths. The modern
custom discovery response advertises tools plus events; it does not provide
resources/prompts discovery.

The middleware buffers at most 256 KiB while identifying a request method.
Larger requests are replayed to the core MCP transport unchanged; oversized
draft Events requests are therefore unsupported, while ordinary large tool
requests retain the existing transport path. This extension does not add
Operator tools or change Operator authority.

Subscriptions persist in `${HERMES_HOME:-~/.hermes}/mcp-events/subscriptions.sqlite3`
(SQLite, directory mode 0700 and database mode 0600 where supported). Rows
contain deterministic ID, hashed bearer principal, event arguments, callback
URL, `whsec_` secret and expiration. The signing secret is stored as required
for restart-safe delivery; protect the runtime home and backups accordingly.
Default lifetime is 24 hours; supplied `ttlMs` is bounded to 60 seconds–30 days;
`ttlMs: null` grants a non-expiring subscription. This non-replayable event
returns `cursor: null`. Internal emission entry point is
`operator_mcp_events.emit_test(test_id, message)`; it is intentionally not
registered as a network MCP tool or route.

Outbound callback requests require HTTPS, resolve only globally-routable IPs,
connect to the checked IP while retaining TLS hostname verification, and do
not follow redirects. This prototype has bounded synchronous retries and no
persistent delivery queue: failed callbacks can be lost after the emitter
returns/process shutdown. It is suitable for an isolated ChatGPT Work test, not
production delivery guarantees. Callback URLs must be publicly reachable HTTPS.

The OpenAI guide provides the exact ChatGPT Work setup and lifecycle test:
connect/scan the plugin, use a Work chat (web or desktop Cloud), request a
subscription, confirm callback verification, call the internal emitter with a
matching `test_id`, verify ChatGPT receives/responds, then stop monitoring and
confirm unsubscribe. This remains an experimental, test-only event surface, not
a production event source. Deployment status is environment-specific and is
not implied by this source document; review the delivery and identity limits
before relying on subscriptions.

#### Staged ChatGPT Work test procedure

1. Deploy only to an isolated HTTPS-reachable staging instance after review;
   configure its existing MCP plugin connection and authentication. Keep the
   callback URL public HTTPS; do not log request bodies or `delivery.secret`.
2. In ChatGPT, connect/rescan the plugin so its tools and `hermes.test` appear.
   Start a Work chat on the web, or choose Work + Cloud in the desktop app.
3. Ask ChatGPT to monitor `hermes.test` with a unique `test_id` and state the
   expected response. Confirm the server receives `events/subscribe`, answers
   callback verification, and stores the subscription.
4. On the staging server, with the same runtime `HERMES_HOME`, emit locally:
   `python -c 'from operator_mcp_events import emit_test; print(emit_test("<test-id>", "MCP Events test wake-up"))'`
   Replace `<test-id>` with the exact filter chosen in ChatGPT. This is an
   in-process local API, not an HTTP endpoint or MCP tool.
5. Confirm delivery receives 2xx and ChatGPT responds in the subscribed Work
   chat according to the user instruction. Then ask ChatGPT to stop monitoring;
   verify `events/unsubscribe` and confirm no later matching event is delivered.
6. Remove the staging subscription/database when the experiment is complete;
   do not copy the callback signing secret into logs or source control.

OpenAI also recommends testing refresh after restart, expiry, duplicate
subscription/delivery, invalid signatures, revoked access and filtered-out
events. This prototype's test event does not support replay; deliveries use a
bounded in-process retry loop without a persistent outbox, so a process stop
can lose an event after it is emitted. The principal key is a one-way hash of
the authenticated Authorization header (or one shared local anonymous
principal when no auth is configured), not a stable account ID; OAuth credential
rotation can therefore create a distinct subscription identity. This must be
resolved before relying on long-lived account-scoped subscriptions.


The shared `mcp_compat.HermesMCP` adapter preserves explicit HTTP/SSE options:
SDK 1 accepts them at construction; SDK 2 accepts them at ASGI app creation.
Both retain JSON, stateless Streamable HTTP when started with `--http` and
the existing host/origin restrictions. SDK 2 uses its public app-version
parameter; only SDK 1 needs the legacy private version assignment.

## Transport matrix

| Transport | Path | Notes |
|---|---|---|
| stdio | — | Default local mode (`hermes-gpt` or `python server.py`) |
| Streamable HTTP | `/mcp` | Enabled with `--http`; transport security host/origin allowlist |
| Legacy SSE | `/sse` (plus `/messages/`) | Retained for older clients |

OpenAI Secure MCP Tunnel is an external private bridge, not a fourth Hermes GPT server transport. For the recommended Hermes setup, `tunnel-client` reaches `http://127.0.0.1:4750/mcp` locally over the existing Streamable HTTP transport and carries those MCP requests through an outbound-only OpenAI tunnel. See [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).

Server transport security (`TransportSecuritySettings`) enforces an explicit
host/origin allowlist: loopback by default plus `HERMES_GPT_ALLOWED_HOSTS`
extensions and the OAuth issuer when configured. Public unauthenticated
hosting is unsupported (product invariant).

## Trusted-client authentication metadata

Every tool advertises its security scheme via MCP tool metadata
(`securitySchemes`), driven by `server.tool_meta()`:

| Config | Advertised scheme |
|---|---|
| OAuth configured (`HERMES_GPT_OAUTH_*`) | `oauth2` with the configured scope |
| Static bearer (`HERMES_GPT_BEARER_TOKEN`) | `http` / `bearer` |
| Neither | `noauth` (loopback / trusted-proxy only) |

For Secure MCP Tunnel, the baseline local hop can remain loopback/noauth while OpenAI tunnel identity and Hermes Operator policy protect separate layers. Static bearer can be added as local-hop defense in depth. Built-in OAuth requires separate browser-facing authorization-server reachability because the authorization server itself is not automatically tunneled.

## Binary embedded tool results

`hermes_export_file` returns a direct MCP `CallToolResult` containing safe structured metadata and `EmbeddedResource(BlobResourceContents)` for authorized file bytes. This uses the normal `tools/call` response content union; it is not a new transport and does not require a separate resource-read endpoint.

The file-export surface requires Operator `workspace` authority plus a non-empty `HERMES_GPT_OPERATOR_ALLOWED_PATHS`; see [Binary file export](file-export.md) for the complete confinement, size, extension, denied-path, and audit contract.

The MCP specification leaves rendering of embedded resources to the client. Hermes GPT guarantees the protocol-native blob representation and does not claim that ChatGPT, Codex, or another client will always render it as a downloadable attachment. No text/base64 fallback is emitted.

## Version advertisement

The `initialize` handshake advertises the hermes-gpt app version in
`serverInfo.version` (from `versioning.VERSION`) — not the
MCP SDK version. This lets a client detect a stale process that is still
exposing an old schema. `test_mcp_compat.py::test_initialize_advertises_server_version`
asserts the handshake reports `versioning.VERSION` and that the pinned floor
(`2024-11-05`) remains negotiable on the running SDK.

Client notes:

- **ChatGPT (chatgpt.com connector)**: uses the OAuth metadata to drive the
  ChatGPT connector flow; loopback redirect required. For private developer-mode access without a public Hermes GPT hostname, see [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md).
- **Gemini Spark (consumer Custom apps)**: connects as a manually configured
  confidential client (Client ID and secret entered in the Gemini UI under
  "Advanced features → Show more") because the server advertises no
  `registration_endpoint`. Google's callback
  (`https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-<id>-<host-with-dots-as-underscores>`)
  must be allowlisted exactly — wildcards are not accepted, and the first
  attempt is rejected so the exact value can be read from the server's HTTP
  access log. PKCE S256 is supported; the OAuth boundary is streamable HTTP
  only (`--http`). See [Gemini Spark custom app](gemini-spark.md).
- **Codex CLI**: uses stdio or streamable HTTP with the configured scheme;
  curated tool names (`hermes_extract_page` vs `hermes_web_extract`) are
  documented in `docs/codex.md`.
- **Any client showing an old or incomplete tool list**: compare
  `serverInfo.version` against the expected release, then refresh the client's
  cached tool list. See [docs/updating.md](updating.md) for the check-first
  update and cache-refresh behavior; it is the canonical guide and is not
  duplicated here.

## Package floor and regression coverage

`pyproject.toml` and `requirements.txt` allow `mcp[cli]>=1.28.1,<3`.
The minimum 1.x version is the previously documented verified SDK, rather
than the historical untested `>=1.0` metadata floor. SDK 3 is not admitted.

CI runs both SDK families on Python 3.10, 3.11 and 3.12, plus pinned 1.28.1
and 2.0.0 floor jobs. Tests inspect serialized MCP field aliases, so SDK 2's
Python snake_case attributes do not alter the expected wire contract.
The matrix covers tool inventory, annotations, result schemas, binary export,
authentication and permission gates. Codex Operator aliases return redacted content blocks without an output schema:
some callbacks return JSON objects and others return plain skill text. Their
signature omits an output schema instead of promising the original
callback's string result.
Core tools returning typed dictionaries continue to provide structured content.

Tests read MCP results through the `wire()` helper in `conftest.py`, which
serializes a model by its protocol field names. Hermes builds results with
those same names (`isError`, `structuredContent`), so a test never depends on
whether the installed SDK spells the Python attribute `isError` or `is_error`.

## Checking the other SDK locally

CI covers both families, but a contributor can reproduce either one in a
throwaway environment before pushing:

```sh
python -m venv .venv-sdk2 && .venv-sdk2/bin/pip install -e ".[dev]" "mcp>=2,<3"
.venv-sdk2/bin/python -m pytest -q
```

Swap the specifier for `"mcp>=1.28.1,<2"` to check SDK 1. Two runs cover the
compatibility surface; the suite takes roughly a minute per run.
