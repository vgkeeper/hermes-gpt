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

Hermes implements the draft OpenAI MCP Events extension for protocol
`2026-07-28` alongside core MCP. It adds `server/discover` with
`capabilities.events`, `events/list`, `events/subscribe`, and
`events/unsubscribe`, using verified Standard Webhooks callbacks. It is separate
from core `subscriptions/listen`; see [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)
and its [draft design sketch](https://github.com/modelcontextprotocol/experimental-ext-triggers-events/blob/main/docs/design-sketch-proposal.md).

The only advertised event is `hermes.live_event`, a wake-up projection of the
Live Events journal. Optional filters are `mission_id`, `topic`, and `kind`;
payloads contain event identity, cursor, bounded event metadata, and the
redacted Live Events payload. A callback is a notification, not evidence that a
Mission or job succeeded. Consumers must re-read authoritative durable state
and validate its Work Contract before acting on a completion claim.

Live Events is the only durable business event journal. The OpenAI Events
adapter stores subscription credentials and bounded subscription state
(cursor, truncation flag, lease, expiry, and callback-verification cache) in
`${HERMES_HOME:-~/.hermes}/mcp-events/subscriptions.sqlite3`. It does not keep a
second event or delivery journal, and callback responses are never republished
into Live Events. Subscription state uses a 0700 directory and 0600 database
where supported. The `whsec_` callback secret is retained for restart-safe
projection, so protect the runtime home and backups. Default lifetime is 24
hours; `ttlMs` is bounded to 60 seconds–30 days; `ttlMs: null` does not expire.

A new subscription starts at the current Live Events high watermark unless a
cursor is supplied. Decimal string/integer cursors are bounded by the journal;
re-subscribing to the same identity never moves its durable cursor backwards.
If retention removed requested events, the returned `truncated` flag is set and
projection resumes from the oldest retained event. The projector checkpoints
after each successful callback. Delivery is at-least-once across a crash
between callback acceptance and checkpoint; retries reuse a stable webhook
message ID so receivers can deduplicate. Failed delivery leaves the cursor at
the last accepted event and is retried with bounded backoff. The journal remains
the source of truth; the cursor is projection state, not proof of completion.

The extension is intercepted only on the authenticated `/mcp` Streamable HTTP
endpoint for SDK 2.x modern requests. SDK 1.x rejects
`MCP-Protocol-Version: 2026-07-28` before middleware can intercept it, so the
real-HTTP modern Events test explicitly skips on SDK 1.x. Legacy initialize and
`tools/list` continue on both SDK 1.x and 2.x. Stdio, SSE, legacy protocol
requests, and core tools pass through unchanged. The middleware bounds request
inspection to 256 KiB and replays larger requests unchanged to core MCP;
oversized draft Events requests are unsupported. The extension adds no
Operator tools and does not alter authority gates.

Outbound callbacks require HTTPS, resolve only globally routable IPs, connect
to the checked IP while retaining TLS hostname verification, and do not follow
redirects. Subscription identity is a one-way hash of the authenticated
Authorization header (or a shared anonymous local principal when authentication
is not configured); it is not a stable account identifier. Do not treat
subscription identity or callbacks as authorization or completion evidence.


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
