#!/usr/bin/env python3
"""Report surface defects in the corpus text. Reports only — never rewrites.

A typo in a 3.0-rated line is not a small thing: `style_pack` hands the top
scores to a model as the register to imitate, so "were were" travels into
everything written from it, and the same goes for a doubled word inside a
`chosen` exemplar in a preference pair.

This deliberately does not fix anything. The ratings and the wording are the
corpus owner's judgement, and a script that quietly edits the material it was
asked to check is the last thing a hand-rated corpus needs. It prints ids; the
owner decides.

  humor-mcp lint                 # everything, default thresholds
  humor-mcp lint --min-score 3   # only the lines used as exemplars
  humor-mcp lint --source mallard
"""
import argparse
import re
import sys

from .paths import db_path

# A doubled word, case-insensitively, on a word boundary: "the the", "were
# were", "perpet perpetual" is NOT caught by this (different words) so the
# stem check below handles that separately.
DOUBLED = re.compile(r"\b(\w+)(\s+)(\1)\b", re.IGNORECASE)

# A truncated word immediately followed by the word it was starting: the
# "perpet perpetual" shape, which a plain doubled-word check misses.
STUTTER = re.compile(r"\b(\w{3,})(\s+)(\1\w+)\b", re.IGNORECASE)

# TTS markup that consumers have to strip and that breaks full-text search.
MARKUP = re.compile(r"</?\s*spoken\s*>", re.IGNORECASE)


# Repetition is a device, not always a defect. "Knock knock" is the joke.
# Anything here is reported at lower confidence rather than hidden, because a
# corpus this small can afford a human glance and a silent filter cannot be
# argued with.
RHETORICAL = {"knock", "no", "bye", "yeah", "ha", "hey", "so", "very", "really",
              "who", "what", "now", "go", "run", "help", "wait", "ok", "okay"}


def findings(text):
    out = []
    for m in STUTTER.finditer(text or ""):
        # A truncated word followed by its completion is a transcription
        # artefact; there is no reading of it that is deliberate.
        out.append(("stutter", m.group(0), "likely"))
    for m in DOUBLED.finditer(text or ""):
        word = m.group(1).lower()
        out.append(("doubled-word", m.group(0),
                    "rhetorical?" if word in RHETORICAL else "likely"))
    for m in MARKUP.finditer(text or ""):
        out.append(("tts-markup", m.group(0), "likely"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="humor-mcp lint", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-score", type=float, default=None,
                    help="only lines scored at least this highly (exemplar risk)")
    ap.add_argument("--source", default=None, help="restrict to one pack id")
    ap.add_argument("--db", default=None, help="database path (default: the usual)")
    args = ap.parse_args(argv)

    import sqlite3
    con = sqlite3.connect(f"file:{args.db or db_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    where, params = ["1=1"], []
    if args.source:
        where.append("source_id = ?"); params.append(args.source)
    if args.min_score is not None:
        where.append("score >= ?"); params.append(args.min_score)
    w = " AND ".join(where)

    hits = likely = 0
    print("lines")
    for r in con.execute(f"SELECT id, source_id, score, text FROM lines "
                         f"WHERE {w} ORDER BY id", params):
        for kind, frag, conf in findings(r["text"]):
            hits += 1
            likely += conf == "likely"
            score = "unrated" if r["score"] is None else f"score {r['score']}"
            print(f"  line {r['id']:>6}  {r['source_id']:<10} {score:<10} "
                  f"{kind:<12} {conf:<12} {frag!r}")

    print("preference pairs")
    pw, pp = (["1=1"], [])
    if args.source:
        pw.append("source_id = ?"); pp.append(args.source)
    for r in con.execute(f"SELECT id, source_id, chosen, rejected FROM pairs "
                         f"WHERE {' AND '.join(pw)} ORDER BY id", pp):
        for side in ("chosen", "rejected"):
            for kind, frag, conf in findings(r[side]):
                hits += 1
                likely += conf == "likely"
                # A defect in `chosen` is the one that matters: that is the
                # side held up as the better answer.
                mark = "CHOSEN" if side == "chosen" else "rejected"
                print(f"  pair {r['id']:>6}  {r['source_id']:<10} {mark:<10} "
                      f"{kind:<12} {conf:<12} {frag!r}")

    print()
    print(f"{hits} finding(s), {likely} likely and {hits - likely} possibly "
          f"rhetorical ('Knock knock' is the joke, not a typo).")
    print("Nothing was changed — the wording is the corpus owner's to judge.")
    return 1 if likely else 0


if __name__ == "__main__":
    raise SystemExit(main())
