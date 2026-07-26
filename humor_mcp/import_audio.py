#!/usr/bin/env python3
"""
Build a pack from a recording plus a timestamped transcript.

    python import_audio.py --id tight-five --audio set.mp3 --transcript set.srt \
        --performer "Your Name" --title "Comedy Cellar, March" --i-own-this

    python import_audio.py --id tight-five --reactions ast.json \
        --transcript set.srt --performer "Their Name"

WHY BOTH FILES
  The reactions say WHEN the room went and how hard; the transcript says WHAT
  was said. Aligning them gives you lines anchored to real audience response,
  and it works on transcripts with no [laughter] markers at all — which is all
  auto-generated captions.

  Whisper .json, .srt and .vtt all carry timings. A plain .txt does not, so it
  cannot be aligned; use import_transcript.py for that.

WHERE THE REACTIONS COME FROM
  Prefer --reactions: a timeline from a trained classifier, in the simple JSON
  shape documented on load_reactions() below.

  --audio runs the bundled heuristic detector instead, and you should know that
  it DOES NOT WORK on real audience recordings. Scored against 14 minutes of
  real stand-up with per-line ground truth: F1 0.24, 93 detections for 26 actual
  laughs. Its features do not separate — on that recording median spectral
  flatness was 0.058 for laughter and 0.055 for speech. It survives here for
  synthetic and cleanly-separated audio, and as a worked example of the
  interface, not as something to trust.

  This does not transcribe. Nothing here downloads a model or calls a service.
  Bring a transcript from whatever you already use — YouTube's, Whisper's, your
  podcast host's.
"""
import argparse, json, sys
from pathlib import Path

from . import paths

from ._utf8 import force_utf8  # noqa: E402
force_utf8()

from . import transcripts  # noqa: E402


def load_reactions(path):
    """A reaction timeline produced by something that actually works.

    Same shape the bundled detector emits, so anything can produce it — an
    AudioSet classifier, a hand-marked list, another tool's output:

        [{"start": 12.3, "end": 14.1, "kind": "laughter", "strength": 0.6}, ...]

    Only `start` is required. `end` defaults to start + 1s, `kind` to
    "laughter", `strength` to 0.5.
    """
    if not path.exists():
        sys.exit(f"no such file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{path}: not valid JSON — {e}")
    if isinstance(data, dict):
        data = data.get("reactions") or data.get("events") or []
    if not isinstance(data, list):
        sys.exit(f"{path}: expected a JSON list of reactions, got {type(data).__name__}")

    out, problems = [], []
    for i, r in enumerate(data):
        if not isinstance(r, dict) or "start" not in r:
            problems.append(f"  item {i}: needs at least a numeric 'start'")
            continue
        try:
            start = float(r["start"])
            end = float(r.get("end", start + 1.0))
        except (TypeError, ValueError):
            problems.append(f"  item {i}: 'start'/'end' must be numbers")
            continue
        if end <= start:
            end = start + 1.0
        kind = str(r.get("kind") or "laughter")
        try:
            strength = float(r.get("strength", 0.5))
        except (TypeError, ValueError):
            strength = 0.5
        out.append({"start": round(start, 3), "end": round(end, 3),
                    "dur": round(end - start, 3), "kind": kind,
                    "strength": round(max(0.0, min(1.0, strength)), 3)})
    if problems:
        sys.exit(f"{path}: {len(problems)} unusable entr(ies):\n" + "\n".join(problems[:8]))
    if not out:
        sys.exit(f"{path}: no reactions in the file")
    out.sort(key=lambda e: e["start"])
    return out


def align(cues, events, ctx_n=2, lead=0.35, min_chars=12):
    """Attach each reaction to the words that earned it.

    A reaction starts slightly after the line lands, so the line is the last cue
    that began before the reaction did (allowing `lead` seconds of overlap for a
    room that starts going before the sentence finishes).
    """
    timed = [c for c in cues if c.start is not None]
    timed.sort(key=lambda c: c.start)
    rows, used = [], set()
    for ev in events:
        t = ev["start"] + lead
        prior = [i for i, c in enumerate(timed) if c.start is not None and c.start <= t]
        if not prior:
            continue
        i = prior[-1]
        line = timed[i].text.strip()
        if len(line) < min_chars or i in used:
            continue
        used.add(i)
        ctx = " ".join(c.text.strip() for c in timed[max(0, i - ctx_n):i])
        rows.append({
            "text": line, "context": ctx, "kind": "joke",
            "laugh": ev["strength"],
            "meta": {"reaction": ev["kind"], "t_start": timed[i].start,
                     "reaction_at": ev["start"], "reaction_dur": ev["dur"],
                     "detector": "audio"},
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", help="the recording; optional if --reactions is given")
    ap.add_argument("--reactions", metavar="JSON",
                    help="a reaction timeline from a real classifier, instead of "
                         "the bundled heuristic detector (which does not work on "
                         "real audience audio — see the README). JSON list of "
                         '{"start": seconds, "end": seconds, "kind": '
                         '"laughter"|"applause", "strength": 0..1}')
    ap.add_argument("--transcript")
    ap.add_argument("--id")
    ap.add_argument("--performer")
    ap.add_argument("--title", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--license", default="")
    ap.add_argument("--i-own-this", action="store_true")
    ap.add_argument("--packs-dir", default=None,
                    help="where to write the pack (default: your corpus at "
                         "~/.humor-mcp/packs, not this repo)")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print detected reactions and stop; writes nothing")
    ap.add_argument("--context-sentences", type=int, default=2)
    ap.add_argument("--flat-speech-max", type=float, default=0.22,
                    help="raise if a noisy recording reports speech as reactions")
    ap.add_argument("--min-reaction", type=float, default=0.45,
                    help="seconds; ignore bursts shorter than this")
    a = ap.parse_args()
    PACKS = Path(a.packs_dir) if a.packs_dir else paths.import_target()

    if not a.audio and not a.reactions:
        sys.exit("give me --reactions (a timeline from a real classifier) or "
                 "--audio (which uses the bundled heuristic detector — see the "
                 "README for how badly that performs on real audience audio).")

    if a.reactions:
        events, label = load_reactions(Path(a.reactions)), Path(a.reactions).name
        print(f"{label}: {len(events)} reaction(s) supplied")
    else:
        from . import audio_reactions as ar
        audio = Path(a.audio)
        if not audio.exists():
            sys.exit(f"no such file: {audio}")
        try:
            y, sr = ar.load(audio)
        except Exception as e:
            sys.exit(f"could not read {audio.name}: {e}\n"
                     "wav/flac/ogg/mp3 work directly; m4a and aac need converting "
                     "with ffmpeg first.")
        events = ar.detect(y, sr, min_dur=a.min_reaction,
                           flat_speech_max=a.flat_speech_max)
        label = audio.name
        print(f"{audio.name}: {len(y)/sr/60:.1f} min, {len(events)} reaction(s) detected")
        print("  NOTE: the bundled detector scored F1 0.24 against real stand-up "
              "with ground truth.\n  Prefer --reactions from a trained classifier; "
              "see the README.")
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    if kinds:
        print("  " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))

    if a.dry_run:
        for e in events:
            mm, ss = divmod(e["start"], 60)
            print(f"  {int(mm):02d}:{ss:05.2f}  {e['kind']:9s} {e['dur']:5.2f}s  "
                  f"strength={e['strength']:.2f}")
        return
    if not events:
        sys.exit("no reactions detected, so there is nothing to anchor lines to.\n"
                 "Run with --dry-run and check the flatness values; a quiet or "
                 "heavily compressed audience mix may need --flat-speech-max lowered.")

    for req in ("id", "performer", "transcript"):
        if not getattr(a, req.replace("-", "_")):
            sys.exit(f"--{req} is required (use --dry-run to inspect audio only)")

    cues, meta = transcripts.read_any(a.transcript)
    if not transcripts.has_timings(cues):
        sys.exit(f"{a.transcript} has no timestamps, so it cannot be aligned to "
                 "audio.\nUse a .srt, .vtt or Whisper .json — or run "
                 "import_transcript.py, which works from text markers instead.")

    rows = align(cues, events, a.context_sentences)
    if not rows:
        sys.exit("reactions were detected but none lined up with a transcript cue — "
                 "are the audio and transcript from the same recording?")

    title = a.title or meta.get("title") or Path(label).stem
    url = a.url or meta.get("url") or ""
    cred = f'{a.performer} — "{title}"' + (f" ({url})" if url else "")
    for r in rows:
        r["attribution"] = cred
        r["meta"] = json.dumps({**r["meta"], "source": label, "set": title},
                               ensure_ascii=False)

    lic = a.license or ("CC-BY-4.0" if a.i_own_this else "ALL-RIGHTS-RESERVED")
    own = a.i_own_this or bool(a.license)
    d = PACKS / a.id
    if d.exists() and not a.append:
        sys.exit(f"{d} already exists — pass --append, or pick another --id.")
    d.mkdir(parents=True, exist_ok=True)
    mode = "a" if (a.append and (d / "lines.jsonl").exists()) else "w"
    with (d / "lines.jsonl").open(mode, encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    existing = {}
    if a.append and (d / "pack.json").exists():
        existing = json.loads((d / "pack.json").read_text(encoding="utf-8"))
    authors = existing.get("authors") or a.performer
    if a.performer not in authors:
        authors += f"; {a.performer}"
    (d / "pack.json").write_text(json.dumps({
        "id": a.id, "title": existing.get("title") or (a.title or f"{a.performer} — sets"),
        "authors": authors, "url": url or existing.get("url", ""),
        "license": existing.get("license", lic) if a.append else lic,
        "citation": "", "redistributable": own, "commercial_use": own,
        "attribution_required": True, "license_verified": own,
        "note": ("Lines anchored to audience reactions measured in the recording. "
                 + ("Owned by the uploader." if own else
                    "Third-party performance — NOT redistributable; the exporter "
                    "will refuse it.")),
        "files": ["lines.jsonl"],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    matched = len(rows)
    print(f"\nwrote {d} - {matched} line(s) from {len(events)} reaction(s)")
    if matched < len(events):
        print(f"  {len(events) - matched} reaction(s) had no distinct line before them "
              "(back-to-back reactions, or cues too short)")
    print(f"  credit: {a.performer} / {lic}"
          f"{'' if own else '   [local only - not redistributable]'}")
    print("\nnow run:  python build.py")


if __name__ == "__main__":
    main()
