#!/usr/bin/env python3
"""
Build humor.db from packs/.

    python build.py                 # build every pack found
    python build.py --only mallard cup
    python build.py --export share/ # copy out only the packs that may be redistributed

A pack is a directory under packs/ containing pack.json plus lines.jsonl and/or
pairs.jsonl. To add your own corpus, make a directory, write a pack.json, drop in
a lines.jsonl, and re-run. Nothing else needs to change.
"""
import argparse, json, os, shutil, sqlite3, sys
from pathlib import Path

from . import paths  # noqa: E402
DB = paths.db_path()

from ._utf8 import force_utf8
force_utf8()

# ---------------------------------------------------------------- pack model
# A closed field-type set with a value predicate per type, borrowed from
# data-ui. The point is that a manifest fails at AUTHOR time, loudly, rather
# than being coerced into something the author did not mean: `"redistributable":
# "false"` is a non-empty string, so bool() made it True and the exporter
# shipped material whose manifest said three times not to.
def _is_bool(v):
    return isinstance(v, bool)


def _is_text(v):
    return isinstance(v, str)


def _is_strlist(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


FIELD_TYPES = {
    "bool": (_is_bool, 'a real boolean — true/false, not "true"/"false"'),
    "text": (_is_text, "a string"),
    "strlist": (_is_strlist, "a list of strings"),
}

# name: (type, required, default)
PACK_FIELDS = {
    "id":                   ("text", True, None),
    "title":                ("text", True, None),
    "authors":              ("text", True, None),
    "license":              ("text", True, None),
    "url":                  ("text", False, ""),
    "citation":             ("text", False, ""),
    "note":                 ("text", False, ""),
    "hidden_reason":        ("text", False, ""),
    "redistributable":      ("bool", False, False),
    "commercial_use":       ("bool", False, False),
    "attribution_required": ("bool", False, True),
    "license_verified":     ("bool", False, False),
    "default_hidden":       ("bool", False, False),
    "custom_license":       ("bool", False, False),
    "files":                ("strlist", False, []),
}

PLACEHOLDER_LICENSES = {"UNSET", "UNKNOWN", "UNVERIFIED", "TBD", ""}
NONCOMMERCIAL_LICENSES = {"CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-NC-ND-4.0"}
KNOWN_LICENSES = NONCOMMERCIAL_LICENSES | PLACEHOLDER_LICENSES | {
    "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-ND-4.0",
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
    "GPL-3.0", "LGPL-3.0", "MPL-2.0",
    "PUBLIC-DOMAIN", "ALL-RIGHTS-RESERVED",
}


def validate_pack(meta, where, problems):
    """Append every problem found, never stop at the first.

    A validator that dies on the first fault makes the author peel errors off
    one rebuild at a time.
    """
    def bad(msg):
        problems.append(f"{where}: {msg}")

    for key in meta:
        if key not in PACK_FIELDS:
            near = [k for k in PACK_FIELDS if k.startswith(key[:4])]
            bad(f"unknown field {key!r}" + (f" — did you mean {near[0]!r}?" if near
                                            else " — it would be silently ignored"))
    for key, (ftype, required, _default) in PACK_FIELDS.items():
        if key not in meta:
            if required:
                bad(f"missing required field {key!r}")
            continue
        pred, want = FIELD_TYPES[ftype]
        if not pred(meta[key]):
            bad(f"{key!r} must be {want}, got {meta[key]!r}")
        elif required and ftype == "text" and not meta[key].strip():
            bad(f"{key!r} is required and cannot be empty")

    lic = meta.get("license")
    if isinstance(lic, str):
        norm = lic.strip().upper()
        known = {k.upper() for k in KNOWN_LICENSES}
        if norm not in known and not meta.get("custom_license"):
            bad(f"licence {lic!r} is not a recognised identifier. Fix the typo, or "
                f"set \"custom_license\": true to state that you meant it.")
        if norm in {p.upper() for p in PLACEHOLDER_LICENSES} and meta.get("license_verified"):
            bad(f"licence is the placeholder {lic!r} but license_verified is true — "
                "a placeholder cannot have been verified")
        if norm == "ALL-RIGHTS-RESERVED" and meta.get("redistributable"):
            bad("licence is ALL-RIGHTS-RESERVED but redistributable is true")
        if norm in {n.upper() for n in NONCOMMERCIAL_LICENSES} and meta.get("commercial_use"):
            bad(f"{lic} forbids commercial use but commercial_use is true")
    if meta.get("default_hidden") and not (meta.get("hidden_reason") or "").strip():
        bad("default_hidden is true but hidden_reason is empty — record why, or the "
            "reason lives only in someone's memory")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  authors TEXT NOT NULL,
  url TEXT,
  license TEXT NOT NULL,
  citation TEXT,
  redistributable INTEGER NOT NULL DEFAULT 0,
  commercial_use INTEGER NOT NULL DEFAULT 0,
  attribution_required INTEGER NOT NULL DEFAULT 1,
  license_verified INTEGER NOT NULL DEFAULT 0,
  -- Separate from the licence gate on purpose: "you may not share this" and
  -- "this is off-rubric for what I'm building" are different objections and
  -- must not share a switch.
  default_hidden INTEGER NOT NULL DEFAULT 0,
  hidden_reason TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  ext_id TEXT,
  text TEXT NOT NULL,
  context TEXT,
  kind TEXT,
  score REAL,
  laugh REAL,
  rater TEXT,
  note TEXT,
  tags TEXT,
  breakdown TEXT,
  attribution TEXT,
  meta TEXT,
  -- Set on a row that merely repeats another row's text, pointing at the copy
  -- worth reading. NULL means "this row is the canonical one". Ranked reads
  -- filter on it, so the same line stops occupying two slots; the duplicate
  -- keeps its own rating and stays reachable by id and by explicit kind.
  origin_id INTEGER REFERENCES lines(id)
);
CREATE INDEX IF NOT EXISTS lines_src ON lines(source_id);
CREATE INDEX IF NOT EXISTS lines_score ON lines(score);
CREATE INDEX IF NOT EXISTS lines_kind ON lines(kind);
CREATE INDEX IF NOT EXISTS lines_origin ON lines(origin_id);

CREATE TABLE IF NOT EXISTS pairs (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  ext_id TEXT,
  prompt TEXT,
  chosen TEXT NOT NULL,
  rejected TEXT NOT NULL,
  weight REAL,
  attribution TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS pairs_src ON pairs(source_id);

CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts
  USING fts5(text, context, note, tags, content='lines', content_rowid='id');
"""

def link_duplicates(con):
    """Point repeated texts at the one copy worth reading. Returns (groups, rows).

    The same line can be stored twice: once analysed and once bare — in the
    mallard pack every rated `joke` also exists as a rated `candidate` with the
    identical score, 256 of them, a quarter of every rating in the corpus. Both
    are real rows and both keep their score; what is wrong is letting one line
    occupy two slots in a ranked answer, which is why `taste_profile(n=10)` was
    returning six distinct lines.

    ⚠ Never link on text alone. Two performers can say the same words, and
    merging them would credit one with the other's line — the rule the pack
    importers already follow. So a group is only linked when it cannot be two
    different people or two different judgements:

      * more than one distinct non-empty attribution -> leave every row alone
        (that is the two-performers case the rule exists for)
      * more than one distinct score -> leave every row alone, because one of
        the ratings would vanish from ranked reads and a disagreement about a
        line is a fact worth seeing, not a duplicate worth hiding

    An attribution present on one copy and absent on the other is NOT a
    conflict — that is how the mallard pack stores it, and treating it as one
    linked nothing at all on the first run.

    Canonical = the row carrying a breakdown (the analysed copy is the useful
    one), tie-broken by lowest id so a rebuild always picks the same winner.
    """
    # This connection has no row_factory, so unpack positionally.
    rows = con.execute(
        "SELECT id, source_id, text, coalesce(attribution,''), score, "
        "       (breakdown IS NOT NULL AND breakdown != '') "
        "FROM lines ORDER BY id").fetchall()
    groups = {}
    for rid, src, text, attr, score, analysed in rows:
        groups.setdefault((src, text), []).append((rid, attr, score, analysed))

    n_groups = n_rows = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        attrs = {a for _, a, _, _ in members if a}
        scores = {s for _, _, s, _ in members if s is not None}
        if len(attrs) > 1 or len(scores) > 1:
            continue
        canonical = min(members, key=lambda m: (0 if m[3] else 1, m[0]))
        dupes = [rid for rid, _, _, _ in members if rid != canonical[0]]
        con.executemany("UPDATE lines SET origin_id=? WHERE id=?",
                        [(canonical[0], i) for i in dupes])
        n_groups += 1
        n_rows += len(dupes)
    return n_groups, n_rows


LINE_COLS = ["source_id", "ext_id", "text", "context", "kind", "score", "laugh",
             "rater", "note", "tags", "breakdown", "attribution", "meta"]
PAIR_COLS = ["source_id", "ext_id", "prompt", "chosen", "rejected", "weight",
             "attribution", "meta"]


def read_pack(d, problems=None):
    mf = d / "pack.json"
    if not mf.exists():
        return None
    try:
        meta = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        (problems if problems is not None else []).append(f"{mf}: not valid JSON — {e}")
        return None
    if not isinstance(meta, dict):
        (problems if problems is not None else []).append(
            f"{mf}: must be a JSON object")
        return None
    local = []
    validate_pack(meta, str(mf), local)
    if problems is None:
        if local:
            raise SystemExit("\n".join(["pack.json is invalid:"] +
                                       [f"  - {p}" for p in local]))
    else:
        problems += local
    # every declared field now has its declared type, so defaults are safe
    for key, (_t, _req, default) in PACK_FIELDS.items():
        meta.setdefault(key, default)
    return meta


def read_all_packs(only=None):
    """Read and validate every pack across every corpus directory.

    Reports ALL problems together, and lets a later directory (your own corpus)
    shadow a bundled pack that claims the same id.
    """
    problems, found = [], {}
    for root in paths.pack_dirs():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            meta = read_pack(d, problems)
            if meta is None:
                continue
            if only and meta.get("id") not in only:
                continue
            pid = meta.get("id")
            if pid in found:
                print(f"  note: {pid!r} in {root} shadows the one in "
                      f"{found[pid][0].parent}")
            found[pid] = (d, meta)
    if problems:
        raise SystemExit("\n".join(
            [f"{len(problems)} problem(s) in the corpus model — nothing was built:"]
            + [f"  - {p}" for p in problems]))
    return [found[k] for k in sorted(found)]


def jsonl(p):
    if not p.exists():
        return
    for ln in p.open(encoding="utf-8"):
        ln = ln.strip()
        if ln:
            yield json.loads(ln)


DROP = """
DROP TABLE IF EXISTS lines_fts;
DROP TABLE IF EXISTS lines;
DROP TABLE IF EXISTS pairs;
DROP TABLE IF EXISTS sources;
"""


def outgoing(new_ids):
    """Packs the database already holds that this build will not put back.

    A pack is not kept by being in the old database — every table is dropped and
    rebuilt from whatever packs are readable right now, so a pack survives only
    by being read again. Two ordinary situations therefore destroy data with no
    prompt at all: building from a checkout that lacks a pack your corpus has (a
    fresh clone, a second worktree), and `--only`, which rebuilds the WHOLE
    database from just the named packs. Both look like a normal build. Name what
    is about to leave, before it leaves.
    """
    if not DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT s.id, (SELECT count(*) FROM lines l WHERE l.source_id = s.id) "
            "FROM sources s ORDER BY s.id").fetchall()
        con.close()
    except sqlite3.Error:
        return []            # no schema yet, or unreadable: nothing to lose
    return [(i, n) for i, n in rows if i not in new_ids]


def build(only=None):
    # Rebuild IN PLACE rather than deleting the file. A running MCP server holds
    # an open handle, and Windows refuses to unlink an open file — which broke
    # every rebuild made while the server was live, i.e. the normal case. WAL
    # already allows one writer alongside readers, so dropping and recreating
    # the tables needs no deletion at all.
    packs = read_all_packs(only)          # validates everything before touching the db
    if not packs:
        print("no packs found. Corpus directories searched:")
        for d in paths.candidate_dirs():
            print(f"  {d}" + ("" if d.is_dir() else "   [does not exist]"))
        print(f"\nPut a pack in {paths.import_target()}, or import one:\n"
              "  humor-mcp import-corpus --id my-sets --input jokes.txt \\\n"
              '      --title "My jokes" --authors "Your Name" --license CC-BY-4.0')
        return
    # The database lives beside your corpus, which on a fresh install does not
    # exist yet — without this, the first command anyone runs is an opaque
    # "unable to open database file" from sqlite.
    DB.parent.mkdir(parents=True, exist_ok=True)
    going = outgoing({m["id"] for _, m in packs})
    if going:
        print(f"!! this build REMOVES {len(going)} pack(s) already in {DB}:")
        for i, n in going:
            print(f"     {i}  ({n} lines)")
        print("   Nothing in this build supplies them. Packs were read from:")
        for d in paths.pack_dirs():
            print(f"     {d}")
        print("   --only rebuilds the whole database from just the ids you name."
              "\n   Set HUMOR_DB to build somewhere else if that is not what you"
              " meant.\n")
    con = sqlite3.connect(DB)
    con.executescript(DROP)
    con.executescript(SCHEMA)

    total_l = total_p = 0
    for d, meta in packs:
        # validate_pack has already guaranteed these are real booleans, so int()
        # means what it says — no bool("false") -> True coercion left anywhere
        con.execute(
            "INSERT OR REPLACE INTO sources (id,title,authors,url,license,citation,"
            "redistributable,commercial_use,attribution_required,license_verified,"
            "default_hidden,hidden_reason,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (meta["id"], meta["title"], meta["authors"], meta["url"],
             meta["license"], meta["citation"],
             int(meta["redistributable"]), int(meta["commercial_use"]),
             int(meta["attribution_required"]), int(meta["license_verified"]),
             int(meta["default_hidden"]), meta["hidden_reason"], meta["note"]))

        nl = np = 0
        for r in jsonl(d / "lines.jsonl"):
            if not (r.get("text") or "").strip():
                continue
            r["source_id"] = meta["id"]
            con.execute(f"INSERT INTO lines ({','.join(LINE_COLS)}) "
                        f"VALUES ({','.join('?' * len(LINE_COLS))})",
                        [r.get(c) for c in LINE_COLS])
            nl += 1
        for r in jsonl(d / "pairs.jsonl"):
            if not (r.get("chosen") or "").strip():
                continue
            r["source_id"] = meta["id"]
            con.execute(f"INSERT INTO pairs ({','.join(PAIR_COLS)}) "
                        f"VALUES ({','.join('?' * len(PAIR_COLS))})",
                        [r.get(c) for c in PAIR_COLS])
            np += 1
        flag = "" if meta["license_verified"] else "  [license UNVERIFIED]"
        if meta["default_hidden"]:
            flag += "  [hidden by default]"
        red = "shareable" if meta["redistributable"] else "LOCAL ONLY"
        print(f"  {meta['id']:10s} lines={nl:6d} pairs={np:6d}  "
              f"{meta['license']:20s} {red}{flag}")
        total_l += nl
        total_p += np

    dup_groups, dup_rows = link_duplicates(con)
    if dup_groups:
        print(f"  {'linked':10s} {dup_rows} row(s) in {dup_groups} duplicate "
              f"group(s) point at a canonical copy (ranked reads show each once)")

    con.execute("INSERT INTO lines_fts(rowid,text,context,note,tags) "
                "SELECT id,text,coalesce(context,''),coalesce(note,''),coalesce(tags,'') "
                "FROM lines")
    con.commit()
    try:
        con.execute("VACUUM")
    except sqlite3.OperationalError as e:
        # A reader mid-query blocks VACUUM. The build is already committed and
        # correct; only the reclaimed free pages are missed.
        print(f"  (skipped VACUUM: {e})")
    con.close()
    size = DB.stat().st_size / 1e6
    print(f"\nbuilt {DB}  ({size:.1f} MB)  lines={total_l}  pairs={total_p}")


def export(dest):
    """Copy out only packs whose license actually permits redistribution."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    kept, refused = [], []
    for d, meta in read_all_packs():
        lic = meta["license"].strip().upper()
        why = None
        if lic in {p.upper() for p in PLACEHOLDER_LICENSES}:
            why = f"license is {lic or 'empty'} — declare one in pack.json first"
        elif not meta["redistributable"]:
            why = "not redistributable"
        elif not meta["license_verified"]:
            why = "license unverified"
        if why:
            refused.append((meta["id"], why))
        else:
            shutil.copytree(d, dest / d.name, dirs_exist_ok=True)
            kept.append(meta)

    creditlines = ["# Credits\n",
                   "Every line in this corpus belongs to whoever wrote it. "
                   "Packs included here:\n"]
    for m in kept:
        creditlines.append(f"\n## {m['title']}\n")
        creditlines.append(f"- **Authors:** {m['authors']}\n")
        if m.get("url"):
            creditlines.append(f"- **Source:** {m['url']}\n")
        creditlines.append(f"- **License:** {m['license']}"
                           f"{' (non-commercial use only)' if not m.get('commercial_use') else ''}\n")
        if m.get("citation"):
            creditlines.append(f"- **Cite as:**\n\n```\n{m['citation']}\n```\n")
        if m.get("note"):
            creditlines.append(f"- {m['note']}\n")
    (dest / "CREDITS.md").write_text("".join(creditlines), encoding="utf-8")

    print(f"exported {len(kept)} pack(s) -> {dest}")
    for pid, why in refused:
        print(f"  REFUSED {pid}: {why}")
    print(f"wrote {dest / 'CREDITS.md'}")


def main():
    ap = argparse.ArgumentParser(
        prog="humor-mcp build",
        description="Compile packs into the corpus database.")
    ap.add_argument("--only", nargs="*", metavar="ID",
                    help="build just these pack ids")
    ap.add_argument("--export", metavar="DIR",
                    help="copy out only the packs whose licence permits it, "
                         "with a CREDITS.md, instead of building")
    a = ap.parse_args()
    if a.export:
        export(a.export)
    else:
        build(set(a.only) if a.only else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
