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
  humor-mcp lint --no-stock      # typos only, skip the phrasing report
"""
import argparse
import math
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


# Ordinary connective English. A phrase built only from these is a collocation
# every writer uses ("at this point", "i'm pretty sure"), not a borrowed move.
STOPWORDS = {
    "a", "about", "after", "all", "am", "an", "and", "another", "any", "are",
    "as", "at", "back", "be", "been", "before", "being", "but", "by", "can",
    "could", "did", "do", "does", "doing", "done", "down", "each", "even",
    "ever", "every", "for", "from", "get", "gets", "getting", "go", "going",
    "got", "had", "has", "have", "he", "her", "here", "hers", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "like", "make",
    "makes", "me", "more", "most", "much", "my", "never", "no", "not", "now",
    "of", "off", "on", "one", "only", "or", "our", "out", "over", "own", "re",
    "s", "said", "same", "say", "says", "she", "should", "so", "some", "still",
    "such", "sure", "t", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "thing", "things", "think", "this", "those",
    "through", "to", "too", "up", "us", "very", "was", "way", "we", "well",
    "were", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "ll", "ve", "m", "d", "am",
}


def _content(words):
    return [w for w in words if w not in STOPWORDS]


def stock_phrases(rows, min_words=2, max_words=6, min_uses=2):
    """Phrases this corpus reuses across unrelated setups.

    The review's example is "chose violence" — an off-the-shelf meme phrase
    that turns up in a rated line AND verbatim in a different slate's setup.
    That is worth flagging because a stock phrase is borrowed shape rather than
    written shape, and a model told to imitate the register will happily reach
    for it.

    ⚠ This is a REPORT, not a verdict, and deliberately not a schema column.
    Reuse across setups is evidence of a stock phrase; it is not proof, and a
    callback inside one batch is a device rather than a crutch — so phrases are
    only counted once per setup and need to appear under two different ones.

    Ranked so the borrowed cores are actually visible: on the mallard pack
    "chose violence" comes 8th, alongside "emotional support", "vendetta
    against" and "hostage situation". Ranking by recurrence alone put it
    295th behind ordinary collocations.
    """
    seen = {}
    for ctx, text in rows:
        words = re.findall(r"[a-z']+", (text or "").lower())
        here = set()
        for n in range(min_words, max_words + 1):
            for i in range(len(words) - n + 1):
                span = words[i:i + n]
                # A borrowed move carries its own images. Require the phrase to
                # be mostly content words and to start and end on one, which is
                # what separates "chose violence" from "and now i".
                if len(_content(span)) < 2 or span[0] in STOPWORDS \
                        or span[-1] in STOPWORDS:
                    continue
                here.add(" ".join(span))
        for ph in here:
            seen.setdefault(ph, set()).add(ctx or "")
    # Ranking by how OFTEN a phrase recurs puts ordinary English on top: "at
    # this point" recurs more than any borrowed move ever will. What marks a
    # stock phrase is rare words in a reused combination — "violence" is
    # distinctive, "point" is not. So weight each phrase by how unusual its
    # words are in this corpus (plain idf over setups) and let recurrence be
    # the smaller term. Ranking by count alone buried the known example 295th
    # of 850; a finding you cannot see is not a finding.
    docs = [set(re.findall(r"[a-z']+", (t or "").lower())) for _, t in rows]
    n_docs = max(1, len(docs))
    df = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1

    def rarity(phrase):
        content = _content(phrase.split())
        if not content:
            return 0.0
        return sum(math.log(n_docs / (1 + df.get(w, 0))) for w in content) / len(content)

    def score(ph, n):
        return rarity(ph) * math.log(1 + n)

    out = [(ph, len(ctxs)) for ph, ctxs in seen.items() if len(ctxs) >= min_uses]
    # One entry per overlapping family, and it must be the BEST-scoring member,
    # not the longest. Keeping the longest suppressed "chose violence" under
    # "didn't need it and chose violence", whose ordinary words drag its rarity
    # down — so the borrowed core was hidden behind the sentence containing it.
    # A stock phrase is the short distinctive bit; that is what makes it stock.
    out.sort(key=lambda t: -score(*t))
    kept = []
    for ph, n in out:
        if any(ph in k or k in ph for k, _, _ in kept):
            continue
        kept.append((ph, n, round(rarity(ph), 2)))
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="humor-mcp lint", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-score", type=float, default=None,
                    help="only lines scored at least this highly (exemplar risk)")
    ap.add_argument("--source", default=None, help="restrict to one pack id")
    ap.add_argument("--db", default=None, help="database path (default: the usual)")
    ap.add_argument("--no-stock", action="store_true",
                    help="skip the reused-phrasing report")
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

    stock = []
    if not args.no_stock:
        print("reused phrasing (candidate stock phrases)")
        rows = [(r["context"], r["text"]) for r in con.execute(
            f"SELECT context, text FROM lines WHERE {w} "
            f"AND (class IS NULL OR class != 'control')", params)]
        rows += [(r["context"], r["context"]) for r in con.execute(
            f"SELECT DISTINCT context FROM lines WHERE {w} "
            f"AND context IS NOT NULL AND trim(context) != ''", params)]
        stock = stock_phrases(rows)
        for ph, n, rar in stock[:15]:
            print(f"  {n} setups  rarity {rar:>5}  {ph!r}")
        if not stock:
            print("  (none reused across two or more setups)")

    print()
    print(f"{hits} finding(s), {likely} likely and {hits - likely} possibly "
          f"rhetorical ('Knock knock' is the joke, not a typo).")
    if not args.no_stock:
        print(f"{len(stock)} phrase(s) reused across setups, ranked by how unusual "
              f"their words are — evidence of borrowed shape, not a verdict.")
    print("Nothing was changed — the wording is the corpus owner's to judge.")
    return 1 if likely else 0


if __name__ == "__main__":
    raise SystemExit(main())
