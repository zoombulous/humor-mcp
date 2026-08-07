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


def start(extra_env):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "humor_mcp.cli", "serve-http", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "HUMOR_DB": str(FDB), **extra_env})
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                return proc, port
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("server did not come up")


def http(port, path="/mcp", data=None, method=None):
    """(status, parsed-or-raw-body). urllib raises on >=400; we want the code."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if data is None else json.dumps(data).encode()
             if not isinstance(data, bytes) else data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
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
    check(c == 200 and len(r["result"]["tools"]) == 8, "tools/list serves all 8")
    c, r = http(port, data={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "search_humor",
                                       "arguments": {"query": "dog"}}})
    hit = json.loads(r["result"]["content"][0]["text"])
    check(c == 200 and hit["count"] == 1 and
          hit["results"][0]["credit"]["source"] == "own",
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

print("rate limit")
proc, port = start({"HUMOR_HTTP_RATE": "0", "HUMOR_HTTP_BURST": "3"})
try:
    codes = [http(port, data={"jsonrpc": "2.0", "id": 1, "method": "ping"})[0]
             for _ in range(5)]
    check(codes[:3] == [200, 200, 200] and codes[3:] == [429, 429],
          f"burst of 3 then 429s (got {codes})")
finally:
    proc.kill()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
