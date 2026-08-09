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
        # check_same_thread=False: sqlite's guard is about which thread USES a
        # connection, not about concurrency. Over stdio there is one thread, so
        # it never mattered; over HTTP each request runs on its own worker and
        # the lazily-created connection would belong to whichever one arrived
        # first. Both transports serialize all access (stdio by nature,
        # http_server under one dispatch lock), which is the property the
        # guard exists to enforce.
        _con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                               check_same_thread=False)
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
    # The full credit block used to sit on every row: ~300 identical bytes per
    # line, which on a breakdown-less result was a third to a half of the whole
    # payload, and tool results are spent straight out of the caller's context.
    # Rows now name their source and `credits` carries one block per source.
    d["source"] = r["source_id"]
    if "origin_id" in r.keys() and r["origin_id"] is not None:
        # Reachable by id, so say why it is not turning up in ranked answers.
        d["duplicate_of"] = r["origin_id"]
    return d


def with_credits(res, rows):
    """One credit block per source actually present in the payload."""
    res["credits"] = {sid: credit_for(sid)
                      for sid in sorted({r["source_id"] for r in rows})}
    return res


def counted(res, rows, total):
    """`count` was rows-returned, which reads as 'this is all there is'."""
    res["returned"] = len(rows)
    res["count"] = len(rows)          # kept: consumers already read this
    if total is not None:
        res["total"] = total
    return res


def total_matching(sql_count, args):
    try:
        return db().execute(sql_count, args).fetchone()[0]
    except Exception:              # a count must never sink the real answer
        return None


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
        # A typo'd pack silently returning count:0 reads as "no data exists",
        # which sends the caller away wrong. Unknown must be loud.
        known = [r["id"] for r in db().execute("SELECT id FROM sources").fetchall()]
        if source not in known:
            raise ValueError(f"unknown pack {source!r}; loaded: {', '.join(sorted(known))}")
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
    check_kind(kind)
    sf, params = src_filter(include_restricted, source, include_hidden, alias="l")
    extra, ex = [], []
    if kind:
        extra.append("l.kind = ?"); ex.append(kind)
    if min_score is not None:
        extra.append("l.score >= ?"); ex.append(min_score)
    if whole_lines:
        extra.append("whole_line(l.text) = 1")
    cc, _cp = canonical_clause(kind, alias="l")
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    extra_sql += cc

    note = None
    kc, kp = ("", [])          # word-scale guard; browse-only, but the total
                               # query below reads it on both paths
    if m:
        sql = (f"SELECT l.* FROM lines_fts f JOIN lines l ON l.id = f.rowid "
               f"WHERE lines_fts MATCH ? AND {sf}"
               f"{extra_sql} ORDER BY bm25(lines_fts) LIMIT ?")
        args = [m] + params + ex + [limit]
    else:
        # Browse mode ranks by score, and the word lexicon's 0-5 scale would
        # put single words above every actual joke — the same collision
        # top_rated already guards against. A query is fine (bm25 ranks, and
        # searching "therapy" should find the word); ranking is not. The
        # exclusion (and its hint) applies only when word rows are actually in
        # reach: a hint about material that does not exist is a wild-goose
        # chase, and this corpus may be somebody else's, without a lexicon.
        if not kind and db().execute(
                f"SELECT 1 FROM lines l WHERE {sf} AND l.kind='word' LIMIT 1",
                params).fetchone():
            kc, kp = kind_clause(None, alias="l")
            note = WORD_HINT
        sql = (f"SELECT l.* FROM lines l WHERE {sf}{extra_sql}{kc} "
               f"ORDER BY l.score DESC NULLS LAST, l.id LIMIT ?")
        args = params + ex + kp + [limit]
    rows = db().execute(sql, args).fetchall()
    if m:
        total = total_matching(
            f"SELECT count(*) FROM lines_fts f JOIN lines l ON l.id = f.rowid "
            f"WHERE lines_fts MATCH ? AND {sf}{extra_sql}", [m] + params + ex)
    else:
        total = total_matching(
            f"SELECT count(*) FROM lines l WHERE {sf}{extra_sql}{kc}",
            params + ex + kp)
    res = counted({"results": [row_out(r) for r in rows]}, rows, total)
    res["order"] = ("relevance (bm25)" if m else "score desc, then id")
    with_credits(res, rows)
    if not rows:
        # An empty result has more than one cause, and they call for opposite
        # moves (rewrite the query vs relax a filter vs name kind='word').
        # Blaming the wrong one sends the caller away — separate them, the way
        # rating_note does for top_rated.
        if m and ex:
            base = db().execute(
                f"SELECT count(*) c FROM lines_fts f JOIN lines l ON l.id = f.rowid "
                f"WHERE lines_fts MATCH ? AND {sf}", [m] + params).fetchone()["c"]
            res["note"] = (
                f"The query matches {base} line(s), but the "
                f"kind/min_score/whole_lines filters excluded them all — relax "
                f"the filters, not the query." if base else
                "The query matched nothing — the corpus holds material, this "
                "search just found none of it.")
        elif m:
            res["note"] = ("The query matched nothing — the corpus holds "
                           "material, this search just found none of it.")
        elif note:
            res["note"] = ("Nothing matches these filters except single-word "
                           "lexicon entries, which rank on a different 0-5 "
                           "scale and are excluded here; pass kind='word' "
                           "for those.")
        else:
            res["note"] = "Nothing matches these filters."
    elif note:
        res["note"] = note
    return capped(res, was_capped, asked)


# Scoring scales differ by kind: jokes are rated 0-3, the funny-word lexicon 0-5.
# Mixing them ranks single words above every joke, so word entries are held back
# unless a caller asks for kind='word' explicitly.
OFF_SCALE_KINDS = ("word",)


def known_kinds():
    return [r["kind"] for r in db().execute(
        "SELECT DISTINCT kind FROM lines WHERE kind IS NOT NULL "
        "ORDER BY kind").fetchall()]


def check_kind(kind):
    """An unknown kind used to come back as an ordinary empty result, which
    reads as 'the corpus has none of those' rather than 'that kind does not
    exist'. Unknown source already errors loudly; kind now matches it."""
    if kind and kind not in known_kinds():
        raise ValueError(f"unknown kind {kind!r}; available: "
                         f"{', '.join(known_kinds())}")


def kind_clause(kind, alias=None):
    col = f"{alias}.kind" if alias else "kind"
    if kind:
        return f" AND {col} = ?", [kind]
    return (f" AND ({col} IS NULL OR {col} NOT IN "
            f"({','.join('?' * len(OFF_SCALE_KINDS))}))", list(OFF_SCALE_KINDS))


def canonical_clause(kind, alias=None):
    """Hide rows that merely repeat another row's text.

    build.py links a repeated line to the copy worth reading (see
    link_duplicates). Without this, one line occupies two slots in every ranked
    answer — `taste_profile(n=10)` was returning six distinct lines because a
    quarter of the mallard ratings are stored twice.

    Same precedent as the word-scale guard above: the filter lifts the moment a
    caller names a kind, because `kind='candidate'` is an explicit request for
    exactly the rows this hides. Nothing is deleted and no rating is lost — the
    duplicate keeps its score and stays reachable by id via get_line.
    """
    if kind:
        return "", []
    col = f"{alias}.origin_id" if alias else "origin_id"
    return f" AND {col} IS NULL", []


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
    check_kind(kind)
    sf, params = src_filter(include_restricted, source, include_hidden)
    kc, kp = kind_clause(kind)
    cc, _ = canonical_clause(kind)
    rows = db().execute(
        f"SELECT * FROM lines WHERE {sf}{kc}{cc} AND score IS NOT NULL AND score >= ? "
        f"ORDER BY score DESC, laugh DESC NULLS LAST LIMIT ?",
        params + kp + [min_score, limit]).fetchall()
    total = total_matching(
        f"SELECT count(*) FROM lines WHERE {sf}{kc}{cc} AND score IS NOT NULL "
        f"AND score >= ?", params + kp + [min_score])
    res = counted({"results": [row_out(r) for r in rows],
                   "order": "score desc, then laugh desc",
                   "note": rating_note(rows, sf, params, kc, kp, kind, min_score)},
                  rows, total)
    return capped(with_credits(res, rows), was_capped, asked)


def t_taste_profile(rater=None, n=12, kind=None, include_restricted=False,
                    include_hidden=False):
    """What this corpus's human actually liked vs didn't — the calibration signal."""
    n, _ = clamp(n, 12)
    check_kind(kind)
    sf, params = src_filter(include_restricted, None, include_hidden)
    kc, kp = kind_clause(kind)
    cc, _ = canonical_clause(kind)
    sf = sf + kc + cc
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
    res = {
        "raters": [r["rater"] for r in db().execute(
            "SELECT DISTINCT rater FROM lines WHERE rater IS NOT NULL").fetchall()],
        "score_distribution": {str(r["score"]): r["c"] for r in dist},
        "liked": [row_out(r) for r in hi],
        "disliked": [row_out(r) for r in lo],
        "how_to_use": "These are absolute human ratings. Match the LIKED register; "
                      "the DISLIKED set is the failure mode to avoid, not merely "
                      "weaker material.",
    }
    return with_credits(res, list(hi) + list(lo))


def t_breakdown(query="", limit=5, include_restricted=False, include_hidden=False):
    asked, m = limit, fts_query(query)
    limit, was_capped = clamp(limit, 5)
    sf, params = src_filter(include_restricted, None, include_hidden, alias="l")
    if (query or "").strip() and not m:
        # "?!?" or an emoji strips to zero tokens. Browsing HERE would present
        # the top-scored breakdowns as if they matched a query they have
        # nothing to do with — browse is for an empty query, asked for as such.
        return {"count": 0, "results": [],
                "note": "the query contains no searchable words (punctuation "
                        "and symbols are stripped); pass an empty query to "
                        "browse by score"}
    if m:
        rows = db().execute(
            f"SELECT l.* FROM lines_fts f JOIN lines l ON l.id=f.rowid "
            f"WHERE lines_fts MATCH ? AND {sf} "
            f"AND l.breakdown IS NOT NULL AND l.breakdown != '' "
            f"ORDER BY bm25(lines_fts) LIMIT ?", [m] + params + [limit]).fetchall()
    else:
        # The parameter doc has always promised "empty = browse by score";
        # until now an empty query returned a bare count:0, which reads as
        # "no breakdowns exist" — the opposite of what browsing should say.
        # Score ranking brings the same word-lexicon scale collision browse
        # has in t_search, so the same exclusion applies.
        kc, kp = kind_clause(None, alias="l")
        cc, _ = canonical_clause(None, alias="l")
        rows = db().execute(
            f"SELECT l.* FROM lines l WHERE {sf}{kc}{cc} "
            f"AND l.breakdown IS NOT NULL AND l.breakdown != '' "
            f"ORDER BY l.score DESC NULLS LAST, l.id LIMIT ?",
            params + kp + [limit]).fetchall()
    res = counted({"results": [row_out(r) for r in rows],
                   "order": "relevance (bm25)" if m else "score desc, then id"},
                  rows, None)
    with_credits(res, rows)
    if not rows:
        res["note"] = ("No line matching this query carries a breakdown." if m
                       else "No line in reach carries a breakdown.")
    return capped(res, was_capped, asked)


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
        p = {"id": r["id"], "prompt": r["prompt"], "chosen": r["chosen"],
             "rejected": r["rejected"], "source": r["source_id"]}
        # Nothing joined pairs to lines before, so a caller could not reach the
        # rating or the breakdown behind either side of a preference. Resolve
        # by exact text, and only when it is unambiguous — a text appearing
        # twice would make the id a guess.
        for side in ("chosen", "rejected"):
            hit = db().execute(
                "SELECT id FROM lines WHERE text = ? AND source_id = ? "
                "AND origin_id IS NULL LIMIT 2", (r[side], r["source_id"])).fetchall()
            if len(hit) == 1:
                p[f"{side}_id"] = hit[0]["id"]
        # The pack stores a template slug here (`mundane_complaint`), not the
        # setup text — a consumer reading it as a prompt gets a category name.
        if r["prompt"] and " " not in (r["prompt"] or "") and "_" in r["prompt"]:
            p["prompt_is_template_slug"] = True
        out.append(p)
    res = counted({"results": out,
                   "order": "match order" if query else "random sample"},
                  out, total_matching(
                      f"SELECT count(*) FROM pairs WHERE {sf}" +
                      (" AND (chosen LIKE ? OR prompt LIKE ?)" if query else ""),
                      params + ([f"%{query}%"] * 2 if query else [])))
    with_credits(res, rows)
    res = capped(res, was_capped, asked)
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


def t_get_line(id=None):
    """Fetch one line by id — the join every other tool was missing.

    preference_pairs now hands back chosen_id/rejected_id, ranked reads hand
    back ids, and until this existed none of them could be followed anywhere.
    Deliberately not gated: an id is only obtainable from a result the caller
    was already allowed to see, and a restricted pack's rows never leave the
    build in the first place.
    """
    try:
        lid = int(id)
    except (TypeError, ValueError):
        raise ValueError("id must be an integer line id")
    r = db().execute("SELECT * FROM lines WHERE id = ?", (lid,)).fetchone()
    if not r:
        hi = db().execute("SELECT max(id) m FROM lines").fetchone()["m"]
        raise ValueError(f"no line with id {lid} (ids run 1..{hi})")
    out = row_out(r)
    if r["origin_id"] is not None:
        canon = db().execute("SELECT * FROM lines WHERE id = ?",
                             (r["origin_id"],)).fetchone()
        if canon:
            out["duplicate_of_line"] = row_out(canon)
            out["note"] = ("This row repeats another line's text and is held "
                           "out of ranked results; the copy under "
                           "`duplicate_of_line` is the one carrying the analysis.")
    return with_credits({"line": out}, [r])


RIFF_MECHANISM_NOTE = (
    "The corpus almost certainly holds no joke about your subject — it is one "
    "person's rated material, not a topic index. What travels is the MECHANISM: "
    "which moves this rater scored well, what the register sounds like, and what "
    "the same idea looks like when it is flattened. Build the joke about the "
    "caller's own subject using these; do not paste the exemplars back."
)


def t_riff(subject="", n=6, include_restricted=False, include_hidden=False):
    """Technique for an arbitrary subject, rather than a lookup that misses.

    Asked about a subject the corpus has never seen — someone's job, their
    colleagues, whatever they are in the middle of talking about — search
    honestly returns nothing, which reads as "this corpus is no use". It is
    the wrong question of it. The rated material cannot supply a joke about a
    crew schedule; it can supply the moves that earned a 3 and the failures
    that earned a 0, and let the caller's own model do the writing.

    Read-only and stateless, like every other tool here: `subject` is used to
    bias exemplar selection within this one call and is never stored, logged or
    added to the corpus.
    """
    n, _ = clamp(n, 6)
    sf, params = src_filter(include_restricted, None, include_hidden)
    cc, _ = canonical_clause(None)

    # What actually earns a high score here, counted off the breakdowns rather
    # than asserted — method and tone are lists, so flatten them.
    rows = db().execute(
        f"SELECT score, breakdown FROM lines WHERE {sf}{cc} "
        f"AND breakdown IS NOT NULL AND breakdown != '' AND score IS NOT NULL",
        params).fetchall()
    methods, tones = {}, {}
    for r in rows:
        try:
            b = json.loads(r["breakdown"])
        except Exception:
            continue
        for field, bag in (("method", methods), ("tone", tones)):
            v = b.get(field)
            for item in (v if isinstance(v, list) else [v] if v else []):
                s = bag.setdefault(str(item), {"n": 0, "score_sum": 0.0})
                s["n"] += 1
                s["score_sum"] += r["score"]

    def ranked(bag):
        def at_least(k):
            out = [{"name": name, "n": v["n"],
                    "mean_score": round(v["score_sum"] / v["n"], 2)}
                   for name, v in bag.items() if v["n"] >= k]
            out.sort(key=lambda d: (-d["mean_score"], -d["n"]))
            return out[:8]
        # Three occurrences before a mechanism is worth naming — but a small or
        # someone else's corpus would then get an empty list, which is the very
        # "this tool has nothing for you" answer riff exists to avoid. Relax the
        # bar rather than return nothing; `n` is reported so the caller can see
        # how thin the evidence is.
        return at_least(3) or at_least(1)

    # Minimal contrastive pairs: the same idea sharp, then flattened. These are
    # the strongest signal in the corpus for showing a model what to avoid.
    prs = db().execute(
        f"SELECT * FROM pairs WHERE {sf} ORDER BY RANDOM() LIMIT ?",
        params + [n]).fetchall()

    taste = t_taste_profile(n=n, include_restricted=include_restricted,
                            include_hidden=include_hidden)
    # A subject the corpus has never heard of is the EXPECTED case here, so a
    # topic miss must not empty the answer — that is the behaviour riff exists
    # to replace. Fall back to the corpus's best material and say which it is.
    ex = exemplars_for(subject or None, n, include_restricted, include_hidden)
    topic_hit = bool(ex) if subject else None
    if subject and not ex:
        ex = exemplars_for(None, n, include_restricted, include_hidden)

    res = {
        "subject": subject or None,
        "how_to_use": RIFF_MECHANISM_NOTE,
        "mechanisms_that_score": ranked(methods),
        "register": ranked(tones),
        "sharp_vs_flat": [{"sharper": p["chosen"], "flatter": p["rejected"],
                           "category": p["prompt"]} for p in prs],
        "liked": [r["text"] for r in taste["liked"]],
        "disliked": [r["text"] for r in taste["disliked"]],
        "exemplars": [{"text": r["text"], "context": r.get("context"),
                       "score": r.get("score"), "basis": basis_of(r)} for r in ex],
        "attribution_block": taste.get("attribution_block") or ATTRIB_FALLBACK,
    }
    if topic_hit is False:
        res["topic_matched"] = False
        res["note"] = ("Nothing in the corpus is about this subject, which is "
                       "expected and not a failure — this is one person's rated "
                       "material, not a topic index. The exemplars below are the "
                       "corpus's best work on its own subjects; take the mechanism "
                       "and the register from them, not the topic.")
    elif topic_hit:
        res["topic_matched"] = True
    return with_credits(res, list(prs))


ATTRIB_FALLBACK = ("Style reference drawn from the corpus credited in `credits`; "
                   "reproduce that credit alongside any quoted or closely "
                   "paraphrased line.")


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
    used = sorted({r["source"] for r in ex["results"]} |
                  {r["source"] for r in liked["liked"]})
    creds = []
    for sid in used:
        s = db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if s:
            creds.append(f"{s['title']} — {s['authors']} ({s['license']})")
    out = {
        "exemplars": [{"text": r["text"], "context": r.get("context"),
                       "score": r.get("score"), "basis": basis_of(r),
                       "source": r["source"]}
                      for r in ex["results"]],
        "credits": {sid: credit_for(sid) for sid in used},
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
    # One shared string used to carry preference_pairs' own exception, so on
    # that tool it described itself in the third person. Each tool states its
    # own browse behaviour; PARAM_DOC_BY_TOOL overrides where they differ.
    "query": "words to search for; empty = browse by score",
    "source": "restrict to one pack id — naming a pack overrides both gates below",
    # Fallback only — the advertised schema lists the kinds actually loaded
    # (see kind_doc); a static enum here once promised kinds with zero rows,
    # and an agent that filtered on one read the empty result as "no data".
    "kind": "joke / pun / candidate / slate_winner / eval / utterance / word",
    "min_score": "only lines a human scored at least this highly",
    "whole_lines": ("drop transcript offcuts that start or stop mid-sentence: "
                    "keeps lines that open with a capital, digit or quote AND "
                    "end in terminal punctuation (.!?…\"')] etc.); heuristic"),
    "limit": "how many results (capped at %d)" % MAX_LIMIT,
    "n": "how many examples per side (capped at %d)" % MAX_LIMIT,
    "rater": "restrict to one rater's judgements",
    "topic": "focus the exemplars on a subject; omitted = highest-rated overall",
    "include_restricted": ("include packs whose LICENCE bars redistribution "
                           "(local reference only)"),
    "include_hidden": ("include packs the corpus owner marked off-rubric — their "
                       "licence is fine, they were judged unrepresentative"),
    "id": "line id, as returned by any other tool (see chosen_id / rejected_id)",
    "subject": ("what you want jokes about — your own topic, in your own words. "
                "Used only to bias exemplar choice within this call; never stored"),
}

# Where a tool's parameter genuinely differs from the shared description.
PARAM_DOC_BY_TOOL = {
    "preference_pairs": {
        "query": "words to search for; empty = a fresh random sample",
    },
    "breakdown": {
        "query": "words to search for; empty = browse the top-scored breakdowns",
    },
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
     "matching a query — how the joke is built, not just the text. An empty query "
     "browses the top-scored breakdowns.", t_breakdown),

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

    ("get_line", "Fetch one line by id — follow a chosen_id/rejected_id from "
     "preference_pairs, or any id from a search result, to its rating and "
     "breakdown.", t_get_line),

    ("riff", "Ask for the TECHNIQUE behind this corpus's humor so you can apply it "
     "to a subject the corpus has never seen — the caller's own topic. Returns the "
     "mechanisms that score well, the register, sharp-vs-flat contrastive pairs and "
     "exemplars. Use this instead of search when the subject is the user's own life, "
     "work or conversation: search will honestly return nothing, because this is one "
     "person's rated material, not a topic index. Read-only; the subject is never "
     "stored.", t_riff),
]
HANDLERS = {name: fn for name, _, fn in TOOLS}


JSON_TYPE = {bool: "boolean", int: "integer", float: "number", str: "string"}


def kind_doc():
    """Advertise the kinds this corpus actually holds, not a hopeful enum.

    Reach matters as much as existence: a kind that lives only in a restricted
    or hidden pack is invisible to a default query, and advertising it would
    recreate the very failure this function exists to fix — an agent filters
    on the promised kind, gets count:0, and concludes there is no data.
    """
    try:
        sf, params = src_filter(False, None, False)
        kinds = [r["kind"] for r in db().execute(
            f"SELECT DISTINCT kind FROM lines WHERE {sf} AND kind IS NOT NULL "
            f"ORDER BY kind", params)]
        return " / ".join(kinds) if kinds else PARAM_DOC["kind"]
    except Exception:                     # no db yet: fall back, never fail listing
        return PARAM_DOC["kind"]


def param_schema(fn, tool_name=None):
    """JSON Schema properties + required list, derived from the signature."""
    overrides = PARAM_DOC_BY_TOOL.get(tool_name or "", {})
    props, required = {}, []
    for name, prm in inspect.signature(fn).parameters.items():
        if name not in PARAM_DOC:
            raise RuntimeError(
                f"{fn.__name__}({name}=...) has no entry in PARAM_DOC. Every tool "
                "parameter must be documented — that is what keeps the advertised "
                "schema and the implementation from drifting.")
        desc = overrides.get(name, PARAM_DOC[name])
        spec = {"description": kind_doc() if name == "kind" else desc}
        if prm.default is inspect.Parameter.empty:
            required.append(name)
            spec["type"] = "string"
        else:
            spec["type"] = JSON_TYPE.get(type(prm.default), "string")
            if prm.default is None:                     # optional filter
                spec["type"] = "number" if name == "min_score" else "string"
            else:
                spec["default"] = prm.default
        # min_score was `number` on search_humor but `integer` on top_rated,
        # purely because their defaults differ in type. It is one quantity.
        if name == "min_score":
            spec["type"] = "number"
            spec["description"] += " (inclusive: 2.5 means score >= 2.5)"
        props[name] = spec
    return props, required


def tool_defs():
    out = []
    for name, desc, fn in TOOLS:
        props, required = param_schema(fn, name)
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        out.append({"name": name, "description": desc, "inputSchema": schema})
    return out


def handle(req):
    m = req.get("method")
    if m == "initialize":
        pv = (req.get("params") or {}).get("protocolVersion") or "2024-11-05"
        from . import __version__
        return {"protocolVersion": pv, "capabilities": {"tools": {}},
                "serverInfo": {"name": "humor-mcp", "version": __version__}}
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
