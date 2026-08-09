#!/usr/bin/env python3
"""Drive server.py over real stdio the way an MCP client does."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------- fixture corpus
# These tests check the SERVER's behaviour, so they build their own small corpus
# rather than asserting against whichever packs happen to be installed. A clone
# of this repo does not contain the private packs the author has locally, and a
# suite that goes red on someone else's first run is worse than no suite.
FIXTURE = Path(tempfile.mkdtemp(prefix="humor-mcp-fixture-"))
FPACKS = FIXTURE / "packs"


def _pack(pid, manifest, lines=(), pairs=()):
    d = FPACKS / pid
    d.mkdir(parents=True, exist_ok=True)
    files = []
    if lines:
        (d / "lines.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
            encoding="utf-8")
        files.append("lines.jsonl")
    if pairs:
        (d / "pairs.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pairs),
            encoding="utf-8")
        files.append("pairs.jsonl")
    (d / "pack.json").write_text(json.dumps({**manifest, "id": pid, "files": files}),
                                 encoding="utf-8")


def _bd(mech):
    return json.dumps({"method": [mech], "reason": ["it turns on " + mech]})


_pack("own", {"title": "Own work", "authors": "A. Owner", "license": "CC-BY-4.0",
              "redistributable": True, "commercial_use": True, "license_verified": True,
              "url": "https://example.com/own"},
      lines=[{"text": "The dog has filed a complaint about the mailman.", "context":
              "asked how the pets are", "kind": "joke", "score": 3, "rater": "owner",
              "note": "deadpan", "tags": json.dumps(["dog"]), "breakdown": _bd("reframe"),
              "attribution": "written by A. Owner"},
             {"text": "I alphabetised the spice rack twice.", "context": "the weekend",
              "kind": "joke", "score": 3, "rater": "owner", "breakdown": _bd("escalation")},
             {"text": "A hospital joke that lands in the middle.", "context": "",
              "kind": "joke", "score": 2, "rater": "owner"},
             {"text": "This one did not work at all.", "context": "", "kind": "joke",
              "score": 0, "rater": "owner", "note": "too quippy"},
             {"text": "A weaker attempt nobody laughed at.", "context": "", "kind": "joke",
              "score": 1, "rater": "owner"},
             {"text": "therapy", "kind": "word", "score": 5, "rater": "owner"},
             # Whole kinds carry no human score: slate_winner lines are best-of-N
             # picks the engine chose, eval lines are verdicts. They are the shape
             # that made top_rated return a bare empty list at every min_score.
             {"text": "The basil knows things about you now.", "context": "the spices",
              "kind": "slate_winner", "attribution": "engine best-of-N winner"},
             {"text": "Verdict: the second one lands.", "kind": "eval"},
             # An auto-segmented transcript offcut: starts lowercase, stops mid
             # sentence. Reads like a joke that died rather than half of one.
             {"text": "and then the mailman said", "kind": "joke"},
             # The same rated line stored a second time without its analysis —
             # the shape that fills a quarter of the mallard pack and made
             # taste_profile(n=10) return six distinct lines. Note the missing
             # attribution: one copy carrying it and the other not is how the
             # real pack stores it, and treating that as a provenance conflict
             # linked nothing at all on the first attempt.
             {"text": "The dog has filed a complaint about the mailman.",
              "context": "asked how the pets are", "kind": "candidate",
              "score": 3, "rater": "owner"}],
      pairs=[{"prompt": "Tell me a joke.",
              "chosen": "The dog has filed a complaint about the mailman.",
              "rejected": "This one did not work at all.", "weight": 1.0},
             {"prompt": "Again.", "chosen": "Also good.", "rejected": "Also bad."}])

# licence says it may not leave this machine
_pack("locked", {"title": "Third-party set", "authors": "Someone Else",
                 "license": "ALL-RIGHTS-RESERVED", "redistributable": False,
                 "commercial_use": False, "license_verified": True},
      lines=[{"text": "A restricted line about a dog.", "kind": "joke",
              "attribution": 'P. One — "Set A" (https://example.com/a)'},
             {"text": "The same words from two different mouths.", "kind": "joke",
              "attribution": 'P. One — "Set A" (https://example.com/a)'},
             {"text": "The same words from two different mouths.", "kind": "joke",
              "attribution": 'P. Two — "Set B" (https://example.com/b)'}])

# licence is fine; the owner judged it unrepresentative
_pack("offrubric", {"title": "Off-rubric pairs", "authors": "Crowd",
                    "license": "CC-BY-4.0", "redistributable": True,
                    "commercial_use": True, "license_verified": True,
                    "default_hidden": True,
                    "hidden_reason": "regressed the rubric; kept as a contrast set"},
      pairs=[{"prompt": "p%d" % i, "chosen": "c%d" % i, "rejected": "r%d" % i}
             for i in range(40)])

_pack("unverified", {"title": "Licence not established", "authors": "Unknown",
                     "license": "UNKNOWN", "redistributable": False,
                     "commercial_use": False, "license_verified": False},
      lines=[{"text": "Provenance for this line was never recorded.", "kind": "joke"}])

FDB = FIXTURE / "humor.db"
# No cwd override: HUMOR_PACKS and HUMOR_DB are absolute, and running from
# elsewhere would put `humor_mcp` off sys.path in a checkout with nothing installed.
_b = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "build"],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=300,
                    env={**os.environ, "HUMOR_PACKS": str(FPACKS),
                         "HUMOR_DB": str(FDB)})
if _b.returncode != 0:
    print(_b.stdout, _b.stderr)
    raise SystemExit("could not build the fixture corpus")
SERVER_ENV = {**os.environ, "HUMOR_DB": str(FDB)}

REQS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "corpus_stats", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "sources", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": "dog", "limit": 3}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "search_humor",
                "arguments": {"query": "dog", "limit": 3, "include_restricted": True}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "top_rated", "arguments": {"limit": 3}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "taste_profile", "arguments": {"n": 3}}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
     "params": {"name": "breakdown", "arguments": {"query": "hospital", "limit": 2}}},
    {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
     "params": {"name": "preference_pairs", "arguments": {"limit": 2}}},
    {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
     "params": {"name": "style_pack", "arguments": {"n": 3}}},
    # adversarial: FTS-breaking input must not crash
    {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": '" OR x MATCH (', "limit": 2}}},
    {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
     "params": {"name": "nope", "arguments": {}}},
    # the off-rubric gate: hidden by default, reachable three ways
    {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
     "params": {"name": "preference_pairs", "arguments": {"limit": 50}}},
    {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
     "params": {"name": "preference_pairs",
                "arguments": {"limit": 50, "include_hidden": True}}},
    {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
     "params": {"name": "preference_pairs",
                "arguments": {"limit": 5, "source": "offrubric"}}},
    # a caller must not be able to flood its own context
    {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": "the", "limit": 99999}}},
    {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
     "params": {"name": "top_rated", "arguments": {"limit": 5000}}},
    {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": "dog", "limit": -3}}},
    # search_humor + source= is the aliased ("l.source_id") path; exercise it
    # against a restricted pack, where naming it must override the licence gate
    {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
     "params": {"name": "search_humor",
                "arguments": {"query": "dog", "limit": 5, "source": "locked"}}},
    {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": "dog", "limit": 5}}},
    {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
     "params": {"name": "search_humor",
                "arguments": {"limit": 5, "source": "locked"}}},
    # top_rated ranks by human score, so unrated kinds can never appear in it at
    # any threshold. It must say so — an unexplained empty list reads as "the
    # corpus has none of these", which sends the caller away from lines that are
    # right there. 23/24 are the unrated case, 26 the merely-too-high-bar case.
    {"jsonrpc": "2.0", "id": 23, "method": "tools/call",
     "params": {"name": "top_rated", "arguments": {"kind": "slate_winner"}}},
    {"jsonrpc": "2.0", "id": 24, "method": "tools/call",
     "params": {"name": "top_rated",
                "arguments": {"kind": "slate_winner", "min_score": 0}}},
    {"jsonrpc": "2.0", "id": 25, "method": "tools/call",
     "params": {"name": "search_humor",
                "arguments": {"kind": "slate_winner", "limit": 5}}},
    {"jsonrpc": "2.0", "id": 26, "method": "tools/call",
     "params": {"name": "top_rated", "arguments": {"kind": "joke", "min_score": 99}}},
    # style_pack must not hand back a corpus-wide brief as if it were about the
    # topic asked for
    {"jsonrpc": "2.0", "id": 27, "method": "tools/call",
     "params": {"name": "style_pack", "arguments": {"topic": "xylophone", "n": 3}}},
    {"jsonrpc": "2.0", "id": 28, "method": "tools/call",
     "params": {"name": "style_pack", "arguments": {"topic": "dog", "n": 3}}},
    # the engine's best-of-N picks are unrated, so every score-gated query drops
    # them. They are the most on-register material there is and belong in the
    # brief — but labelled, so a machine's pick is never read as a human verdict.
    {"jsonrpc": "2.0", "id": 29, "method": "tools/call",
     "params": {"name": "style_pack", "arguments": {"n": 4}}},
    # transcript offcuts are filterable, but only when asked for — defaulting it
    # on would silently drop real lines from packs that skip a final full stop
    {"jsonrpc": "2.0", "id": 30, "method": "tools/call",
     "params": {"name": "search_humor", "arguments": {"query": "mailman", "limit": 5}}},
    {"jsonrpc": "2.0", "id": 31, "method": "tools/call",
     "params": {"name": "search_humor",
                "arguments": {"query": "mailman", "limit": 5, "whole_lines": True}}},
]

p = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "serve"],
                   input="\n".join(json.dumps(r) for r in REQS) + "\n",
                   capture_output=True, text=True, encoding="utf-8", timeout=120,
                   env=SERVER_ENV)
if p.stderr.strip():
    print("STDERR:", p.stderr[:2000])

resp = {}
for ln in p.stdout.splitlines():
    if not ln.strip():
        continue
    r = json.loads(ln)
    resp[r.get("id")] = r

fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def body(i):
    return json.loads(resp[i]["result"]["content"][0]["text"])


print("protocol")
check(resp[1]["result"]["serverInfo"]["name"] == "humor-mcp", "initialize")
check(2 not in [r for r in resp if False] and len(resp[2]["result"]["tools"]) == 10,
      "tools/list returns 10 tools")
check(all("inputSchema" in t for t in resp[2]["result"]["tools"]), "every tool has a schema")

print("\ncorpus")
st = body(3)
check(st["lines"] == 14, f"every fixture line loaded = {st['lines']}")
check(st["pairs"] == 42, f"every fixture pair loaded = {st['pairs']}")
check(st["sources"] == 4, f"sources = {st['sources']}")
check({"own", "locked", "offrubric", "unverified"} == set(st["by_source"]) | {"offrubric"},
      f"packs present: {sorted(st['by_source'])}")

print("\ncredit")
src = body(4)["sources"]
check(all(s["authors"] for s in src), "every source names its authors")
check(all(s["license"] for s in src), "every source declares a license")
unver = {s["id"] for s in src if not s["license_verified"]}
check(unver == {"unverified"}, f"unverified flagged: {sorted(unver)}")
nonred = {s["id"] for s in src if not s["redistributable"]}
check(nonred == {"locked", "unverified"}, f"non-redistributable: {sorted(nonred)}")
check({s["id"] for s in src if s["default_hidden"]} == {"offrubric"},
      "off-rubric packs flagged")
check(all(s["hidden_reason"] for s in src if s["default_hidden"]),
      "every hidden pack explains why")

print("\nresults carry credit")
r5 = body(5)
check(r5["count"] > 0, f"search returned {r5['count']}")
check(all("source" in x for x in r5["results"]) and
      all(c.get("authors") for c in r5["credits"].values()),
      "authorship travels once per source, not once per row")
srcs_default = {x["source"] for x in r5["results"]}
check("locked" not in srcs_default, "restricted pack hidden by default")
r6 = body(6)
check(any(x["source"] == "locked" for x in r6["results"]),
      "include_restricted widens the search")

print("\nper-line attribution")
p = subprocess.run([sys.executable, "-c", """
import sqlite3
c = sqlite3.connect(r"%s")
q = lambda w: c.execute(
    "select count(*) from lines where source_id='locked' and (" + w + ")").fetchone()[0]
print(q("attribution like '%%example.com%%'"), q("attribution like '%%P. One%%'"
      " or attribution like '%%P. Two%%'"))
""" % FDB], capture_output=True, text=True)
url_n, named_n = (int(x) for x in p.stdout.split())
check(url_n == 3, f"restricted lines linked to a source: {url_n}")
check(named_n == 3, f"restricted lines crediting a named performer: {named_n}")
check(url_n == named_n, "every linked line also names its performer")

print("\ndedupe keeps credit intact")
p = subprocess.run([sys.executable, "-c", """
import sqlite3
c = sqlite3.connect(r"%s")
q = lambda s: c.execute(s).fetchone()[0]
print(q('''select count(*) from (select source_id,text,attribution from lines
           group by source_id,text,attribution having count(*)>1)'''),
      q("select count(*) from lines"),
      q("select count(*) from lines where breakdown is not null"),
      q('''select count(*) from (select text from lines
           group by text having count(distinct attribution)>1)'''))
""" % FDB], capture_output=True, text=True)
dupes, total, with_bd, split = (int(x) for x in p.stdout.split())
check(dupes == 0, f"no (text, attribution) duplicates in the build: {dupes}")
check(total == 14, f"all fixture lines present: {total}")
check(with_bd == 2, f"breakdown json survives the build: {with_bd}")
check(split == 1, f"same text under different credit stays separate: {split} case(s)")

print("\ntools")
check(len(body(7)["results"]) > 0, "top_rated")
tp = body(8)
check(tp["liked"] and tp["disliked"], "taste_profile has both poles")
check(all(x["source"] in tp["credits"] for x in tp["liked"]),
      "taste_profile credits its lines, once per source")
check(body(9)["count"] >= 0, "breakdown")
pr = body(10)
check(all(x["source"] in pr["credits"] for x in pr["results"]),
      "preference pairs credited")
sp = body(11)
check("attribution_block" in sp and sp["attribution_block"].strip(), "style_pack ships credit")
check(all(e["source"] in sp["credits"] for e in sp["exemplars"]),
      "style_pack exemplars credited")

print("\noff-rubric gate (separate from the licence gate)")
d14, d15, d16 = body(14), body(15), body(16)
def_srcs = {x["source"] for x in d14["results"]}
check(def_srcs == {"own"}, f"default pairs are on-rubric only: {sorted(def_srcs)}")
check(set(d14.get("withheld", {}).get("packs", {})) == {"offrubric"},
      f"withheld is reported, not silent: {d14.get('withheld', {}).get('packs')}")
check("offrubric" in {x["source"] for x in d15["results"]},
      "include_hidden reaches them")
check({x["source"] for x in d16["results"]} == {"offrubric"},
      "naming the pack overrides the gate")
check(all("off_rubric" in d16["credits"][x["source"]] for x in d16["results"]),
      "off-rubric packs say why when you do pull them")
srcs = {s["id"]: s for s in body(4)["sources"]}
check(srcs["offrubric"]["redistributable"] and srcs["offrubric"]["default_hidden"],
      "shareable by licence, hidden by judgement — the two are independent")
check(not srcs["locked"]["redistributable"] and not srcs["locked"]["default_hidden"],
      "restricted by licence but not judged off-rubric — also independent")

print("\nregression: results cannot flood the caller's context")
big = resp[17]["result"]["content"][0]["text"]
d17 = json.loads(big)
check(d17["count"] <= 200, f"limit=99999 clamped to {d17['count']}")
check(len(big) < 400_000, f"response is {len(big):,} chars, not megabytes")
check("limit_capped" in d17, "and the caller is told it was capped")
check(json.loads(resp[18]["result"]["content"][0]["text"])["count"] <= 200,
      "top_rated clamped too")
d19 = json.loads(resp[19]["result"]["content"][0]["text"])
check(d19["count"] <= 10 and "limit_capped" not in d19,
      "a nonsense limit falls back to the default without complaining")

print("\nsql fragments are aliased, not string-patched")
p = subprocess.run([sys.executable, "-c", """
import sys
sys.path.insert(0, r"%s")
from humor_mcp import server
frag_a, _ = server.src_filter(False, "own", False)
frag_b, _ = server.src_filter(False, "own", False, alias="l")
frag_c, _ = server.src_filter(False, None, False, alias="l")
kc_a, _ = server.kind_clause(None)
kc_b, _ = server.kind_clause(None, alias="l")
print(repr(frag_a)); print(repr(frag_b)); print(repr(frag_c))
print(repr(kc_a)); print(repr(kc_b))
""" % ROOT], capture_output=True, text=True, encoding="utf-8", timeout=120,
                   # src_filter now consults the db (unknown packs must be loud),
                   # so this probe must see the FIXTURE corpus: without HUMOR_DB
                   # it reads whatever ~/.humor-mcp holds — absent on a clean
                   # clone (suite crashes), private on the author's machine
                   # (suite silently depends on it). The pack is "own" for the
                   # same reason: it exists in the fixture, not just locally.
                   env={**os.environ, "HUMOR_DB": str(FDB)})
out = p.stdout.splitlines()
check(out[0] == "'source_id = ?'", f"unaliased stays bare: {out[0]}")
check(out[1] == "'l.source_id = ?'", f"alias qualifies it: {out[1]}")
check(out[2].startswith("'l.source_id NOT IN"), f"alias applies to NOT IN too: {out[2]}")
check(".kind" not in out[3] and "kind IS NULL" in out[3],
      f"kind_clause unaliased: {out[3]}")
check("l.kind IS NULL" in out[4] and "l.kind NOT IN" in out[4],
      f"kind_clause aliased on every mention: {out[4]}")
src = (ROOT / "humor_mcp" / "server.py").read_text(encoding="utf-8")
check(".replace('source_id'" not in src and '.replace("source_id"' not in src,
      "no string-patching of SQL left in the file")

d20, d21, d22 = body(20), body(21), body(22)
s20 = {x["source"] for x in d20["results"]}
check(d20["count"] > 0 and s20 == {"locked"},
      f"aliased FTS + source= returns that pack: {d20['count']} from {sorted(s20)}")
check("locked" not in {x["source"] for x in d21["results"]},
      "same query without source= still excludes the restricted pack")
s22 = {x["source"] for x in d22["results"]}
check(d22["count"] > 0 and s22 == {"locked"},
      f"aliased non-FTS browse + source= also works: {sorted(s22)}")

print("\nschemas are derived from signatures, not maintained beside them")
p = subprocess.run([sys.executable, "-c", """
import inspect, json, sys
sys.path.insert(0, r"%s")
from humor_mcp import server
bad = []
for name, _desc, fn in server.TOOLS:
    props, required = server.param_schema(fn)
    sig = set(inspect.signature(fn).parameters)
    if set(props) != sig:
        bad.append((name, sorted(sig ^ set(props))))
    for pname, prm in inspect.signature(fn).parameters.items():
        if prm.default is not inspect.Parameter.empty and prm.default is not None:
            if props[pname].get("default") != prm.default:
                bad.append((name, pname))
print(json.dumps(bad))
# a tool whose parameter is undocumented must fail, not ship a silent gap
def t_bogus(query="", undocumented_arg=1):
    return {}
try:
    server.param_schema(t_bogus)
    print("NO_ERROR")
except RuntimeError as e:
    print("RAISED" if "PARAM_DOC" in str(e) else "WRONG_ERROR")
""" % ROOT], capture_output=True, text=True, encoding="utf-8", timeout=120)
lines_out = p.stdout.strip().splitlines()
check(lines_out[0] == "[]", f"every tool's schema matches its signature: {lines_out[0]}")
check(lines_out[1] == "RAISED", "an undocumented parameter fails loudly at import")
defs = resp[2]["result"]["tools"]
bd = [t for t in defs if t["name"] == "breakdown"][0]
# breakdown.query grew a default when "empty = browse by score" became true of
# it (it was the one required parameter anywhere); requiredness must follow the
# signature down as well as up.
check(bd["inputSchema"].get("required") is None,
      f"requiredness derived from the signature: {bd['inputSchema'].get('required')}")
sh = [t for t in defs if t["name"] == "search_humor"][0]["inputSchema"]["properties"]
check(sh["limit"]["type"] == "integer" and sh["limit"]["default"] == 10,
      "int default -> integer type")
check(sh["include_hidden"]["type"] == "boolean", "bool default -> boolean type")
check(sh["min_score"]["type"] == "number", "None default with a numeric name -> number")
check(all(pr.get("description") for t in defs
          for pr in t["inputSchema"]["properties"].values()),
      "every advertised parameter carries a description")

print("\nan empty result explains itself")
d23, d24, d25, d26 = body(23), body(24), body(25), body(26)
check(d23["count"] == 0 and d23["note"], "unrated kind returns empty WITH a reason")
check("none carry a human score" in d23["note"],
      f"and names the cause: {d23['note'][:60]}...")
check("search_humor" in d23["note"], "and points at the tool that CAN show them")
check(d24["note"] == d23["note"],
      "min_score=0 changes nothing — the ratings are absent, not low")
check(d25["count"] > 0,
      f"the same lines are reachable via search_humor: {d25['count']}")
check(d26["count"] == 0 and "min_score" in d26["note"],
      "a bar nothing clears says to lower the bar")
check(d23["note"] != d26["note"], "the two empty cases are told apart")

# The structural version of the above: no kind in the corpus may come back
# empty and silent. A unit test per kind would go stale the moment a pack adds
# one, so enumerate whatever is actually loaded.
p = subprocess.run([sys.executable, "-c", """
import os, sys, json
sys.path.insert(0, r"%s")
os.environ["HUMOR_DB"] = r"%s"
from humor_mcp import server
kinds = [r[0] for r in server.db().execute(
    "select distinct kind from lines where kind is not null order by kind")]
silent = [(k, ms) for k in kinds for ms in (0, 2)
          if server.t_top_rated(kind=k, min_score=ms)["count"] == 0
          and not server.t_top_rated(kind=k, min_score=ms)["note"]]
print(json.dumps([kinds, silent]))
""" % (ROOT, FDB)], capture_output=True, text=True, encoding="utf-8", timeout=120)
kinds, silent = json.loads(p.stdout.strip().splitlines()[-1])
check(len(kinds) >= 4, f"fixture covers several kinds: {kinds}")
check(silent == [], f"no kind returns a silent empty: {silent}")

print("\nstyle_pack does not pass a corpus-wide brief off as a topic brief")
d27, d28 = body(27), body(28)
check(d27["exemplars"] == [] and d27["topic_matched"] is False,
      "a topic with no match is marked unmatched")
check("topic_note" in d27 and "xylophone" in d27["topic_note"],
      "and says so in words, naming the topic")
check(d27["liked"] and "corpus-wide" in d27["topic_note"],
      "the calibration still ships, correctly labelled as not topical")
check(d28["exemplars"] and d28["topic_matched"] is True,
      f"a topic that does match is marked matched: {len(d28['exemplars'])} exemplars")
check("topic_note" not in d28, "and carries no warning")

print("\nthe engine's own picks reach the style brief, labelled")
d29 = body(29)
bases = [e["basis"] for e in d29["exemplars"]]
check(all(e.get("basis") for e in d29["exemplars"]),
      f"every exemplar states what it rests on: {bases}")
check(any(b.startswith("engine best-of-N") for b in bases),
      "unrated best-of-N picks are no longer excluded by the score gate")
check(any(b.startswith("human-rated") for b in bases),
      "and human ratings still lead — picks do not crowd them out")
check(any(e["text"].startswith("The basil") for e in d29["exemplars"]),
      "the specific unrated slate_winner is in there")

print("\ntranscript offcuts are filterable, opt-in only")
d30, d31 = body(30), body(31)
t30 = {x["text"] for x in d30["results"]}
t31 = {x["text"] for x in d31["results"]}
check("and then the mailman said" in t30,
      "an offcut is returned by default — nothing is hidden behind your back")
check("and then the mailman said" not in t31, "whole_lines drops it")
check(any(x.startswith("The dog has filed") for x in t31),
      f"and keeps the complete line on the same query: {len(t31)} kept")
_h = subprocess.run([sys.executable, "-c", """
import sys, json
sys.path.insert(0, r"%s")
from humor_mcp import server
w = server.whole_line
print(json.dumps([w("The dog has filed a complaint."), w("and then he said"),
                  w("Couples therapy is"), w('"Quoted and finished."'),
                  w("A pun with no full stop")]))
""" % ROOT], capture_output=True, text=True, encoding="utf-8", timeout=120)
whole, lower, cut, quoted, nostop = json.loads(_h.stdout.strip().splitlines()[-1])
check((whole, lower, cut) == (1, 0, 0), "complete kept, lowercase and cut-off dropped")
check(quoted == 1, "a quoted line that finishes is complete")
check(nostop == 0,
      "KNOWN LIMIT: a real line with no full stop is called an offcut — the "
      "reason this is opt-in")

print("\nbuild names what it is about to delete")
WARN_DB = FIXTURE / "warn.db"
shutil.copyfile(FDB, WARN_DB)
BENV = {**os.environ, "HUMOR_PACKS": str(FPACKS), "HUMOR_DB": str(WARN_DB)}
_w = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "build", "--only", "own"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300, env=BENV).stdout
check("REMOVES" in _w, "a build that drops packs says so before dropping them")
check("locked" in _w and "unverified" in _w,
      "and names each one it is about to lose")
check("(3 lines)" in _w, "with the line count, so the size of the loss is visible")
_f = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "build"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300, env=BENV).stdout
check("REMOVES" not in _f, "a build that loses nothing does not cry wolf")

print("\nrobustness")
check(not resp[12].get("result", {}).get("isError"), "FTS-hostile query handled")
check(resp[13]["result"].get("isError"), "unknown tool errors cleanly")

# ---------------------------------------------------------------- the F-series
# Direct in-process calls: these assert the tools' contracts, not the transport.
# ⚠ Point the module at the FIXTURE before importing it. An earlier probe in
# this file silently read the author's private ~/.humor-mcp/humor.db, passed
# locally, and crashed on a clean clone — so the first thing asserted below is
# that this suite is reading its own corpus and nobody else's.
os.environ["HUMOR_DB"] = str(FDB)
os.environ["HUMOR_PACKS"] = str(FPACKS)
from humor_mcp import server as server_mod
server = server_mod

print("\nthe suite is reading its own fixture, not a private corpus")
check(str(server.DB_PATH) == str(FDB),
      f"server loaded the fixture db ({server.DB_PATH})")

print("\nduplicate lines occupy one slot, not two (F4)")
import sqlite3
_c = sqlite3.connect(FDB)
_dupes = _c.execute(
    "SELECT id, kind, score, origin_id FROM lines WHERE source_id='own' "
    "AND text = 'The dog has filed a complaint about the mailman.' "
    "ORDER BY id").fetchall()
check(len(_dupes) == 2, f"fixture stores the line twice ({len(_dupes)})")
_canon = [r for r in _dupes if r[3] is None]
_link = [r for r in _dupes if r[3] is not None]
check(len(_canon) == 1 and len(_link) == 1,
      "exactly one copy is canonical and one points at it")
check(_canon[0][1] == "joke" and _link[0][1] == "candidate",
      "the analysed copy is the canonical one, not whichever came first")
check(_link[0][2] == 3.0, "the duplicate KEEPS its rating — nothing is discarded")
check(_link[0][3] == _canon[0][0], "the link points at the canonical id")

# The two-performers case: same words, different attribution. Merging these
# would credit one performer with the other's line, so they must both stand.
_two = _c.execute(
    "SELECT origin_id FROM lines WHERE source_id='locked' "
    "AND text = 'The same words from two different mouths.'").fetchall()
check(len(_two) == 2 and all(r[0] is None for r in _two),
      "same words from different mouths are NEVER merged")

_tp = server.HANDLERS["taste_profile"](n=10)
_texts = [x["text"] for x in _tp["liked"]]
check(len(_texts) == len(set(_texts)),
      f"taste_profile returns distinct lines ({len(set(_texts))}/{len(_texts)})")
_cand = server.HANDLERS["search_humor"](kind="candidate")
check(any(x["text"].startswith("The dog has filed") for x in _cand["results"]),
      "naming kind='candidate' still reaches the rows the filter hides")

print("\ncredit travels once per source, not once per row (F11)")
_s = server.HANDLERS["search_humor"](query="dog", limit=5)
check(all("credit" not in r for r in _s["results"]), "no per-row credit block")
check(all(r["source"] in _s["credits"] for r in _s["results"]),
      "every row's source resolves in the credits map")

print("\nAPI additions (F12)")
check(_s["total"] >= _s["returned"], f"total {_s['total']} >= returned {_s['returned']}")
check(bool(_s.get("order")), "result ordering is stated, not guessed at")
_p = server.HANDLERS["preference_pairs"](limit=5)
check(any("chosen_id" in x or "rejected_id" in x for x in _p["results"]),
      "pairs resolve to line ids where the text is unambiguous")
_g = server.HANDLERS["get_line"](id=_link[0][0])
check(_g["line"]["duplicate_of"] == _canon[0][0] and "duplicate_of_line" in _g["line"],
      "get_line on a duplicate names the canonical copy")
try:
    server.HANDLERS["get_line"](id=10 ** 9)
    check(False, "get_line on a missing id errors")
except ValueError:
    check(True, "get_line on a missing id errors")
try:
    server.HANDLERS["search_humor"](kind="nosuchkind")
    check(False, "an unknown kind errors instead of returning empty")
except ValueError as e:
    check("available" in str(e), "an unknown kind errors and lists the real ones")

print("\nbatches and classes, both derived — the rater is never asked again (F7/F13)")
_sl = _c.execute(
    "SELECT slate_id, count(*) n, max(score) top FROM lines "
    "WHERE source_id='own' AND slate_id IS NOT NULL GROUP BY slate_id").fetchall()
check(bool(_sl), f"a shared setup is recovered as a batch ({len(_sl)} found)")
_win = _c.execute("SELECT count(*) FROM lines WHERE source_id='own' "
                  "AND class='winner'").fetchone()[0]
check(_win >= 1, f"the top of each batch is marked winner ({_win})")
_lone = _c.execute(
    "SELECT count(*) FROM lines WHERE source_id='own' AND slate_id IS NOT NULL "
    "AND slate_id IN (SELECT slate_id FROM lines GROUP BY slate_id HAVING count(*)<2)"
).fetchone()[0]
check(_lone == 0, "a lone line is not called a batch")
# Every score in the fixture survives being classified.
_before = _c.execute("SELECT count(*) FROM lines WHERE source_id='own' "
                     "AND score IS NOT NULL").fetchone()[0]
check(_before == 7, f"classification changed no ratings ({_before} still scored)")

_tp2 = server.HANDLERS["taste_profile"](n=4)
check("rated_in_batches" in _tp2,
      "taste_profile says scores are within-batch rankings, not absolute grades")
check(_tp2["rated_in_batches"]["batches"] == len(_sl),
      "and reports the batch count it actually found")

print("\nlint ranks borrowed phrasing where it can be seen")
from humor_mcp.lint import stock_phrases as _stock, findings as _find
# A borrowed core reused across two setups, buried inside longer sentences
# whose ordinary words would drag its rarity down — the shape that hid
# "chose violence" behind "didn't need it and chose violence".
_rows = [("setup one", "the printer knew and chose violence anyway"),
         ("setup two", "my umbrella turned inside out and chose violence"),
         ("setup three", "i think it is probably fine at this point"),
         ("setup four", "i think it is probably fine at this point too"),
         ("setup five", "well i think that it is fine at this point")]
_st = _stock(_rows)
_names = [p for p, _, _ in _st]
check("chose violence" in _names,
      f"the distinctive core is reported, not the sentence around it: {_names[:3]}")
check(_names and _names[0] == "chose violence",
      f"and it outranks ordinary collocations: {_names[:3]}")
check(not any("at this point" == p for p in _names),
      "a phrase of pure connective English is not called borrowed")
check(all(not (a != b and (a in b or b in a)) for a in _names for b in _names),
      "one entry per overlapping family")
# The typo checks and the phrasing report are independent: a clean line with a
# reused phrase is not a defect.
check(_find("my umbrella turned inside out and chose violence") == [],
      "a stock phrase is not reported as a typo")

print("\ncontrast: the winner-vs-failure set assembles itself")
from humor_mcp.contrast import slates as _slates
_cs = _slates(_c, "own")
if _cs:
    _ids = [r["id"] for _, rows in _cs for r in rows]
    _classes = {r["class"] for _, rows in _cs for r in rows}
    check(_classes <= {"winner", "fail"},
          f"controls are excluded — a seeded non-answer is not a failed joke: {_classes}")
    check(all(any(r["class"] == "winner" for r in rows) and
              any(r["class"] == "fail" for r in rows) for _, rows in _cs),
          "every batch offered carries both sides of the contrast")
else:
    # The fixture's breakdowns carry no `fit`, so class stays unset there and
    # there is nothing to contrast. Assert the honest empty rather than skip.
    check(_c.execute("SELECT count(*) FROM lines WHERE source_id='own' "
                     "AND class='fail'").fetchone()[0] == 0,
          "no contrast set because the fixture has no classified failures")

print("\nriff: technique for a subject the corpus has never seen")
_r = server.HANDLERS["riff"](subject="scheduling a concrete pour with the crew")
check(_r["mechanisms_that_score"] or _r["exemplars"],
      "riff answers even when nothing matches the subject")
check("liked" in _r and "disliked" in _r, "riff carries the register both ways")
check(_r["subject"] == "scheduling a concrete pour with the crew",
      "riff echoes the subject it was given")
_j = json.dumps(_r)
check("credits" in _r, "riff credits its material")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
