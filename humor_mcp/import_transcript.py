#!/usr/bin/env python3
"""
Turn a raw transcript into a corpus pack.

    python import_transcript.py --id tight-five --input set.srt \
        --performer "Your Name" --title "Comedy Cellar, March" --i-own-this

Accepts .srt, .vtt, Whisper .json, and plain .txt (with or without SPEAKER:
labels). Timestamps and cue numbers are stripped.

HOW LINES ARE FOUND
  If the transcript contains laughter or applause markers — [laughter],
  (laughs), *applause* — they are used the way an audio pipeline uses the
  waveform: the text immediately before a marker is the line, and what came
  before that is its context. This is the good case.

  If there are no markers, the transcript is segmented into sentences with a
  rolling context window and nothing is marked as a punchline, because there is
  no signal saying which parts landed. You get material, not judgements, and the
  importer says so.

CREDIT
  --performer is required. This exists because the corpus this tool was built
  from lost its performer names at ingest and they had to be recovered by hand
  from video IDs a year later. Every line gets the performer, the set title and
  the source URL stamped on it individually.

  Transcripts of other people's performances default to NOT redistributable.
  Pass --i-own-this if the material is yours, or --license to set one explicitly.
"""
import argparse, json, re, sys
from pathlib import Path

from . import paths

from ._utf8 import force_utf8
force_utf8()

# Reaction markers, strongest first. The weight is a heuristic stand-in for the
# laugh volume an audio pipeline would measure — treat it as ordering, not
# magnitude.
REACTIONS = [
    (re.compile(r"[\[\(*]\s*(?:big laugh|huge laugh|laughter and applause|"
                r"laughs and applause|applause and cheer\w*)\s*[\]\)*]", re.I), 1.0),
    (re.compile(r"[\[\(*]\s*(?:applause|cheer\w*|clapping|whoop\w*)\s*[\]\)*]", re.I), 0.9),
    (re.compile(r"[\[\(*]\s*(?:audience laugh\w*|laughter)\s*[\]\)*]", re.I), 0.8),
    (re.compile(r"[\[\(*]\s*(?:laughs?|chuckl\w*|giggl\w*|snicker\w*)\s*[\]\)*]", re.I), 0.5),
]
# Stage directions and noises that are not reactions — dropped outright.
NOISE = re.compile(r"[\[\(*]\s*(?:music|sighs?|clears throat|coughs?|silence|pause|"
                   r"inaudible|unintelligible|crosstalk|beep\w*)\s*[\]\)*]", re.I)
# Music is marked with note characters as often as with brackets: "♪ music ♪",
# "♪♪", or a bare ♪ around a lyric. Strip the whole span, not just the notes.
MUSIC = re.compile(r"[♪♫]+[^♪♫\n]*[♪♫]+|[♪♫]+")
# Square brackets and *asterisks* are stage-direction convention, so anything
# left in them after the reaction and noise passes is safe to drop. Parentheses
# are NOT: "(I was not fine)" is an aside, and often the joke. Known parenthetical
# directions — (laughs), (sighs) — are already removed by REACTIONS and NOISE.
ANY_BRACKET = re.compile(r"\[[^\]\n]{0,60}\]|\*[^*\n]{0,60}\*")
# A speaker label, not any capitalised run before a colon. The loose version
# matched "So I said:" and "Here's the thing:", which deleted the first half of
# the line AND invented a performer name that then became its attribution.
SPEAKER = re.compile(
    r"^\s*("
    r"[A-Z][A-Z.'\- ]{1,19}"                            # HOST, DR. SMITH
    r"|[A-Z][a-z]+\.?(?:\s+[A-Z][a-z'\-]+\.?){0,2}"     # Taylor Tomlinson, Mr. Smith
    r"):\s+")
SENT_END = re.compile(r"(?<=[.!?])\s+(?=[\"'A-Z0-9])")
ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|etc|Inc|Ltd)\.$", re.I)


# ------------------------------------------------------------------- readers
# Parsing lives in transcripts.py so the audio importer, which needs the
# timings, and this one, which does not, cannot drift apart.
from . import transcripts  # noqa: E402


def read_srt_vtt(p):
    return [c.text for c in transcripts.read_srt_vtt(p)]


def read_whisper_json(p):
    cues, meta = transcripts.read_json(p)
    return [c.text for c in cues], meta


def read_txt(p):
    return [c.text for c in transcripts.read_txt(p)]


# ------------------------------------------------------------------ segmenting
def sentences(text):
    parts, buf = [], ""
    for chunk in SENT_END.split(text):
        buf = (buf + " " + chunk).strip() if buf else chunk.strip()
        if buf and not ABBREV.search(buf):
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return [s for s in parts if s]


def flatten(chunks):
    """Caption lines -> a flat token stream of (speaker, sentence) and reactions."""
    stream, speaker = [], None
    for c in chunks:
        m = SPEAKER.match(c)
        if m:
            speaker = m.group(1).strip().rstrip(":")
            c = c[m.end():]
        c = NOISE.sub(" ", MUSIC.sub(" ", c))
        # split around reaction markers, keeping them as their own tokens
        pos, pieces = 0, []
        while pos < len(c):
            best = None
            for rx, w in REACTIONS:
                m = rx.search(c, pos)
                if m and (best is None or m.start() < best[0].start()):
                    best = (m, w)
            if not best:
                pieces.append(("text", c[pos:]))
                break
            m, w = best
            pieces.append(("text", c[pos:m.start()]))
            pieces.append(("reaction", w))
            pos = m.end()
        for kind, val in pieces:
            if kind == "reaction":
                stream.append(("reaction", val, speaker))
            else:
                val = ANY_BRACKET.sub(" ", val)
                for s in sentences(re.sub(r"\s+", " ", val).strip()):
                    stream.append(("text", s, speaker))
    return stream


def build_lines(chunks, ctx_n=2, min_chars=12):
    """Reaction-anchored where possible; otherwise a rolling window."""
    stream = flatten(chunks)
    has_reaction = any(k == "reaction" for k, _, _ in stream)
    texts = [(v, sp) for k, v, sp in stream if k == "text"]
    out = []
    if has_reaction:
        idx = [i for i, (k, _, _) in enumerate(stream) if k == "reaction"]
        for i in idx:
            weight = stream[i][1]
            before = [(v, sp) for k, v, sp in stream[:i] if k == "text"]
            if not before:
                continue
            line, sp = before[-1]
            if len(line) < min_chars:
                continue
            ctx = " ".join(v for v, _ in before[-(ctx_n + 1):-1])
            out.append({"text": line, "context": ctx, "kind": "joke",
                        "laugh": weight, "speaker": sp})
    else:
        for i, (line, sp) in enumerate(texts):
            if len(line) < min_chars:
                continue
            ctx = " ".join(v for v, _ in texts[max(0, i - ctx_n):i])
            out.append({"text": line, "context": ctx, "kind": "utterance",
                        "speaker": sp})
    return out, has_reaction


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True)
    ap.add_argument("--input", required=True, nargs="+",
                    help="one or more transcripts; several sets can share a pack")
    ap.add_argument("--performer", required=True,
                    help="who delivered this material — required, no default")
    ap.add_argument("--title", default="", help="the set / episode this came from")
    ap.add_argument("--url", default="", help="where it came from")
    ap.add_argument("--license", default="")
    ap.add_argument("--i-own-this", action="store_true",
                    help="the material is yours; marks the pack shareable CC-BY-4.0 "
                         "unless --license says otherwise")
    ap.add_argument("--context-sentences", type=int, default=2)
    ap.add_argument("--min-chars", type=int, default=12)
    ap.add_argument("--packs-dir", default=None,
                    help="where to write the pack (default: your corpus at "
                         "~/.humor-mcp/packs, not this repo)")
    ap.add_argument("--append", action="store_true",
                    help="add to an existing pack instead of refusing")
    a = ap.parse_args()
    PACKS = Path(a.packs_dir) if a.packs_dir else paths.import_target()

    all_rows, any_reaction, per_file = [], False, []
    for f in a.input:
        p = Path(f)
        if not p.exists():
            sys.exit(f"no such file: {p}")
        meta = {}
        sfx = p.suffix.lower()
        if sfx in (".srt", ".vtt"):
            chunks = read_srt_vtt(p)
        elif sfx == ".json":
            chunks, meta = read_whisper_json(p)
        elif sfx in (".txt", ".md", ""):
            chunks = read_txt(p)
        else:
            sys.exit(f"unsupported transcript type {sfx}; use .srt, .vtt, .json or .txt")
        rows, had = build_lines(chunks, a.context_sentences, a.min_chars)
        any_reaction = any_reaction or had
        title = a.title or meta.get("title") or p.stem
        url = a.url or meta.get("url") or ""
        cred = f'{a.performer} — "{title}"' + (f" ({url})" if url else "")
        for r in rows:
            sp = r.pop("speaker", None)
            r["attribution"] = (f"{sp} (in {cred})" if sp and sp.lower() not in
                                a.performer.lower() else cred)
            r["meta"] = json.dumps({"transcript": p.name, "set": title},
                                   ensure_ascii=False)
        per_file.append((p.name, len(rows), had))
        all_rows += rows

    if not all_rows:
        sys.exit("no usable lines found — is the transcript empty?")

    lic = a.license or ("CC-BY-4.0" if a.i_own_this else "ALL-RIGHTS-RESERVED")
    own = a.i_own_this or bool(a.license)
    d = PACKS / a.id
    if d.exists() and not a.append:
        sys.exit(f"{d} already exists — pass --append to add to it, or pick another --id.")
    d.mkdir(parents=True, exist_ok=True)

    mode = "a" if (a.append and (d / "lines.jsonl").exists()) else "w"
    with (d / "lines.jsonl").open(mode, encoding="utf-8") as fh:
        for r in all_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    existing = {}
    if a.append and (d / "pack.json").exists():
        existing = json.loads((d / "pack.json").read_text(encoding="utf-8"))
    authors = existing.get("authors") or a.performer
    if a.performer not in authors:
        authors += f"; {a.performer}"
    (d / "pack.json").write_text(json.dumps({
        "id": a.id,
        "title": existing.get("title") or (a.title or f"{a.performer} — transcripts"),
        "authors": authors, "url": a.url or existing.get("url", ""),
        "license": existing.get("license", lic) if a.append else lic,
        "citation": "", "redistributable": own, "commercial_use": own,
        "attribution_required": True, "license_verified": own,
        "note": ("Transcribed from performance audio. " + (
            "Owned by the uploader." if own else
            "Third-party material transcribed for private study — NOT redistributable; "
            "the exporter will refuse it.")),
        "files": ["lines.jsonl"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {d} - {len(all_rows)} lines")
    for name, n, had in per_file:
        print(f"  {name}: {n} lines  ({'reaction-anchored' if had else 'no markers'})")
    print(f"  credit: {a.performer} / {lic}"
          f"{'' if own else '   [local only - not redistributable]'}")
    if not any_reaction:
        print("\n  NOTE: no laughter or applause markers were found, so nothing is\n"
              "  marked as a punchline — these are utterances, not judged material.\n"
              "  A transcript that keeps its [laughter] cues gives much better lines.")
    print("\nnow run:  python build.py")


if __name__ == "__main__":
    main()
