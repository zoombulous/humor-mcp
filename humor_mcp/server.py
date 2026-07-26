#!/usr/bin/env python3
"""
humor-mcp — an MCP server over a local, credited humor corpus.

stdlib only, stdio transport, read-only. No network, no database server.
Point HUMOR_DB at a different .db to serve a different corpus.

Every result carries the credit for the line it came from. Tools refuse to
return material from a pack whose license bars it unless you ask explicitly.
"""
import inspect, json, os, re, sqlite3, sys
from pathlib import Path

from . import paths

DB_PATH = paths.db_path()

# MCP is UTF-8 on the wire; Windows consoles are not.
from ._utf8 import force_utf8
force_utf8()

_con = None


TERMINAL = ".!?\"')]…"


def whole_line(t):
    """Does this read as a complete utterance rather than a transcript offcut?

    Auto-segmented stand-up arrives cut mid-sentence — "Couples therapy is",
    "when it doesn't work out, because then you just have" — and those land in
    search results looking like jokes that failed rather than halves of one.
    Capitalised start plus terminal punctuation separates them cheaply.

    It is a heuristic and it is wrong on tidy corpora that omit a final stop
    (several CUP puns do), which is exactly why nothing filters on it unless a
    caller asks. Keep it that way: a default that silently drops real lines from
    somebody else's pack would be a worse bug than the one it fixes.
    """
    t = (t or "").strip()
    return 1 if t and (t[0].isupper() or t[0].isdigit() or t[0] in "\"'“‘") \
        and t[-1] in TERMINAL else 0


def db():
    global _con
    if _con is None:
        if not DB_PATH.exists():
            raise RuntimeError(
                f"no corpus at {DB_PATH}. Run `python build.py` first, or set HUMOR_DB.")
        _con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        _con.row_factory = sqlite3.Row
        _con.create_function("whole_line", 1, whole_line, deterministic=True)
    return _con


def fts_query(q):
    """Make an arbitrary user string safe for FTS5 MATCH."""
    toks = re.findall(r"[\w']+", q or "")
    if not toks:
        return None
    return " AND ".join('"' + t.replace('"', '') + '"' for t in toks)


_src_cache = {}


def credit_for(source_id):
    if source_id not in _src_cache:
        r = db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        _src_cache[source_id] = dict(r) if r else {}
    s = _src_cache[source_id]
    if not s:
        return {"source": source_id}
    c = {"source": s["id"], "title": s["title"], "authors": s["authors"],
         "license": s["license"]}
    if s["url"]:
        c["url"] = s["url"]
    if not s["license_verified"]:
        c["warning"] = "license NOT verified — do not republish without checking"
    if not s["commercial_use"]:
        c["commercial_use"] = "prohibited"
    if s["default_hidden"]:
        c["off_rubric"] = s["hidden_reason"] or "hidden from default results"
    return c


def row_out(r):
    d = {"id": r["id"], "text": r["text"]}
    for k in ("context", "kind", "score", "laugh", "rater", "note", "attribution"):
        if k in r.keys() and r[k] not in (None, ""):
            d[k] = r[k]
    for k in ("tags", "breakdown"):
        if k in r.keys() and r[k]:
            try:
                d[k] = json.loads(r[k])
            except Exception:
                d[k] = r[k]
    d["credit"] = credit_for(r["source_id"])
    return d


# MCP results land directly in the calling model's context window. An
# unclamped limit returned 5.4 MB of JSON in one response, which floods the
# caller rather than helping it.
MAX_LIMIT = 200


def clamp(n, default):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default, False
    if n < 1:
        return default, False
    return min(n, MAX_LIMIT), n > MAX_LIMIT


def capped(res, was_capped, asked):
    if was_capped:
        res["limit_capped"] = (f"asked for {asked}, capped at {MAX_LIMIT} — tool "
                               "results go straight into the caller's context")
    return res


def src_filter(include_restricted, source, include_hidden=False, alias=None):
    """Returns (sql_fragment, params).

    Two independent gates, deliberately not merged:
      include_restricted -> packs whose LICENCE bars redistribution
      include_hidden     -> packs the corpus owner marked off-rubric
    Naming a pack explicitly via `source` overrides both; if you ask for it by
    name you have said what you want.

    `alias` qualifies the column for queries that join, e.g. alias="l" emits
    "l.source_id". Callers used to patch that in with str.replace on the
    returned fragment, which would have silently mangled any future column
    whose name contained "source_id".
    """
    col = f"{alias}.source_id" if alias else "source_id"
    where, params = [], []
    if source:
        where.append(f"{col} = ?")
        params.append(source)
        return " AND ".join(where), params

    bad = []
    if not include_restricted:
        bad.append("redistributable=0")
    if not include_hidden:
        bad.append("default_hidden=1")
    if bad:
        excluded = [r["id"] for r in db().execute(
            f"SELECT id FROM sources WHERE {' OR '.join(bad)}").fetchall()]
        if excluded:
            where.append(f"{col} NOT IN ({','.join('?' * len(excluded))})")
            params += excluded
    return (" AND ".join(where) if where else "1=1"), params


# ------------------------------------------------------------------ the tools
def t_search(query="", source=None, kind=None, min_score=None, limit=10,
             whole_lines=False, include_restricted=False, include_hidden=False):
    asked, m = limit, fts_query(query)
    limit, was_capped = clamp(limit, 10)
    sf, params = src_filter(include_restricted, source, include_hidden, alias="l")
    extra, ex = [], []
    if kind:
        extra.append("l.kind = ?"); ex.append(kind)
    if min_score is not None:
        extra.append("l.score >= ?"); ex.append(min_score)
    if whole_lines:
        extra.append("whole_line(l.text) = 1")
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""

    if m:
        sql = (f"SELECT l.* FROM lines_fts f JOIN lines l ON l.id = f.rowid "
               f"WHERE lines_fts MATCH ? AND {sf}"
               f"{extra_sql} ORDER BY bm25(lines_fts) LIMIT ?")
        args = [m] + params + ex + [limit]
    else:
        sql = (f"SELECT l.* FROM lines l WHERE {sf}"
               f"{extra_sql} ORDER BY l.score DESC NULLS LAST, l.id LIMIT ?")
        args = params + ex + [limit]
    rows = db().execute(sql, args).fetchall()
    return capped({"count": len(rows), "results": [row_out(r) for r in rows]},
                  was_capped, asked)


# Scoring scales differ by kind: jokes are rated 0-3, the funny-word lexicon 0-5.
# Mixing them ranks single words above every joke, so word entries are held back
# unless a caller asks for kind='word' explicitly.
OFF_SCALE_KINDS = ("word",)


def kind_clause(kind, alias=None):
    col = f"{alias}.kind" if alias else "kind"
    if kind:
        return f" AND {col} = ?", [kind]
    return (f" AND ({col} IS NULL OR {col} NOT IN "
            f"({','.join('?' * len(OFF_SCALE_KINDS))}))", list(OFF_SCALE_KINDS))


WORD_HINT = ("Single-word lexicon entries are excluded (different 0-5 scale); "
             "pass kind='word' for those.")


def rating_note(found, sf, params, kc, kp, kind, min_score):
    """Why did top_rated come back empty?

    This tool ranks by human score, so `score >= ?` silently drops every
    unrated line — in SQL `NULL >= 0` is NULL, never true. Most of a corpus is
    unrated (engine output nobody sat down and graded), and whole kinds can be
    unrated end to end: slate_winner lines are best-of-N picks, eval lines are
    verdicts. For those, NO min_score will ever return a row.

    An empty list alone reads as "the corpus has nothing", which is wrong and
    sends the caller away. Separate the two causes and name them: the bar was
    too high (lower it), or this slice carries no ratings at all (go to
    search_humor, which does not rank and will happily show them).
    """
    if found:
        return None if kind else WORD_HINT
    r = db().execute(f"SELECT count(*) n, count(score) rated FROM lines "
                     f"WHERE {sf}{kc}", params + kp).fetchone()
    where = f"kind={kind!r}" if kind else "this selection"
    reach = f"search_humor(kind={kind!r})" if kind else "search_humor"
    if not r["n"]:
        return f"Nothing in the corpus matches {where}."
    if not r["rated"]:
        return (f"{r['n']} line(s) match {where}, but none carry a human score, "
                f"so top_rated cannot rank them at any min_score — this is a "
                f"property of the material, not of your threshold. "
                f"Read them with {reach} instead.")
    return (f"{r['rated']} of {r['n']} line(s) matching {where} are human-rated, "
            f"but none reach min_score={min_score}. Lower min_score.")


def t_top_rated(limit=20, source=None, min_score=2, kind=None,
                include_restricted=False, include_hidden=False):
    asked = limit
    limit, was_capped = clamp(limit, 20)
    sf, params = src_filter(include_restricted, source, include_hidden)
    kc, kp = kind_clause(kind)
    rows = db().execute(
        f"SELECT * FROM lines WHERE {sf}{kc} AND score IS NOT NULL AND score >= ? "
        f"ORDER BY score DESC, laugh DESC NULLS LAST LIMIT ?",
        params + kp + [min_score, limit]).fetchall()
    return capped({"count": len(rows), "results": [row_out(r) for r in rows],
                   "note": rating_note(rows, sf, params, kc, kp, kind, min_score)},
                  was_capped, asked)


def t_taste_profile(rater=None, n=12, kind=None, include_restricted=False,
                    include_hidden=False):
    """What this corpus's human actually liked vs didn't — the calibration signal."""
    n, _ = clamp(n, 12)
    sf, params = src_filter(include_restricted, None, include_hidden)
    kc, kp = kind_clause(kind)
    sf = sf + kc
    params = params + kp
    rq = " AND rater = ?" if rater else " AND rater IS NOT NULL"
    rp = [rater] if rater else []
    hi = db().execute(f"SELECT * FROM lines WHERE {sf}{rq} AND score IS NOT NULL "
                      f"ORDER BY score DESC, id LIMIT ?", params + rp + [n]).fetchall()
    lo = db().execute(f"SELECT * FROM lines WHERE {sf}{rq} AND score IS NOT NULL "
                      f"ORDER BY score ASC, id LIMIT ?", params + rp + [n]).fetchall()
    dist = db().execute(f"SELECT score, count(*) c FROM lines WHERE {sf}{rq} "
                        f"AND score IS NOT NULL GROUP BY score ORDER BY score DESC",
                        params + rp).fetchall()
    return {
        "raters": [r["rater"] for r in db().execute(
            "SELECT DISTINCT rater FROM lines WHERE rater IS NOT NULL").fetchall()],
        "score_distribution": {str(r["score"]): r["c"] for r in dist},
        "liked": [row_out(r) for r in hi],
        "disliked": [row_out(r) for r in lo],
        "how_to_use": "These are absolute human ratings. Match the LIKED register; "
                      "the DISLIKED set is the failure mode to avoid, not merely "
                      "weaker material.",
    }


def t_breakdown(query, limit=5, include_restricted=False, include_hidden=False):
    m = fts_query(query)
    limit, _ = clamp(limit, 5)
    sf, params = src_filter(include_restricted, None, include_hidden, alias="l")
    if not m:
        return {"count": 0, "results": []}
    rows = db().execute(
        f"SELECT l.* FROM lines_fts f JOIN lines l ON l.id=f.rowid "
        f"WHERE lines_fts MATCH ? AND {sf} "
        f"AND l.breakdown IS NOT NULL AND l.breakdown != '' "
        f"ORDER BY bm25(lines_fts) LIMIT ?", [m] + params + [limit]).fetchall()
    return {"count": len(rows), "results": [row_out(r) for r in rows]}


def t_pairs(query=None, source=None, limit=10, include_restricted=False,
            include_hidden=False):
    asked = limit
    limit, was_capped = clamp(limit, 10)
    sf, params = src_filter(include_restricted, source, include_hidden)
    if query:
        like = f"%{query}%"
        rows = db().execute(
            f"SELECT * FROM pairs WHERE {sf} AND (chosen LIKE ? OR prompt LIKE ?) LIMIT ?",
            params + [like, like, limit]).fetchall()
    else:
        # Browsing with no query: sample, don't return the same head of the table
        # every call. Insertion order here is pack order, so a plain LIMIT would
        # only ever show whichever pack was built first.
        rows = db().execute(f"SELECT * FROM pairs WHERE {sf} ORDER BY RANDOM() LIMIT ?",
                            params + [limit]).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "prompt": r["prompt"], "chosen": r["chosen"],
                    "rejected": r["rejected"], "credit": credit_for(r["source_id"])})
    res = capped({"count": len(out), "results": out}, was_capped, asked)
    if not include_hidden and not source:
        held = db().execute("SELECT s.id, count(p.id) c FROM sources s "
                            "JOIN pairs p ON p.source_id=s.id "
                            "WHERE s.default_hidden=1 GROUP BY s.id").fetchall()
        if held:
            res["withheld"] = {
                "packs": {r["id"]: r["c"] for r in held},
                "why": "marked off-rubric by the corpus owner; pass "
                       "include_hidden=true or name the pack via `source` to see them",
            }
    return res


def t_sources():
    rows = db().execute("SELECT * FROM sources ORDER BY id").fetchall()
    out = []
    for s in rows:
        nl = db().execute("SELECT count(*) c FROM lines WHERE source_id=?",
                          (s["id"],)).fetchone()["c"]
        npr = db().execute("SELECT count(*) c FROM pairs WHERE source_id=?",
                           (s["id"],)).fetchone()["c"]
        out.append({
            "id": s["id"], "title": s["title"], "authors": s["authors"],
            "url": s["url"], "license": s["license"],
            "license_verified": bool(s["license_verified"]),
            "redistributable": bool(s["redistributable"]),
            "commercial_use": bool(s["commercial_use"]),
            "citation": s["citation"], "note": s["note"],
            "default_hidden": bool(s["default_hidden"]),
            "hidden_reason": s["hidden_reason"] or None,
            "lines": nl, "pairs": npr,
        })
    return {"sources": out,
            "rules": {
                "attribution": "Anything you publish that draws on these lines must "
                               "carry the credit shown here.",
                "redistributable=false": "local reference only — hidden unless "
                                         "include_restricted=true. A licence question.",
                "default_hidden=true": "the licence is fine; the corpus owner judged it "
                                       "off-rubric. Hidden unless include_hidden=true or "
                                       "you name the pack. A relevance question.",
            }}


# Best-of-N winners the engine picked for itself. Nobody scored them, so every
# score-gated query drops them — including, until now, the style brief, which is
# the one place they most belong: they are the most on-register material in the
# corpus. They rest on a different authority than a human rating, so each
# exemplar says which one it rests on rather than passing a machine's pick off
# as a verdict.
PICK_KINDS = ("slate_winner",)


def basis_of(r):
    if r.get("score") is not None:
        return f"human-rated {r['score']}"
    if r.get("kind") in PICK_KINDS:
        return "engine best-of-N pick, unrated"
    return "unrated"


def exemplars_for(topic, n, include_restricted, include_hidden):
    """Exemplars drawn from both authorities, neither crowding the other out.

    Rated lines lead — a human actually ruled on them — but taking them first
    and filling to n would shut the picks out entirely whenever the corpus has
    more than n rated lines, which is the common case. So each authority gets
    reserved slots and whichever pool runs short is backfilled from the other.
    """
    def q(**kw):
        return t_search(query=topic or "", limit=n,
                        include_restricted=include_restricted,
                        include_hidden=include_hidden, **kw)["results"]

    rated = (q(min_score=2) if topic else
             t_top_rated(limit=n, include_restricted=include_restricted,
                         include_hidden=include_hidden)["results"])
    picks = [r for k in PICK_KINDS for r in q(kind=k)]

    half = (n + 1) // 2
    chosen, seen = [], set()
    for r in rated[:half] + picks[:n - half] + rated[half:] + picks[n - half:]:
        if len(chosen) >= n:
            break
        if r["id"] not in seen:
            seen.add(r["id"])
            chosen.append(r)
    return chosen


def t_style_pack(topic=None, n=8, include_restricted=False, include_hidden=False):
    """A compact brief you can paste into a system prompt."""
    n, _ = clamp(n, 8)
    liked = t_taste_profile(n=n, include_restricted=include_restricted,
                            include_hidden=include_hidden)
    ex = {"results": exemplars_for(topic, n, include_restricted, include_hidden)}
    used = sorted({r["credit"]["source"] for r in ex["results"]} |
                  {r["credit"]["source"] for r in liked["liked"]})
    creds = []
    for sid in used:
        s = db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if s:
            creds.append(f"{s['title']} — {s['authors']} ({s['license']})")
    out = {
        "exemplars": [{"text": r["text"], "context": r.get("context"),
                       "score": r.get("score"), "basis": basis_of(r),
                       "credit": r["credit"]}
                      for r in ex["results"]],
    }
    # The exemplars are the only topic-aware part of this brief; liked/disliked
    # are corpus-wide calibration either way. If the topic matched nothing, a
    # silently empty `exemplars` beside a full brief still reads as a valid
    # answer about that topic. Say plainly that it is not one.
    if topic:
        out["topic"] = topic
        out["topic_matched"] = bool(ex["results"])
        if not ex["results"]:
            out["topic_note"] = (
                f"Nothing rated or picked matches {topic!r}, so there are no "
                f"topic exemplars. Everything below is corpus-wide register "
                f"calibration and says nothing about {topic!r} — use it as a "
                f"voice brief, not a topic brief. search_humor({topic!r}) will "
                f"find unrated lines on the subject if any exist.")
    out.update({
        "liked": [r["text"] for r in liked["liked"]],
        "disliked": [r["text"] for r in liked["disliked"]],
        "score_distribution": liked["score_distribution"],
        "attribution_block": "Style reference drawn from:\n" +
                             "\n".join(f"  - {c}" for c in creds),
        "usage": "Use the exemplars for register and rhythm, not for verbatim reuse. "
                 "If any output quotes or closely paraphrases an exemplar, reproduce "
                 "the attribution_block alongside it.",
    })
    return out


def t_stats():
    c = db()
    return {
        "db": str(DB_PATH),
        "size_mb": round(DB_PATH.stat().st_size / 1e6, 1),
        "lines": c.execute("SELECT count(*) c FROM lines").fetchone()["c"],
        "pairs": c.execute("SELECT count(*) c FROM pairs").fetchone()["c"],
        "sources": c.execute("SELECT count(*) c FROM sources").fetchone()["c"],
        "kinds": {r["kind"]: r["c"] for r in c.execute(
            "SELECT coalesce(kind,'?') kind, count(*) c FROM lines GROUP BY kind "
            "ORDER BY c DESC").fetchall()},
        "by_source": {r["source_id"]: r["c"] for r in c.execute(
            "SELECT source_id, count(*) c FROM lines GROUP BY source_id "
            "ORDER BY c DESC").fetchall()},
        "human_rated": c.execute(
            "SELECT count(*) c FROM lines WHERE score IS NOT NULL").fetchone()["c"],
    }


# ------------------------------------------------------------------ the model
# Parameter names, types, defaults and requiredness are read off the FUNCTION
# SIGNATURE; only the prose lives here. Previously each tool was declared twice
# — once as a JSON Schema dict, once as a Python signature — and nothing checked
# they agreed, so every new argument was a two-place edit that could silently
# drift. Anything in a signature but missing here fails at import.
PARAM_DOC = {
    "query": "words to search for; empty = browse by score",
    "source": "restrict to one pack id — naming a pack overrides both gates below",
    "kind": "joke / pun / candidate / slate_winner / eval / utterance / word",
    "min_score": "only lines a human scored at least this highly",
    "whole_lines": ("drop transcript offcuts that start or stop mid-sentence; "
                    "heuristic, and strict about a final full stop"),
    "limit": "how many results (capped at %d)" % MAX_LIMIT,
    "n": "how many examples per side (capped at %d)" % MAX_LIMIT,
    "rater": "restrict to one rater's judgements",
    "topic": "focus the exemplars on a subject; omitted = highest-rated overall",
    "include_restricted": ("include packs whose LICENCE bars redistribution "
                           "(local reference only)"),
    "include_hidden": ("include packs the corpus owner marked off-rubric — their "
                       "licence is fine, they were judged unrepresentative"),
}

TOOLS = [
    ("search_humor", "Full-text search the humor corpus. Returns lines with their "
     "credit attached. Filter by source pack, kind or minimum human score.", t_search),

    ("top_rated", "The highest human-rated lines in the corpus. Single-word lexicon "
     "entries use a different scale and are excluded unless kind='word'.", t_top_rated),

    ("taste_profile", "What this corpus's human rater actually liked versus disliked, "
     "with the score distribution. Use this to calibrate register before writing.",
     t_taste_profile),

    ("breakdown", "Structural breakdowns (mechanism, setup/turn, technique) for lines "
     "matching a query — how the joke is built, not just the text.", t_breakdown),

    ("preference_pairs", "Chosen vs rejected pairs — what won head-to-head and what "
     "lost. Off-rubric packs are withheld by default and reported in `withheld`.",
     t_pairs),

    ("sources", "Every corpus pack loaded: who wrote it, under what license, whether it "
     "may be redistributed or used commercially, and how to cite it.", t_sources),

    ("style_pack", "A compact, paste-ready style brief: exemplars, liked/disliked "
     "calibration, and the attribution block that must travel with any output.",
     t_style_pack),

    ("corpus_stats", "What is loaded: counts by pack and by kind, size on disk.",
     t_stats),
]
HANDLERS = {name: fn for name, _, fn in TOOLS}


JSON_TYPE = {bool: "boolean", int: "integer", float: "number", str: "string"}


def param_schema(fn):
    """JSON Schema properties + required list, derived from the signature."""
    props, required = {}, []
    for name, prm in inspect.signature(fn).parameters.items():
        if name not in PARAM_DOC:
            raise RuntimeError(
                f"{fn.__name__}({name}=...) has no entry in PARAM_DOC. Every tool "
                "parameter must be documented — that is what keeps the advertised "
                "schema and the implementation from drifting.")
        spec = {"description": PARAM_DOC[name]}
        if prm.default is inspect.Parameter.empty:
            required.append(name)
            spec["type"] = "string"
        else:
            spec["type"] = JSON_TYPE.get(type(prm.default), "string")
            if prm.default is None:                     # optional filter
                spec["type"] = "number" if name == "min_score" else "string"
            else:
                spec["default"] = prm.default
        props[name] = spec
    return props, required


def tool_defs():
    out = []
    for name, desc, fn in TOOLS:
        props, required = param_schema(fn)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        out.append({"name": name, "description": desc, "inputSchema": schema})
    return out


def handle(req):
    m = req.get("method")
    if m == "initialize":
        pv = (req.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"protocolVersion": pv, "capabilities": {"tools": {}},
                "serverInfo": {"name": "humor-mcp", "version": "0.1.0"}}
    if m == "tools/list":
        return {"tools": tool_defs()}
    if m == "tools/call":
        p = req.get("params") or {}
        fn = HANDLERS.get(p.get("name"))
        if not fn:
            return {"content": [{"type": "text", "text": f"unknown tool {p.get('name')}"}],
                    "isError": True}
        try:
            res = fn(**(p.get("arguments") or {}))
            return {"content": [{"type": "text",
                                 "text": json.dumps(res, ensure_ascii=False, indent=1)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True}
    if m == "ping":
        return {}
    return None


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except Exception:
            continue
        rid = req.get("id")
        try:
            res = handle(req)
        except Exception as e:
            if rid is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32603, "message": str(e)}}),
                      flush=True)
            continue
        if rid is None:
            continue  # notification
        if res is None:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601,
                                        "message": f"method not found: {req.get('method')}"}}),
                  flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res},
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
