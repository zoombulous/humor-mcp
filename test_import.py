#!/usr/bin/env python3
"""Exercise the two ingest front doors: import_corpus.py and import_transcript.py."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKS = ROOT / "packs"
TMP = Path(tempfile.mkdtemp(prefix="humor-import-test-"))
fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


# Pin the corpus these tests use, so a real user corpus in ~/.humor-mcp cannot
# change what they see.
ENV = {**os.environ, "HUMOR_PACKS": str(PACKS)}


def run(script, *args, expect_ok=True, env=None):
    p = subprocess.run([sys.executable, str(ROOT / script), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=180,
                       env=env or ENV)
    if expect_ok and p.returncode != 0:
        print(p.stdout, p.stderr)
    return p


def write(name, body):
    f = TMP / name
    f.write_text(body, encoding="utf-8")
    return str(f)


def lines_of(pid):
    return [json.loads(l) for l in
            (PACKS / pid / "lines.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


def manifest(pid):
    return json.loads((PACKS / pid / "pack.json").read_text(encoding="utf-8"))


created = []
try:
    # ---------------------------------------------------------------- transcripts
    print("transcript with reaction markers")
    srt = write("a.srt", """1
00:00:01,000 --> 00:00:04,000
I moved to a new city. Everyone said I'd love it.

2
00:00:04,100 --> 00:00:07,000
I have made one friend. He's my landlord.
[laughter]

3
00:00:07,100 --> 00:00:10,000
♪ music ♪
He doesn't know that yet.
[laughter and applause]
""")
    r = run("import_transcript.py", "--id", "t-react", "--input", srt,
            "--performer", "A. Comic", "--title", "Late set",
            "--url", "https://example.com/s", "--i-own-this")
    created.append("t-react")
    rows = lines_of("t-react")
    check(r.returncode == 0 and len(rows) == 2, f"two reactions -> two lines ({len(rows)})")
    check(all(x["kind"] == "joke" for x in rows), "reaction-anchored lines are jokes")
    check([x["laugh"] for x in rows] == [0.8, 1.0],
          f"laughter < laughter+applause: {[x['laugh'] for x in rows]}")
    check(rows[0]["text"] == "He's my landlord.", f"line is the pre-laugh sentence: {rows[0]['text']!r}")
    check("one friend" in rows[0]["context"], "setup lands in context")
    check(not any("♪" in x["context"] or "music" in x["context"] for x in rows),
          "music cues stripped from context")
    check(all('A. Comic — "Late set"' in x["attribution"] for x in rows),
          "every line stamped with performer and set")
    check("https://example.com/s" in rows[0]["attribution"], "source url carried per line")
    m = manifest("t-react")
    check(m["redistributable"] and m["license"] == "CC-BY-4.0", "--i-own-this -> shareable")

    print("\ntranscript with no markers")
    txt = write("b.txt", "HOST: Welcome back.\nGUEST: Most systems are procrastination "
                         "with a spreadsheet.\n")
    r = run("import_transcript.py", "--id", "t-plain", "--input", txt,
            "--performer", "A. Host")
    created.append("t-plain")
    rows = lines_of("t-plain")
    check(all(x["kind"] == "utterance" for x in rows),
          "no markers -> utterances, nothing claimed as a punchline")
    check(not any("laugh" in x for x in rows), "no fabricated laugh scores")
    check("no laughter or applause markers" in r.stdout, "the weaker path is announced")
    check(any("GUEST" in x["attribution"] for x in rows), "speaker labels become credit")
    m = manifest("t-plain")
    check(not m["redistributable"] and m["license"] == "ALL-RIGHTS-RESERVED",
          "third-party transcript defaults to local-only")

    print("\ntranscript credit is mandatory")
    r = run("import_transcript.py", "--id", "t-nope", "--input", txt, expect_ok=False)
    check(r.returncode != 0 and "performer" in (r.stderr + r.stdout).lower(),
          "refuses without --performer")
    check(not (PACKS / "t-nope").exists(), "no pack written on refusal")

    print("\nwhisper json + append")
    wj = write("c.json", json.dumps({
        "source_url": "https://example.com/ep", "metadata": {"title": "Ep 3"},
        "segments": [{"text": "This is the setup for the bit."},
                     {"text": "And that is the turn. [laughter]"}]}))
    r = run("import_transcript.py", "--id", "t-react", "--input", wj,
            "--performer", "B. Guest", "--append")
    rows = lines_of("t-react")
    check(r.returncode == 0 and len(rows) == 3, f"append added to the pack ({len(rows)})")
    check(any("Ep 3" in x["attribution"] for x in rows),
          "title and url read out of the whisper json")
    check("B. Guest" in manifest("t-react")["authors"]
          and "A. Comic" in manifest("t-react")["authors"],
          "appending records both performers")

    print("\nrefuses to clobber")
    r = run("import_transcript.py", "--id", "t-react", "--input", wj,
            "--performer", "C. Third", expect_ok=False)
    check(r.returncode != 0 and "--append" in r.stderr + r.stdout,
          "existing pack is not overwritten silently")

    # ------------------------------------------------------------------- corpus
    print("\nstructured import still works")
    csv = write("d.csv", "setup,line,rating\n"
                         "asked about the weekend,I alphabetized my spice rack. Twice.,3\n")
    r = run("import_corpus.py", "--id", "c-csv", "--input", csv, "--title", "T",
            "--authors", "A. Comic", "--license", "CC-BY-4.0")
    created.append("c-csv")
    rows = lines_of("c-csv")
    check(r.returncode == 0 and rows[0]["score"] == 3.0, "csv score column mapped")
    check(rows[0]["context"] == "asked about the weekend", "csv setup -> context")

    print("\nunknown licence is quarantined, not rejected")
    r = run("import_corpus.py", "--id", "c-unk", "--input", csv, "--title", "T",
            "--authors", "Someone", "--license", "UNKNOWN")
    created.append("c-unk")
    m = manifest("c-unk")
    check(r.returncode == 0 and not m["license_verified"], "loads but flagged unverified")
    exp = TMP / "share"
    run("build.py", "--export", str(exp))
    check(not (exp / "c-unk").exists(), "and the exporter refuses it")

    print("\nbuild picks all of it up")
    r = run("build.py")
    check(r.returncode == 0 and "t-react" in r.stdout and "c-csv" in r.stdout,
          "imported packs appear in the build")

    # ------------------------------------------------- regressions (code review)
    print("\nregression: a colon mid-sentence is not a speaker label")
    import importlib
    sys.path.insert(0, str(ROOT))
    it = importlib.import_module("import_transcript")
    for s in ["So I said: get out of my house.",
              "Here's the thing: nobody told me.",
              "My advice: never do that."]:
        check(it.SPEAKER.match(s) is None, f"not a label: {s[:34]!r}")
    for s, who in [("HOST: welcome back.", "HOST"),
                   ("Taylor Tomlinson: thanks.", "Taylor Tomlinson"),
                   ("DR. SMITH: indeed.", "DR. SMITH")]:
        m = it.SPEAKER.match(s)
        check(m is not None and m.group(1).strip() == who, f"still a label: {who}")

    print("\nregression: an aside in parentheses survives")
    rows, _ = it.build_lines(
        ["I told her I was fine (I was not fine) and she believed me. [laughter]"])
    check(bool(rows) and "(I was not fine)" in rows[0]["text"],
          f"aside kept: {rows[0]['text'] if rows else '-'}")
    rows, _ = it.build_lines(["The printer broke [inaudible] again. [laughter]"])
    check(bool(rows) and "[inaudible]" not in rows[0]["text"],
          "square-bracket stage directions still stripped")

    print("\nregression: a caption that is only a number is content")
    tr = importlib.import_module("transcripts")
    numsrt = write("num.srt", "1\n00:00:01,000 --> 00:00:03,000\n1995\n\n"
                              "2\n00:00:03,000 --> 00:00:05,000\nwas a good year.\n")
    cues = tr.read_srt_vtt(numsrt)
    check(any("1995" == c.text for c in cues), f"'1995' kept: {[c.text for c in cues]}")
    check(len(cues) == 2 and cues[0].start == 1.0, "cue indices still stripped")

    print("\npack model is validated at author time")
    import json as _json

    def with_manifest(man, lines='{"text":"a line long enough to survive"}\n'):
        d = PACKS / "zz-validate"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        (d / "pack.json").write_text(_json.dumps(man), encoding="utf-8")
        (d / "lines.jsonl").write_text(lines, encoding="utf-8")
        try:
            return run("build.py", expect_ok=False)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    base = {"id": "zz", "title": "T", "authors": "A", "license": "CC-BY-4.0"}
    r = with_manifest({**base, "redistributable": "false"})
    out = r.stdout + r.stderr
    check(r.returncode != 0 and "real boolean" in out,
          'a string "false" is rejected, not coerced to True')
    check("nothing was built" in out, "and nothing is built when the model is invalid")

    r = with_manifest({**base, "license": "CC-BY-NC-4.O", "redistributable": True})
    check(r.returncode != 0 and "not a recognised identifier" in (r.stdout + r.stderr),
          "a typo'd licence is caught instead of shipping as verified")
    r = with_manifest({**base, "license": "Weird-Custom-1.0", "custom_license": True})
    check(r.returncode == 0, "but an unusual licence is allowed once declared deliberate")

    r = with_manifest({**base, "redistributible": True})
    check(r.returncode != 0 and "did you mean 'redistributable'" in (r.stdout + r.stderr),
          "a typo'd key is named, with a suggestion")

    r = with_manifest({**base, "license": "ALL-RIGHTS-RESERVED", "redistributable": True})
    check(r.returncode != 0 and "ALL-RIGHTS-RESERVED but redistributable" in (r.stdout + r.stderr),
          "contradictory licence/redistributable is caught")
    r = with_manifest({**base, "license": "CC-BY-NC-4.0", "commercial_use": True})
    check(r.returncode != 0 and "forbids commercial use" in (r.stdout + r.stderr),
          "NC licence with commercial_use is caught")
    r = with_manifest({**base, "default_hidden": True})
    check(r.returncode != 0 and "hidden_reason is empty" in (r.stdout + r.stderr),
          "hiding a pack requires saying why")

    r = with_manifest({**base, "redistributable": "false", "license": "Nope-1.0",
                       "redistributible": True, "default_hidden": True})
    n = (r.stdout + r.stderr).count("\n  - ")
    check(n >= 4, f"every problem is reported in one pass, not one per rebuild ({n})")

    print("\nyour corpus lives outside the checkout")
    uhome = TMP / "userhome"
    uenv = {**os.environ, "HUMOR_HOME": str(uhome)}
    uenv.pop("HUMOR_PACKS", None)
    mine = write("mine.txt", "My landlord does not know we are friends.\n\n"
                             "I alphabetised the spice rack twice.\n")
    r = run("import_corpus.py", "--id", "u-mine", "--input", mine, "--title", "Mine",
            "--authors", "A Downloader", "--license", "CC-BY-4.0", env=uenv)
    check(r.returncode == 0 and (uhome / "packs" / "u-mine").is_dir(),
          "an import lands in ~/.humor-mcp/packs by default")
    check(not (PACKS / "u-mine").exists(), "and NOT inside the repo checkout")

    udb = TMP / "user.db"
    r = run("build.py", env={**uenv, "HUMOR_DB": str(udb)})
    check(r.returncode == 0 and "u-mine" in r.stdout,
          "build merges the user corpus with the bundled packs")
    check("mallard" in r.stdout or "cup" in r.stdout or "own" in r.stdout
          or len(r.stdout.splitlines()) > 2,
          "bundled packs are still there too")

    # a user pack may shadow a bundled one of the same id
    shadow = uhome / "packs" / "example-yours"
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "lines.jsonl").write_text(
        _json.dumps({"text": "my replacement for the bundled example"}) + "\n",
        encoding="utf-8")
    (shadow / "pack.json").write_text(_json.dumps({
        "id": "example-yours", "title": "Mine instead", "authors": "A Downloader",
        "license": "CC-BY-4.0", "redistributable": True, "commercial_use": True,
        "license_verified": True, "files": ["lines.jsonl"]}), encoding="utf-8")
    r = run("build.py", env={**uenv, "HUMOR_DB": str(udb)})
    check("shadows" in r.stdout, "a same-id user pack shadows the bundled one, and says so")
    import sqlite3 as _sq
    c = _sq.connect(udb)
    title = c.execute("select title from sources where id='example-yours'").fetchone()[0]
    n = c.execute("select count(*) from lines where source_id='example-yours'").fetchone()[0]
    c.close()
    check(title == "Mine instead" and n == 1, f"the user's version wins: {title!r} n={n}")

    print("\nregression: rebuild works while a reader holds the db open")
    import sqlite3
    held = sqlite3.connect(f"file:{ROOT / 'humor.db'}?mode=ro", uri=True)
    held.execute("SELECT count(*) FROM lines").fetchone()
    try:
        r = run("build.py")
        check(r.returncode == 0, f"build.py succeeds with the db open (exit {r.returncode})")
        check(held.execute("SELECT count(*) FROM lines").fetchone()[0] > 0,
              "the open reader still works afterwards")
    finally:
        held.close()

finally:
    for pid in created:
        shutil.rmtree(PACKS / pid, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)
    subprocess.run([sys.executable, str(ROOT / "build.py")], capture_output=True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
