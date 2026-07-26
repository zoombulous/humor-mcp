#!/usr/bin/env python3
"""
Turn your own material into a pack.

    python import_corpus.py --id my-sets --input jokes.txt \
        --title "My tight five" --authors "Your Name" --license CC-BY-4.0

Accepts .txt (one line per entry, blank line separated for multi-line bits),
.csv (needs a text/line/joke column; optional context/score/note columns),
or .jsonl ({"text": ..., optional context/score/note/tags/attribution}).

Credit is not optional: --authors and --license are required, because the whole
point of this server is that every line it hands back can say where it came from.
Use --license UNKNOWN if you genuinely don't know; it will be flagged as
unverified everywhere it surfaces and excluded from any export.
"""
import argparse, csv, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import paths

from _utf8 import force_utf8
force_utf8()

WELL_KNOWN_OPEN = {"CC0-1.0", "CC-BY-4.0", "MIT", "Apache-2.0", "PUBLIC-DOMAIN"}
NONCOMMERCIAL = {"CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-NC-ND-4.0"}


def read_txt(p):
    blocks, cur = [], []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.strip():
            cur.append(ln.rstrip())
        elif cur:
            blocks.append("\n".join(cur)); cur = []
    if cur:
        blocks.append("\n".join(cur))
    return [{"text": b} for b in blocks]


def read_csv(p):
    rows = []
    with p.open(encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            low = {(k or "").strip().lower(): v for k, v in r.items()}
            text = next((low[k] for k in ("text", "line", "joke", "content", "body")
                         if low.get(k)), None)
            if not text:
                continue
            out = {"text": text}
            for src, dst in (("context", "context"), ("setup", "context"),
                             ("score", "score"), ("rating", "score"),
                             ("note", "note"), ("tags", "tags"),
                             ("author", "attribution"), ("attribution", "attribution")):
                if low.get(src):
                    out[dst] = low[src]
            if "score" in out:
                try:
                    out["score"] = float(out["score"])
                except ValueError:
                    del out["score"]
            rows.append(out)
    return rows


def read_jsonl(p):
    rows = []
    for i, ln in enumerate(p.open(encoding="utf-8", errors="replace"), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError as e:
            sys.exit(f"{p}:{i}: not valid JSON — {e}")
        if isinstance(r, str):
            r = {"text": r}
        if not r.get("text"):
            continue
        if isinstance(r.get("tags"), (list, dict)):
            r["tags"] = json.dumps(r["tags"], ensure_ascii=False)
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="short pack id, e.g. my-sets")
    ap.add_argument("--input", required=True, help=".txt, .csv or .jsonl")
    ap.add_argument("--title", required=True)
    ap.add_argument("--authors", required=True,
                    help="who wrote these lines — a person, a group, a dataset")
    ap.add_argument("--license", required=True,
                    help="SPDX id, or UNKNOWN (will be flagged and never exported)")
    ap.add_argument("--url", default="")
    ap.add_argument("--citation", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--kind", default="joke")
    ap.add_argument("--packs-dir", default=None,
                    help="where to write the pack (default: your corpus at "
                         "~/.humor-mcp/packs, not this repo)")
    ap.add_argument("--commercial-use", action="store_true",
                    help="the license permits commercial use")
    ap.add_argument("--redistributable", action="store_true",
                    help="the license permits you to pass the text on to others")
    a = ap.parse_args()
    PACKS = Path(a.packs_dir) if a.packs_dir else paths.import_target()

    src = Path(a.input)
    if not src.exists():
        sys.exit(f"no such file: {src}")
    rows = {".txt": read_txt, ".csv": read_csv, ".jsonl": read_jsonl,
            ".json": read_jsonl}.get(src.suffix.lower(), lambda p: sys.exit(
                f"unsupported input type {src.suffix}; use .txt, .csv or .jsonl"))(src)
    if not rows:
        sys.exit(f"{src}: found no usable rows")

    lic = a.license.strip()
    verified = lic.upper() not in ("UNKNOWN", "UNVERIFIED", "")
    commercial = a.commercial_use or lic in WELL_KNOWN_OPEN
    redistributable = a.redistributable or lic in WELL_KNOWN_OPEN | NONCOMMERCIAL
    if lic in NONCOMMERCIAL and a.commercial_use:
        sys.exit(f"{lic} forbids commercial use — drop --commercial-use.")

    d = PACKS / a.id
    if d.exists():
        sys.exit(f"{d} already exists — pick another --id or delete it first.")
    d.mkdir(parents=True)

    with (d / "lines.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            r.setdefault("kind", a.kind)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    (d / "pack.json").write_text(json.dumps({
        "id": a.id, "title": a.title, "authors": a.authors, "url": a.url,
        "license": lic, "citation": a.citation,
        "redistributable": redistributable, "commercial_use": commercial,
        "attribution_required": True, "license_verified": verified,
        "note": a.note, "files": ["lines.jsonl"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {d} - {len(rows)} lines")
    print(f"  credit: {a.authors} / {lic}"
          f"{'' if verified else '   [UNVERIFIED — will be flagged and never exported]'}")
    print(f"  redistributable={redistributable}  commercial_use={commercial}")
    print("\nnow run:  python build.py")


if __name__ == "__main__":
    main()
