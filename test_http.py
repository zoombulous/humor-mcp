#!/usr/bin/env python3
"""Drive http_server.py over real HTTP the way a remote MCP client does.

Same philosophy as test_server.py: the server runs as a subprocess, the corpus
is a fixture built here, and nothing asserts against whichever packs happen to
be installed. The transport is the thing under test — status codes, batching,
notifications, limits — the tools themselves are test_server.py's problem.
"""
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent

FIXTURE = Path(tempfile.mkdtemp(prefix="humor-mcp-http-"))
FPACKS = FIXTURE / "packs"
FDB = FIXTURE / "fixture.db"

d = FPACKS / "own"
d.mkdir(parents=True)
(d / "lines.jsonl").write_text(json.dumps({
    "text": "The dog has filed a complaint about the mailman.",
    "kind": "joke", "score": 3, "rater": "owner"}) + "\n", encoding="utf-8")
(d / "pack.json").write_text(json.dumps({
    "id": "own", "title": "Own work", "authors": "A. Owner",
    "license": "CC-BY-4.0", "redistributable": True, "commercial_use": True,
    "license_verified": True, "files": ["lines.jsonl"]}), encoding="utf-8")

_b = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "build"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300, cwd=ROOT,
                    env={**os.environ, "HUMOR_PACKS": str(FPACKS),
                         "HUMOR_DB": str(FDB)})
if _b.returncode != 0:
    print(_b.stdout, _b.stderr)
    raise SystemExit("could not build the fixture corpus")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start(extra_env, log=None):
    """log=<path> captures the server's stderr, which is the access log."""
    port = free_port()
    sink = open(log, "wb") if log else subprocess.DEVNULL
    proc = subprocess.Popen(
        [sys.executable, "-m", "humor_mcp.cli", "serve-http", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=sink,
        env={**os.environ, "HUMOR_DB": str(FDB),
             "PYTHONUNBUFFERED": "1", **extra_env})
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                return proc, port
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("server did not come up")


def http(port, path="/mcp", data=None, method=None, headers=None):
    """(status, parsed-or-raw-body). urllib raises on >=400; we want the code."""
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(headers or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if data is None else json.dumps(data).encode()
             if not isinstance(data, bytes) else data,
        method=method or ("POST" if data is not None else "GET"),
        headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    try:
        return code, json.loads(raw)
    except Exception:
        return code, raw.decode("utf-8", "replace")


fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


proc, port = start({})
try:
    print("handshake")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "0"}}})
    check(c == 200 and r["result"]["protocolVersion"] == "2025-06-18",
          "initialize echoes a known protocolVersion")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 2, "method": "initialize",
                            "params": {"protocolVersion": "2099-01-01"}})
    check(c == 200 and r["result"]["protocolVersion"] != "2099-01-01",
          "unknown protocolVersion is not parroted back")
    c, r = http(port, data={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check(c == 202, "notification -> 202, no body")

    print("tools over the wire")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    check(c == 200 and len(r["result"]["tools"]) == 10, "tools/list serves all 10")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "search_humor",
                                       "arguments": {"query": "dog"}}})
    hit = json.loads(r["result"]["content"][0]["text"])
    check(c == 200 and hit["count"] == 1 and
          hit["results"][0]["source"] == "own" and hit["credits"]["own"]["authors"],
          "tools/call returns the line with its credit")
    c, r = http(port, path="/mcp/", data={"jsonrpc": "2.0", "id": 5, "method": "ping"})
    check(c == 200 and r["result"] == {}, "trailing-slash path serves too")

    print("protocol edges")
    c, r = http(port, data=[{"jsonrpc": "2.0", "id": 6, "method": "ping"},
                            {"jsonrpc": "2.0", "method": "notifications/x"}])
    check(c == 200 and isinstance(r, list) and len(r) == 1 and r[0]["id"] == 6,
          "batch: requests answered, notifications not")
    c, r = http(port, data=b"{nope")
    check(c == 400 and r["error"]["code"] == -32700, "unparseable body -> 400 / -32700")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 7, "method": "no/such"})
    check(c == 200 and r["error"]["code"] == -32601, "unknown method -> -32601")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp",
                                 headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            c = r.status
    except urllib.error.HTTPError as e:
        c = e.code
    check(c == 405, "GET asking for the event stream -> 405")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp",
                                 headers={"Accept": "text/html,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        c, r = resp.status, resp.read().decode()
    check(c == 200 and "This IS the MCP endpoint" in r
          and f"http://127.0.0.1:{port}/mcp" in r,
          "GET from a browser -> the endpoint explains itself, with its own URL")
    c, r = http(port, path="/healthz")
    check(c == 200 and r == "ok\n", "healthz answers")
    c, r = http(port, path="/")
    check(c == 200 and "custom connector" in r, "info page tells a human what to do")
    c, r = http(port, path="/mcp", method="DELETE")
    check(c == 405, "DELETE (session teardown) -> 405 on a stateless server")
    c, r = http(port, data=b'{"pad":"' + b"x" * 70000 + b'"}')
    check(c == 413, "oversize body -> 413")

    print("HEAD (link previews and uptime checks)")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="HEAD",
                                 headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        c, body, ctype = resp.status, resp.read(), resp.headers.get("Content-Type")
        clen = resp.headers.get("Content-Length")
    check(c == 200 and body == b"" and "text/plain" in (ctype or "")
          and (clen or "0") != "0",
          f"HEAD /mcp -> 200, no body, GET's headers (was 501; got {c}/{clen})")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="HEAD")
    with urllib.request.urlopen(req, timeout=10) as resp:
        check(resp.status == 200 and resp.read() == b"", "HEAD /healthz -> 200")

    print("concurrency (the thread-bound-sqlite regression)")
    results = []
    def hammer():
        c, r = http(port, data={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                "params": {"name": "corpus_stats", "arguments": {}}})
        ok = c == 200 and not r["result"].get("isError")
        results.append(ok)
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check(all(results) and len(results) == 8,
          "8 parallel workers all reach the corpus (no ProgrammingError)")
finally:
    proc.kill()

print("rate limit (per client)")
proc, port = start({"HUMOR_HTTP_RATE": "0", "HUMOR_HTTP_BURST": "3"})
try:
    codes = [http(port, data={"jsonrpc": "2.0", "id": 1, "method": "ping"})[0]
             for _ in range(5)]
    check(codes[:3] == [200, 200, 200] and codes[3:] == [429, 429],
          f"burst of 3 then 429s (got {codes})")
finally:
    proc.kill()

print("rate limit (global — the box's own bucket)")
proc, port = start({"HUMOR_HTTP_GLOBAL_RATE": "0", "HUMOR_HTTP_GLOBAL_BURST": "2"})
try:
    codes = [http(port, data={"jsonrpc": "2.0", "id": 1, "method": "ping"})[0]
             for _ in range(4)]
    check(codes[:2] == [200, 200] and codes[2:] == [503, 503],
          f"global burst of 2, then 503 while per-client is wide open (got {codes})")
finally:
    proc.kill()

print("bearer keys buy headroom, never admission")
KEYFILE = FIXTURE / "keys.txt"
KEYFILE.write_text(
    "# id:secret:rate:burst\n"
    "friend:s3cret:600:5\n"
    "bad-line-no-secret\n"
    "\n", encoding="utf-8")
proc, port = start({"HUMOR_HTTP_RATE": "0", "HUMOR_HTTP_BURST": "1",
                    "HUMOR_HTTP_KEYS": str(KEYFILE)})
try:
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    anon = [http(port, data=ping)[0] for _ in range(3)]
    check(anon == [200, 429, 429], f"anonymous gets the tight bucket (got {anon})")
    keyed = [http(port, data=ping,
                  headers={"Authorization": "Bearer s3cret"})[0] for _ in range(4)]
    check(keyed == [200, 200, 200, 200],
          f"a keyed caller has its own, larger bucket (got {keyed})")
    c, _ = http(port, data=ping, headers={"Authorization": "Bearer wrong"})
    check(c == 429, f"an unknown key is anonymous, not 401 (got {c})")
    # The anon bucket was already spent above, so 429 IS the anonymous answer
    # here; what matters is that it is never 401/403.
    check(c not in (401, 403), "an unknown key is never rejected outright")
finally:
    proc.kill()

print("access log: one line per call, tool named, arguments never")
LOG = FIXTURE / "access.log"
proc, port = start({"HUMOR_HTTP_KEYS": str(KEYFILE)}, log=str(LOG))
try:
    SECRET_QUERY = "zzsecretqueryzz"
    http(port, data={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "search_humor",
                                "arguments": {"query": SECRET_QUERY}}})
    http(port, data={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "corpus_stats", "arguments": {}}},
         headers={"Authorization": "Bearer s3cret"})
    time.sleep(0.5)
finally:
    proc.kill()
text = LOG.read_text(encoding="utf-8", errors="replace")
lines = [l for l in text.splitlines() if " mcp " in l]
check(any("tool=search_humor" in l and "key=anon" in l and "status=200" in l
          and "ms=" in l for l in lines),
      "anonymous tools/call logs tool, key, status and duration")
check(any("tool=corpus_stats" in l and "key=friend" in l for l in lines),
      "a keyed call logs its key id, not its secret")
check(SECRET_QUERY not in text and "s3cret" not in text,
      "neither the query text nor the key itself reaches the log")
check(len(lines) == 2 and not any('"POST /mcp' in l for l in text.splitlines()),
      f"exactly one line per MCP request, no duplicate default line ({len(lines)})")

print("idle keep-alive reaps are not errors; a silent connection is")
LOG2 = FIXTURE / "idle.log"
proc, port = start({"HUMOR_HTTP_IDLE_TIMEOUT": "1"}, log=str(LOG2))
try:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\n"
              b"Content-Type: application/json\r\nContent-Length: "
              + str(len(body)).encode() + b"\r\n\r\n" + body)
    got = s.recv(4096)
    time.sleep(2.5)                       # let the 1s idle timeout fire
    s.close()
    time.sleep(0.3)
    after_keepalive = LOG2.read_text(encoding="utf-8", errors="replace")
    check(b"200 OK" in got, "keep-alive request answered")
    check("Request timed out" not in after_keepalive,
          "a client that asked and went quiet logs no error")

    quiet = socket.create_connection(("127.0.0.1", port), timeout=5)
    time.sleep(2.5)                       # connect, say nothing at all
    quiet.close()
    time.sleep(0.3)
finally:
    proc.kill()
tail = LOG2.read_text(encoding="utf-8", errors="replace")[len(after_keepalive):]
# Assert on a real log LINE, not the substring: `self.headers` does not exist
# until a request has been parsed, so dereferencing it here used to raise
# inside socketserver and print a traceback whose source lines contain this
# very string — which is how the first version of this check passed while the
# behaviour was broken.
timeout_lines = [l for l in tail.splitlines()
                 if l.strip().startswith("127.0.0.1") and "Request timed out" in l]
check(bool(timeout_lines),
      "a connection that never sent a request logs one clean line, with its IP")
check("Traceback" not in tail and "AttributeError" not in tail,
      "and logs it without a traceback (headers do not exist that early)")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
