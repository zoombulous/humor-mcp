#!/usr/bin/env python3
"""The contrastive set: same setup, one that landed and one that didn't.

The review's sharpest point was that the breakdown schema scores surprisal,
sharpness and connector quality but never scores whether the joke's frame holds
together — and that the rater's own free-text verdicts already say so, e.g. "if
it is raining and he is watering the grass, it doesn't make sense to say he is
winning a negotiation with the sky". That is frame-internal logic, and nothing
in the schema captures it.

Scoring it needs a judgement no script can derive, so this does not invent one.
What it does is assemble the exact rows worth judging, which the corpus can now
identify by itself: a batch that contains BOTH a line that won it and a line
that genuinely missed — controls excluded, because a seeded helpful non-answer
is not a joke that failed. On the mallard pack that is 34 batches and 89 lines.

Pairing them matters more than listing them. A failure is only informative
beside the winner from the same setup: same premise, same rater, same moment,
different outcome.

  humor-mcp contrast              # read them
  humor-mcp contrast --jsonl OUT  # emit the rows ready to annotate

⚠ Nothing here writes to the corpus. The JSONL is a worksheet: it carries the
ids, the existing ratings and empty fields to fill, so that annotating is a
filling-in job rather than an assembly job — and so a half-finished pass is
never mistaken for corpus data.
"""
import argparse
import json
import sys

from .paths import db_path


def slates(con, source=None):
    """Batches holding both a winner and a real failure, newest id first."""
    where, params = ["slate_id IS NOT NULL"], []
    if source:
        where.append("source_id = ?"); params.append(source)
    w = " AND ".join(where)
    ids = [r[0] for r in con.execute(
        f"SELECT slate_id FROM lines WHERE {w} GROUP BY slate_id "
        f"HAVING sum(class='winner') > 0 AND sum(class='fail') > 0 "
        f"ORDER BY slate_id", params)]
    out = []
    for sid in ids:
        rows = con.execute(
            f"SELECT id, class, score, context, text, note FROM lines "
            f"WHERE {w} AND slate_id = ? AND class IN ('winner','fail') "
            f"ORDER BY CASE class WHEN 'winner' THEN 0 ELSE 1 END, id",
            params + [sid]).fetchall()
        if rows:
            out.append((sid, rows))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="humor-mcp contrast", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=None, help="restrict to one pack id")
    ap.add_argument("--db", default=None, help="database path")
    ap.add_argument("--jsonl", default=None,
                    help="write an annotation worksheet here instead of printing")
    args = ap.parse_args(argv)

    import sqlite3
    con = sqlite3.connect(f"file:{args.db or db_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    found = slates(con, args.source)

    if not found:
        print("No batch holds both a winner and a real failure — nothing to "
              "contrast. (Needs slate_id and class, which build.py derives.)",
              file=sys.stderr)
        return 1

    n_rows = sum(len(rows) for _, rows in found)

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for sid, rows in found:
                for r in rows:
                    fh.write(json.dumps({
                        "id": r["id"],
                        "slate_id": sid,
                        "class": r["class"],
                        "score": r["score"],
                        "setup": r["context"],
                        "text": r["text"],
                        "existing_note": r["note"],
                        # To fill in. Named for what they ask, not for a scale:
                        # does the joke's own logic hold, and is the phrasing
                        # borrowed rather than written.
                        "frame_coherence": None,
                        "stock_phrase": None,
                    }, ensure_ascii=False) + "\n")
        print(f"{n_rows} line(s) across {len(found)} batch(es) -> {args.jsonl}")
        print("frame_coherence and stock_phrase are empty and stay empty until "
              "a human fills them; nothing was written to the corpus.")
        return 0

    for sid, rows in found:
        setup = next((r["context"] for r in rows if r["context"]), "")
        print(f"\nbatch {sid} — {setup}")
        for r in rows:
            mark = "WON " if r["class"] == "winner" else "miss"
            print(f"  {mark} {r['score']:<4} [{r['id']}] {r['text']}")
            if r["note"]:
                print(f"       rater's note: {r['note']}")
    print(f"\n{n_rows} line(s) across {len(found)} batch(es). Each miss is worth "
          f"reading beside the winner from its own setup — same premise, same "
          f"rater, different outcome.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
