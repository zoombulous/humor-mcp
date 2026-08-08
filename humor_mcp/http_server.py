#!/usr/bin/env python3
"""Streamable-HTTP transport for humor-mcp — the hosted, public flavor.

server.py speaks to one user on their own machine over stdio. This speaks to
anyone on the internet, so the transport is where the security posture lives:

  - the corpus is whatever HUMOR_DB points at, opened read-only by server.py.
    A public deployment builds that db from a redistributable pack alone
    (HUMOR_PACKS at build time), so restricted material is not merely
    filtered out of results — it is not in the file at all.
  - per-client token-bucket rate limit. Behind a reverse proxy or Cloudflare
    tunnel the peer address is the proxy, so CF-Connecting-IP is honored by
    default; set HUMOR_HTTP_TRUST_PROXY=0 when the port faces clients
    directly, or one caller could spoof the header and dodge the bucket.
  - a GLOBAL bucket underneath the per-client one, because per-client is not
    the box's protector. Hosted Claude clients egress from a handful of shared
    Anthropic addresses, so one per-IP bucket covers many unrelated guests:
    tightening it punishes the innocent, and no arrangement of it caps what
    the box as a whole is asked to do. The global bucket is that cap. Clients
    are charged their own bucket first, so one hammering caller is turned away
    at its own limit without spending the budget everyone else shares.
  - optional bearer keys, for raising a known caller's limit — never for
    admission. An unknown key is served exactly as an anonymous caller is
    (no 401), because the corpus is public; a key buys headroom, not access.
  - request bodies capped, sockets timed out, JSON-RPC only. No sessions, no
    cookies, no auth to steal: the corpus is public and every tool is
    read-only, so the only thing worth guarding is the box's own resources.
  - ONE lock around dispatch. server.py keeps a single sqlite connection,
    which is thread-bound, and every query is single-digit milliseconds
    against a corpus of a few MB — serializing is simpler and safer than
    per-thread connections, and at demo traffic it is not the bottleneck.

Endpoint layout:
  POST /mcp      JSON-RPC over Streamable HTTP (/mcp/ works too — elsewhere
                 on this box the trailing slash has been load-bearing, so
                 neither spelling should be the one that breaks)
  GET  /         what this is and how to add it to Claude
  GET  /healthz  liveness, for monitoring
"""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import server as core

MAX_BODY = int(os.environ.get("HUMOR_HTTP_MAX_BODY", 65536))
RATE_MIN = float(os.environ.get("HUMOR_HTTP_RATE", 120))     # requests/min/client
BURST = float(os.environ.get("HUMOR_HTTP_BURST", 40))
KEY_RATE = float(os.environ.get("HUMOR_HTTP_KEY_RATE", 240))    # a keyed caller
KEY_BURST = float(os.environ.get("HUMOR_HTTP_KEY_BURST", 60))
GLOBAL_RATE = float(os.environ.get("HUMOR_HTTP_GLOBAL_RATE", 600))   # the box
GLOBAL_BURST = float(os.environ.get("HUMOR_HTTP_GLOBAL_BURST", 100))
KEYS_FILE = os.environ.get("HUMOR_HTTP_KEYS", "")
IDLE_TIMEOUT = float(os.environ.get("HUMOR_HTTP_IDLE_TIMEOUT", 30))
TRUST_PROXY = os.environ.get("HUMOR_HTTP_TRUST_PROXY", "1").lower() not in ("0", "false", "")

# The stdio server echoes whatever protocolVersion the client offers, which is
# fine one-on-one. On a public endpoint, claiming to support a version we have
# never seen is a lie waiting to matter; offer our newest known instead.
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL = KNOWN_PROTOCOLS[-1]

MCP_PATHS = ("/mcp", "/mcp/")

_DISPATCH_LOCK = threading.Lock()

# ------------------------------------------------------------- rate limiting
_RL_LOCK = threading.Lock()
_buckets = {}                     # ("ip"|"key", who) -> [tokens, last_refill]
_global = [GLOBAL_BURST, time.monotonic()]


def _spend(bucket, rate_min, burst, now):
    """Refill by elapsed time, then take one token. True if there was one."""
    bucket[0] = min(burst, bucket[0] + (now - bucket[1]) * (rate_min / 60.0))
    bucket[1] = now
    if bucket[0] >= 1.0:
        bucket[0] -= 1.0
        return True
    return False


def _allow(scope, rate_min, burst):
    """Charge one request to a single client's bucket."""
    now = time.monotonic()
    with _RL_LOCK:
        # An attacker cycling source addresses would otherwise grow this dict
        # without bound. Dropping every bucket is crude but sound: the cost of
        # a reset is one burst per client, the cost of no cap is the box's RAM.
        # The global bucket is deliberately NOT in here — evicting it would
        # hand exactly that attacker a full reset of the box's own budget.
        if len(_buckets) > 10000:
            _buckets.clear()
        return _spend(_buckets.setdefault(scope, [burst, now]),
                      rate_min, burst, now)


def _allow_global():
    with _RL_LOCK:
        return _spend(_global, GLOBAL_RATE, GLOBAL_BURST, time.monotonic())


# ------------------------------------------------------------------ api keys
# `key_id:secret[:rate_per_min[:burst]]` per line, `#` starts a comment. A key
# is a rate tier, not a credential: it is looked up to decide how much headroom
# the caller gets, and a miss simply means anonymous limits.
_KEYS_LOCK = threading.Lock()
_keys = {}                        # secret -> (key_id, rate, burst)
_keys_stamp = None                # (mtime, size) of the file we parsed
_keys_next_check = 0.0


def _parse_keys(text):
    out, bad = {}, 0
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            bad += 1
            continue
        try:
            rate = float(parts[2]) if len(parts) > 2 and parts[2] else KEY_RATE
            burst = float(parts[3]) if len(parts) > 3 and parts[3] else KEY_BURST
        except ValueError:
            bad += 1
            continue
        out[parts[1]] = (parts[0], rate, burst)
    return out, bad


def _load_keys():
    """Re-read the key file when it changes, so handing someone a key does not
    need a restart (which would drop every connection in flight). Checked at
    most once every few seconds; a missing or unreadable file means no keys."""
    global _keys, _keys_stamp, _keys_next_check
    if not KEYS_FILE:
        return
    now = time.monotonic()
    with _KEYS_LOCK:
        if now < _keys_next_check:
            return
        _keys_next_check = now + 5.0
        try:
            st = os.stat(KEYS_FILE)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            if _keys_stamp is not None:
                print(f"humor-mcp: key file {KEYS_FILE} is gone; "
                      "every caller is anonymous now", file=sys.stderr)
            _keys, _keys_stamp = {}, None
            return
        if stamp == _keys_stamp:
            return
        try:
            with open(KEYS_FILE, encoding="utf-8") as fh:
                parsed, bad = _parse_keys(fh.read())
        except OSError as e:
            print(f"humor-mcp: cannot read key file {KEYS_FILE}: {e}",
                  file=sys.stderr)
            return
        _keys, _keys_stamp = parsed, stamp
        note = f", {bad} unparseable line(s) ignored" if bad else ""
        print(f"humor-mcp: loaded {len(parsed)} key(s){note}", file=sys.stderr)


def _tier(auth_header):
    """(key_id, rate, burst) for this caller. Anonymous unless a bearer token
    matches a loaded key — an unknown key is not an error, just no headroom."""
    _load_keys()
    if auth_header:
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            with _KEYS_LOCK:
                hit = _keys.get(token.strip())
            if hit:
                return hit
    return ("anon", RATE_MIN, BURST)


# ------------------------------------------------------------------ JSON-RPC
def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _rpc(msg):
    """One JSON-RPC message in, one response dict out — or None to say nothing
    (notifications, and the client's own responses echoed back at us)."""
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "invalid request")
    rid = msg.get("id")
    if "method" not in msg:
        return None                       # a response object; nothing to do
    if msg.get("method") == "initialize":
        pv = ((msg.get("params") or {}).get("protocolVersion"))
        if pv not in KNOWN_PROTOCOLS:
            msg.setdefault("params", {})["protocolVersion"] = LATEST_PROTOCOL
    try:
        with _DISPATCH_LOCK:
            res = core.handle(msg)
    except Exception as e:                # noqa: BLE001 - fault must not kill the worker
        return _rpc_error(rid, -32603, f"{type(e).__name__}: {e}") if rid is not None else None
    if rid is None:
        return None                       # notification, answered by silence
    if res is None:
        return _rpc_error(rid, -32601, f"method not found: {msg.get('method')}")
    return {"jsonrpc": "2.0", "id": rid, "result": res}


INFO_PAGE = """\
humor-mcp — a humor corpus as a remote MCP server.

James Barker's own material (the `mallard` pack, CC-BY-4.0): human-rated
setup/response jokes, best-of-N slate winners with their setups, and the
chosen-vs-rejected preference pairs behind them. Every result carries its
credit.

Add it to Claude (any plan, including Free):
  Settings -> Connectors -> Add custom connector -> paste {endpoint}

Tools: search_humor, top_rated, taste_profile, breakdown, preference_pairs,
sources, style_pack, corpus_stats. All read-only.

Code and corpus: https://github.com/zoombulous/humor-mcp
"""

ENDPOINT_NOTE = """\
This IS the MCP endpoint, and it is alive — it speaks JSON-RPC over POST,
which is why a browser visit shows this page instead of anything happening.

"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "humor-mcp"
    sys_version = ""
    timeout = IDLE_TIMEOUT                # shed connections that stop talking

    def setup(self):
        super().setup()
        # Handler instances are reused across a keep-alive connection, so both
        # of these are per-connection state, reset per request in parse_request.
        self._served = 0
        self._own_log = False
        self._head = False

    def parse_request(self):
        self._own_log = False
        return super().parse_request()

    # -------------------------------------------------------------- plumbing
    def _client_ip(self):
        # `headers` does not exist until a request line has been parsed, and
        # this runs from the log path — which fires for a connection that
        # opened and never said anything, the shape every port scanner has.
        # Dereferencing it blind turned that into an AttributeError traceback
        # inside socketserver, burying the one line worth reading.
        hdrs = getattr(self, "headers", None)
        if TRUST_PROXY and hdrs:
            fwd = hdrs.get("CF-Connecting-IP") or hdrs.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _cors(self):
        # The corpus is public and read-only; a browser-based MCP client (the
        # inspector, say) is a legitimate caller, not a threat model.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id, "
                         "MCP-Protocol-Version, Last-Event-ID")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reply(self, status, body=b"", ctype="application/json; charset=utf-8",
               extra=()):
        self.send_response(status)
        self._cors()
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._served += 1
        # A HEAD answer carries the headers a GET would, and no body.
        if body and not self._head:
            self.wfile.write(body)

    def _text(self, status, text, extra=()):
        self._reply(status, text.encode("utf-8"), "text/plain; charset=utf-8", extra)

    def log_message(self, fmt, *args):
        # One terse line per request, stderr -> journald. The default logger
        # prints the proxy's address; the client behind it is the useful one.
        print(f"{self._client_ip()} {fmt % args}", file=sys.stderr)

    def log_request(self, code="-", size="-"):
        # MCP calls log their own, richer line; anything else keeps the default.
        if not self._own_log:
            super().log_request(code, size)

    def log_error(self, fmt, *args):
        # The base class reports the idle-socket reap as "Request timed out",
        # which on a keep-alive connection is just the client having finished:
        # every one of those lines lands exactly `timeout` seconds after a
        # perfectly good response. Suppressing them is what keeps a real
        # timeout visible. A connection that never sent a request at all still
        # gets a line — that one is worth seeing.
        if fmt.startswith("Request timed out") and self._served:
            return
        self.log_message(fmt, *args)

    # -------------------------------------------------------------- methods
    def do_OPTIONS(self):
        self._reply(204)

    def _endpoint_url(self):
        """The URL the viewer is actually at, as Claude should be given it.
        Behind the tunnel that is https://<host>/mcp; the Host header knows the
        hostname and the proxy header knows the scheme."""
        host = self.headers.get("Host") or "this-host"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}/mcp"

    def do_GET(self):
        if self.path == "/healthz":
            self._text(200, "ok\n")
        elif self.path in MCP_PATHS:
            if "text/event-stream" in (self.headers.get("Accept") or ""):
                # A real MCP client opening the server-push channel. Streamable
                # HTTP allows declining it outright; with no server-initiated
                # messages there is nothing a stream would ever carry.
                self._text(405, "POST JSON-RPC here; there is no event stream\n",
                           extra=(("Allow", "POST, OPTIONS"),))
            else:
                # A human checking the URL before pasting it into Claude — the
                # single most predictable visitor a public endpoint has. Telling
                # them "no" reads as "broken"; tell them what they are looking
                # at instead.
                self._text(200, ENDPOINT_NOTE +
                           INFO_PAGE.format(endpoint=self._endpoint_url()))
        elif self.path == "/":
            self._text(200, INFO_PAGE.format(endpoint=self._endpoint_url()))
        else:
            self._text(404, "not found\n")

    def do_HEAD(self):
        # Link-preview bots and uptime checkers send HEAD. The stdlib's answer
        # is a 501 error page, which reads as a broken endpoint; give them the
        # status and headers GET would, with no body.
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_DELETE(self):
        # Session teardown, in a server that never issues a session.
        self._text(405, "stateless server: no session to delete\n",
                   extra=(("Allow", "POST, OPTIONS"),))

    @staticmethod
    def _called_tools(payload):
        """The tool names in this request, for the access log. Names only —
        arguments can carry a guest's own prompt context and are never logged."""
        msgs = payload if isinstance(payload, list) else [payload]
        names = []
        for m in msgs:
            if isinstance(m, dict) and m.get("method") == "tools/call":
                n = (m.get("params") or {}).get("name")
                if isinstance(n, str) and n not in names:
                    names.append(n[:40])
        return names

    @staticmethod
    def _rpc_methods(payload):
        msgs = payload if isinstance(payload, list) else [payload]
        out = []
        for m in msgs:
            if isinstance(m, dict) and isinstance(m.get("method"), str):
                if m["method"] not in out:
                    out.append(m["method"][:40])
        return out

    def _access(self, key_id, status, started, payload=None):
        """The one structured line per MCP request: who, what tool, how long,
        what came back. No bodies, no query text."""
        ms = int((time.monotonic() - started) * 1000)
        methods = self._rpc_methods(payload) if payload is not None else []
        tools = self._called_tools(payload) if payload is not None else []
        parts = [f"key={key_id}",
                 f"rpc={','.join(methods) or '-'}",
                 f"tool={','.join(tools) or '-'}",
                 f"status={status}", f"ms={ms}"]
        self.log_message("mcp %s", " ".join(parts))

    def do_POST(self):
        if self.path not in MCP_PATHS:
            self._text(404, "not found\n")
            return
        started = time.monotonic()
        self._own_log = True
        key_id, rate, burst = _tier(self.headers.get("Authorization"))

        # The caller's own bucket first, the box's second. Charging the client
        # first means a hammering caller is turned away at its own limit
        # without ever spending from the budget every other guest shares.
        scope = ("key", key_id) if key_id != "anon" else ("ip", self._client_ip())
        if not _allow(scope, rate, burst):
            self._text(429, "rate limited\n", extra=(("Retry-After", "10"),))
            self._access(key_id, 429, started)
            return
        if not _allow_global():
            self._text(503, "server busy\n", extra=(("Retry-After", "5"),))
            self._access(key_id, 503, started)
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._text(411, "Content-Length required\n")
            self._access(key_id, 411, started)
            return
        if length > MAX_BODY:
            self._text(413, f"body over {MAX_BODY} bytes\n")
            self._access(key_id, 413, started)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except Exception:
            body = json.dumps(_rpc_error(None, -32700, "parse error")).encode()
            self._reply(400, body)
            self._access(key_id, 400, started)
            return

        if isinstance(payload, list):     # pre-2025-06-18 clients may batch
            if not payload:
                self._reply(400, json.dumps(
                    _rpc_error(None, -32600, "empty batch")).encode())
                self._access(key_id, 400, started, payload)
                return
            out = [r for r in (_rpc(m) for m in payload) if r is not None]
        else:
            out = [r for r in (_rpc(payload),) if r is not None]

        if not out:
            self._reply(202)              # notifications: accepted, no body
            self._access(key_id, 202, started, payload)
            return
        body = json.dumps(out if isinstance(payload, list) else out[0],
                          ensure_ascii=False).encode("utf-8")
        self._reply(200, body)
        self._access(key_id, 200, started, payload)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="humor-mcp serve-http",
        description="Serve the corpus over Streamable HTTP (MCP remote transport).")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1 — put a tunnel or "
                         "reverse proxy in front rather than binding wide)")
    ap.add_argument("--port", type=int, default=8526)
    args = ap.parse_args(argv)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"humor-mcp: Streamable HTTP on http://{args.host}:{args.port}/mcp "
          f"(db {core.DB_PATH})", file=sys.stderr)
    # The limits in force, printed once, so the journal says what the running
    # process actually believes rather than what a unit file used to say.
    print(f"humor-mcp: limits anon {RATE_MIN:g}/min burst {BURST:g} · "
          f"keyed {KEY_RATE:g}/min burst {KEY_BURST:g} · "
          f"global {GLOBAL_RATE:g}/min burst {GLOBAL_BURST:g} · "
          f"keys {KEYS_FILE or 'none'}", file=sys.stderr)
    _load_keys()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
