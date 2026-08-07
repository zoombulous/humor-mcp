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
import argparse, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import server as core

MAX_BODY = int(os.environ.get("HUMOR_HTTP_MAX_BODY", 65536))
RATE_MIN = float(os.environ.get("HUMOR_HTTP_RATE", 120))     # requests/min/client
BURST = float(os.environ.get("HUMOR_HTTP_BURST", 40))
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
_buckets = {}                     # ip -> [tokens, last_refill]


def _allow(ip):
    now = time.monotonic()
    with _RL_LOCK:
        # An attacker cycling source addresses would otherwise grow this dict
        # without bound. Dropping every bucket is crude but sound: the cost of
        # a reset is one burst per client, the cost of no cap is the box's RAM.
        if len(_buckets) > 10000:
            _buckets.clear()
        b = _buckets.setdefault(ip, [BURST, now])
        b[0] = min(BURST, b[0] + (now - b[1]) * (RATE_MIN / 60.0))
        b[1] = now
        if b[0] >= 1.0:
            b[0] -= 1.0
            return True
        return False


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
    timeout = 30                          # shed connections that stop talking

    # -------------------------------------------------------------- plumbing
    def _client_ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For")
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
        if body:
            self.wfile.write(body)

    def _text(self, status, text, extra=()):
        self._reply(status, text.encode("utf-8"), "text/plain; charset=utf-8", extra)

    def log_message(self, fmt, *args):
        # One terse line per request, stderr -> journald. The default logger
        # prints the proxy's address; the client behind it is the useful one.
        print(f"{self._client_ip()} {fmt % args}", file=__import__("sys").stderr)

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

    def do_DELETE(self):
        # Session teardown, in a server that never issues a session.
        self._text(405, "stateless server: no session to delete\n",
                   extra=(("Allow", "POST, OPTIONS"),))

    def do_POST(self):
        if self.path not in MCP_PATHS:
            self._text(404, "not found\n")
            return
        if not _allow(self._client_ip()):
            self._text(429, "rate limited\n", extra=(("Retry-After", "10"),))
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._text(411, "Content-Length required\n")
            return
        if length > MAX_BODY:
            self._text(413, f"body over {MAX_BODY} bytes\n")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except Exception:
            body = json.dumps(_rpc_error(None, -32700, "parse error")).encode()
            self._reply(400, body)
            return

        if isinstance(payload, list):     # pre-2025-06-18 clients may batch
            if not payload:
                self._reply(400, json.dumps(
                    _rpc_error(None, -32600, "empty batch")).encode())
                return
            out = [r for r in (_rpc(m) for m in payload) if r is not None]
        else:
            r = _rpc(payload)
            out = [r] if r is not None else []

        if not out:
            self._reply(202)              # notifications: accepted, no body
            return
        body = json.dumps(out if isinstance(payload, list) else out[0],
                          ensure_ascii=False).encode("utf-8")
        self._reply(200, body)


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
          f"(db {core.DB_PATH})", file=__import__("sys").stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
