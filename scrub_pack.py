#!/usr/bin/env python3
"""One-shot scrub of the shipped mallard pack (2026-08-07).

Why: the pack shipped with material that was never humor corpus —
  - 406 `preference_pairs:*` pairs: general assistant/ops adjudications
    carrying real workplace names, sites and one personal prompt. Public
    since 2026-07-26; this removes them from every future artefact.
  - 100 `eval` lines: engine-output verdicts on tone-test prompts, several
    of which read as private conversation whether or not they were seeded.
  - 32 unrated lines whose attribution claimed "rated by James Barker".

Keeps: joke / candidate / slate_winner / word lines, and the 57
`humor_h2h:*` pairs — the actual humor preference data.

ingest_mallard.py is patched separately so a re-ingest cannot reintroduce
any of this. Run from the repo root; writes backups to the scratch dir.
"""
import json, shutil, sys
from pathlib import Path

PACK = Path("humor_mcp/packs/mallard")
BACKUP = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../pack-backup")
BACKUP.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- lines.jsonl
src = PACK / "lines.jsonl"
shutil.copy2(src, BACKUP / "lines.jsonl.bak")
kept, dropped_eval, fixed_attr = [], 0, 0
for raw in src.read_text(encoding="utf-8").splitlines():
    r = json.loads(raw)
    if r.get("kind") == "eval":
        dropped_eval += 1
        continue
    if r.get("score") is None and "rated by" in (r.get("attribution") or ""):
        r["attribution"] = "Mallard humor engine (generated); unrated"
        fixed_attr += 1
        raw = json.dumps(r, ensure_ascii=False)
    kept.append(raw)
src.write_text("\n".join(kept) + "\n", encoding="utf-8")
print(f"lines: kept {len(kept)}, dropped {dropped_eval} eval, "
      f"re-attributed {fixed_attr} unrated")

# ---------------------------------------------------------------- pairs.jsonl
src = PACK / "pairs.jsonl"
shutil.copy2(src, BACKUP / "pairs.jsonl.bak")
kept, dropped = [], 0
for raw in src.read_text(encoding="utf-8").splitlines():
    r = json.loads(raw)
    if r.get("ext_id", "").startswith("humor_h2h:"):
        kept.append(raw)
    else:
        dropped += 1
src.write_text("\n".join(kept) + "\n", encoding="utf-8")
print(f"pairs: kept {len(kept)} humor_h2h, dropped {dropped}")

# ------------------------------------------------------------------ manifest
mf = PACK / "pack.json"
m = json.loads(mf.read_text(encoding="utf-8"))
m["note"] = ("Copyright James Barker. Released under CC-BY-4.0: use it for "
             "anything, including commercially, with attribution. The lines are "
             "Mallard engine output; the ratings and head-to-head picks are his "
             "own curation, which is the part worth citing.")
mf.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("manifest: note no longer cites eval verdicts")
