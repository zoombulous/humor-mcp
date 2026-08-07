# GUEST ACCESS RESEARCH (humor MCP)

Research digest (2026-08-07), commissioned for the public "mallard humor corpus" demo:
guest access + abuse limits for an MCP that strangers add to their own Claude.
Read before building the public endpoint.

**Code verification first:** the serving path is clean. `humor_mcp/server.py` is
stdlib-only (json/re/sqlite3), opens the DB read-only (`mode=ro`, line 59), and no
tool touches the network — grep for urllib/requests/httpx/socket across `humor_mcp/`
hits only comments. `import_audio.py`/`audio_reactions.py` are CLI build-time
subcommands, unreachable from the tool surface. **No tool can trigger an upstream
paid API call — there is no spend to cap; design for availability, not dollars.**
Also: `http_server.py` already exists and already has a per-IP token bucket (120/min,
burst 40), 64KB body cap, 30s socket timeout, CF-Connecting-IP handling, and a
stateless streamable-HTTP endpoint — the work remaining is smaller than assumed.

## 1. Auth: what Claude clients actually support

- **claude.ai custom connectors** (Free/Pro/Max/Team/Enterprise; Free = 1 connector):
  the add-connector flow takes a bare URL; OAuth client id/secret is *optional*
  "Advanced settings". Unauthenticated servers work — this is exactly how DeepWiki
  and Microsoft Learn are added. The server must simply not return 401. Must be
  reachable from Anthropic's IP ranges.
  ([support.claude.com](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp))
- **Static bearer/API-key headers on claude.ai**: a "Request headers" field
  (authorization / x-api-key / x-auth-token) exists but is **beta, slow rollout**
  ([docs](https://claude.com/docs/connectors/building/authentication)); long-standing
  gaps tracked in [claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)
  and [#402](https://github.com/anthropics/claude-ai-mcp/issues/402). Requiring a key
  would break the primary audience today.
- **Claude Code**: `claude mcp add --transport http humor https://barker-ai.net/mcp/humor
  --header "Authorization: Bearer KEY"` works fine
  ([code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp)).
- **MCP spec**: OAuth 2.1 is the standard *when auth is required* (401 + resource
  metadata triggers the flow); auth itself is optional for HTTP servers
  ([spec authorization](https://modelcontextprotocol.info/specification/draft/basic/authorization/),
  [Stack Overflow blog](https://stackoverflow.blog/2026/01/21/is-that-allowed-authentication-and-authorization-in-model-context-protocol/)).
  Implementing OAuth for a hobby demo is heavy and adds an OAuth server to maintain.
- **Verdict**: **unauthenticated-by-default with hard limits** (the DeepWiki model),
  plus **optional bearer keys that raise limits** for Claude Code users / friends.
  Never return 401 to anonymous traffic (401 triggers OAuth discovery and the
  connector add fails); treat a bad key as anonymous.

## 2. Rate limiting & abuse controls, layered

**Cloudflare free tier**
([rate limiting docs](https://developers.cloudflare.com/waf/rate-limiting-rules/)):
1 rate-limiting rule, 10s window, **IP-based only**, 10s block, no custom counting
expressions. 5 WAF custom rules. **Bot Fight Mode: leave OFF** — it challenges
non-browser clients, cannot be skipped by WAF rules on free (separate evaluation
pipeline), and would break MCP clients
([community](https://community.cloudflare.com/t/bot-fight-mode-ignores-waf-skip-rule-and-blocks-curl-api-requests/916872),
[docs](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/)).
Turnstile is browser-only — irrelevant for MCP traffic (its one valid use: gating a
future key-signup page). Free DDoS mitigation stays on regardless and is the real
backstop.

**Box-side**: in-app limiting beats nginx here — the app already parses JSON-RPC so
it can limit *per key* and *per tool*, which nginx can't. The existing bucket in
`http_server.py` is the right shape; it needs a **global** bucket added (per-IP
alone fails — see below) and key tiers.

**Is IP/device gating meaningful?** Mostly no. Critical catch: **claude.ai connector
traffic egresses from Anthropic's shared IP ranges** — all claude.ai guests can land
on a handful of IPs, so per-IP limits lump them together (and per-IP limits punish
the innocent, not the attacker). Device-type is spoofable noise. Correct design:
per-IP limits as a coarse anti-hammer layer (generous), **global req/min + global
concurrency as the actual box protector**, keys for anyone needing more.

## 3. Precedents

- **DeepWiki MCP** (`mcp.deepwiki.com`) — public, zero auth, no signup, no published
  limits; throttles heavy automation
  ([guide](https://mcp.directory/blog/deepwiki-mcp-complete-guide-2026),
  [Devin docs](https://docs.devin.ai/work-with-devin/deepwiki-mcp)).
- **Microsoft Learn MCP** (`learn.microsoft.com/api/mcp`) — public, no auth, no
  published limits ([repo](https://github.com/microsoftdocs/mcp)).
- **Glama hosting** sells per-user rate limits + JSON-RPC-aware monitoring as the
  managed version of exactly this ([glama.ai/mcp/hosting](https://glama.ai/mcp/hosting));
  Smithery wraps hosted servers in its own OAuth.
- Nobody publishes numbers; the norm for read-only public corpus MCPs is
  **anonymous + silent throttling**. Zuplo's rule of thumb: agents retry in tight
  loops, so never ship without a limit
  ([zuplo](https://zuplo.com/blog/never-ship-mcp-server-without-rate-limit)).
- **Actual abuse seen**: mass internet scanning of MCP endpoints (Trend Micro found
  ~1,500–1,900 exposed servers; scanners probe for unauthenticated *dangerous*
  tools — file/DB/exec)
  ([Trend Micro](https://www.trendmicro.com/vinfo/us/security/news/vulnerabilities-and-exploits/update-on-exposed-mcp-servers-the-threat-widens-to-the-cloud)).
  A read-only public-corpus server's exposure is resource exhaustion only — which is
  what the limits are for. Being scanned is certain; being exploited requires a tool
  surface this server doesn't have.

## 4. Failure containment on the shared box

- **Separate systemd unit** (`humor-mcp-public.service`), never sharing the Ella
  units. Suggested: `CPUQuota=25%`, `MemoryMax=256M` (corpus is a few MB; stdlib
  server needs ~30MB), `TasksMax=64`, `Nice=10`, plus hardening (`DynamicUser=yes`,
  `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, `PrivateTmp=yes`,
  `ReadOnlyPaths=/srv/humor-public`). OOM-kill of this unit then can't touch
  Ella/chipchip/postgres.
- **Dedicated public DB**: build a mallard-only `humor.db` into `/srv/humor-public/`
  — `paths.py` already supports this exactly (`HUMOR_PACKS=<repo>/humor_mcp/packs`,
  `HUMOR_DB=/srv/humor-public/humor.db`; matches the existing memory warning that
  out-of-checkout builds must set HUMOR_DB). Restricted packs are then *absent from
  the file*, not filtered — `http_server.py`'s stated design (lines 7–11). Server
  opens it `mode=ro`.
- **Kill switches, three depths**: (1) box: `systemctl stop humor-mcp-public` —
  tunnel returns 502, nothing else affected; (2) Cloudflare: a pre-created WAF
  custom rule "block URI path starts with `/mcp/humor`" left **disabled** — enabling
  it is one toggle at the edge (uses 1 of 5 free rules); (3) cloudflared: delete the
  ingress entry + restart the tunnel unit. Document (1) and (2) as THE switches.
- Remember `svc-audit` before starting anything new on the box (existing house
  rule), and register the unit reboot-safe.

## 5. Observability (minimal)

- One structured line per `tools/call` to stderr→journald:
  `ts key_id(anon) tool duration_ms status ip`. That's the whole privacy surface —
  no bodies, no query text (queries could contain a guest's prompt context; don't
  keep them).
- Daily rollup: `journalctl -u humor-mcp-public --since -24h | grep ...` in the
  existing morning briefing cron, counting calls by tool and by key, plus 429 count.
- Alerting: the box already runs Prometheus+Grafana — alert on the *unit's*
  CPU/memory (systemd cgroup metrics via node_exporter) and on sustained 429s; no
  new stack needed. `/healthz` already exists for liveness.

## RECOMMENDED ARCHITECTURE

**Auth**: anonymous allowed (tight limits) + optional `Authorization: Bearer <key>`
for elevated limits. Keys = random strings in `/srv/humor-public/keys.txt`
(`key_id:key:tier`), handed out manually (DM a friend a key); no signup page in v1.
Invalid key → treated as anonymous, never 401.

**Starting limits** (env-tunable, all already or newly in `http_server.py`):

| layer | limit |
|---|---|
| Cloudflare rate rule (the 1 free rule) | 30 req/10s per IP on `/mcp/humor*` → block 10s |
| box per-IP (anon) | 60 req/min, burst 20 (lower the current 120/40) |
| box per-key | 240 req/min, burst 60 |
| box global | 600 req/min, burst 100 — the box protector |
| concurrency | dispatch lock already serializes SQLite (effective concurrency 1); keep |
| body / socket | 64KB / 30s — already present |

**Cloudflare**: rate rule above; WAF custom rules: (a) disabled kill rule for
`/mcp/humor*`, (b) optionally block non-POST/GET/OPTIONS methods on the path.
Bot Fight Mode OFF. Proxy (orange-cloud) on, so the origin IP stays hidden and free
DDoS applies.

**Plumbing**: cloudflared ingress `barker-ai.net/mcp/humor` (and `/mcp/humor/`,
trailing slash is load-bearing on this box) → `127.0.0.1:8526`.
`HUMOR_HTTP_TRUST_PROXY=1` (default) is correct behind the tunnel.

**What the code needs added** (all in `humor_mcp/http_server.py`):
1. Bearer-key parsing + key file loading + per-key buckets and tiered rates
   alongside the existing `_allow(ip)` (~50 lines; generalize `_buckets` to key on
   `("ip", ip)` / `("key", key_id)` / `("global",)`).
2. Global bucket check in `do_POST` before dispatch (~10 lines).
3. Access-log line in `do_POST` capturing tool name (`payload["params"]["name"]`
   when method is `tools/call`), key_id, duration (~15 lines).
4. Nothing in `server.py` — its clamps (`MAX_LIMIT=200`), read-only connection, and
   loud unknown-pack errors are already public-safe.
5. Ops files (not code): `humor-mcp-public.service`, one-time public DB build
   command, two Cloudflare rules.

**Effort**: ~half a day. 2–3h for the three http_server.py changes + tests (extend
`test_http.py`), 1–2h box-side (build public DB, unit, ingress, svc-audit), 30min
Cloudflare rules, ~1h end-to-end verification adding it to a real claude.ai account
(Free-plan account ideally, since that's the guest experience) and via
`claude mcp add`.

**Sources**: [claude.ai custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) ·
[connector auth / request-headers beta](https://claude.com/docs/connectors/building/authentication) ·
[claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112) ·
[#402](https://github.com/anthropics/claude-ai-mcp/issues/402) ·
[Claude Code MCP](https://code.claude.com/docs/en/mcp) ·
[MCP authorization spec](https://modelcontextprotocol.info/specification/draft/basic/authorization/) ·
[SO blog on MCP auth](https://stackoverflow.blog/2026/01/21/is-that-allowed-authentication-and-authorization-in-model-context-protocol/) ·
[CF rate limiting](https://developers.cloudflare.com/waf/rate-limiting-rules/) ·
[CF Bot Fight Mode](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/) ·
[BFM can't be skipped](https://community.cloudflare.com/t/bot-fight-mode-ignores-waf-skip-rule-and-blocks-curl-api-requests/916872) ·
[DeepWiki MCP](https://mcp.directory/blog/deepwiki-mcp-complete-guide-2026) ·
[MS Learn MCP](https://github.com/microsoftdocs/mcp) ·
[Glama hosting](https://glama.ai/mcp/hosting) ·
[Zuplo on MCP rate limits](https://zuplo.com/blog/never-ship-mcp-server-without-rate-limit) ·
[Trend Micro exposed-MCP research](https://www.trendmicro.com/vinfo/us/security/news/vulnerabilities-and-exploits/update-on-exposed-mcp-servers-the-threat-widens-to-the-cloud)
