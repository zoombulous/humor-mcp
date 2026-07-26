#!/usr/bin/env python3
"""
Build a pack from a recording plus a timestamped transcript.

    python import_audio.py --id tight-five --audio set.mp3 --transcript set.srt \
        --performer "Your Name" --title "Comedy Cellar, March" --i-own-this

    python import_audio.py --audio set.mp3 --dry-run     # just show what it hears

WHY BOTH FILES
  The audio supplies the reactions, the transcript supplies the words. Neither
  alone is enough, and the combination is strictly better than either path on
  its own:

    transcript only  needs the captioner to have typed [laughter]. Auto-generated
                     captions never do, which is most of what people have.
    audio only       tells you exactly when the room went and how hard, but not
                     what was said.
    both             real timings and real strength, attached to real words —
                     and it works on transcripts with no markers at all.

  Whisper .json, .srt and .vtt all carry timings. A plain .txt does not, so it
  cannot be aligned; use import_transcript.py for that.

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
    ap.add_argument("--audio", required=True)
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

    print(f"{audio.name}: {len(y)/sr/60:.1f} min, {len(events)} reaction(s) detected")
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

    title = a.title or meta.get("title") or audio.stem
    url = a.url or meta.get("url") or ""
    cred = f'{a.performer} — "{title}"' + (f" ({url})" if url else "")
    for r in rows:
        r["attribution"] = cred
        r["meta"] = json.dumps({**r["meta"], "audio": audio.name, "set": title},
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
