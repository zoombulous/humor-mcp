#!/usr/bin/env python3
"""
Test the reaction detector against synthesized audio with known ground truth.

⚠️ THESE TESTS PASSING DOES NOT MEAN THE DETECTOR WORKS. Measured against real
stand-up it scores F1 0.24 — see the warning in humor_mcp/audio_reactions.py.

They are close to circular by construction: `speech()` is a pure harmonic stack
(flatness 0.004) and `laughter()` is modulated noise (0.47), which is exactly
the gap `detect()` looks for. Real recordings collapse it to 0.055 vs 0.058.

What they still legitimately cover: the signal-processing does what it says on
signals that DO separate, the framing/segmentation maths is right, audio loads
and downmixes correctly, degenerate inputs do not crash, and the alignment of
reactions to transcript lines — which is the part that is actually sound and is
reused verbatim by the --reactions path.
"""
import json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from humor_mcp.audio_reactions import detect, load, SR  # noqa: E402

rng = np.random.default_rng(7)
fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def speech(dur, f0=115.0):
    """Harmonic + slow envelope: peaky spectrum, syllable rate ~3 Hz."""
    t = np.arange(int(dur * SR)) / SR
    y = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.0 * t)
    y = y * env + 0.01 * rng.standard_normal(len(t))
    return (0.28 * y / (np.abs(y).max() + 1e-9)).astype(np.float32)


def applause(dur):
    """Broadband noise, many uncorrelated claps: flat spectrum."""
    n = int(dur * SR)
    y = rng.standard_normal(n)
    for _ in range(int(dur * 90)):
        i = rng.integers(0, max(1, n - 200))
        y[i:i + 200] += 5 * rng.standard_normal(200) * np.exp(-np.arange(200) / 40)
    return (0.85 * y / (np.abs(y).max() + 1e-9)).astype(np.float32)


def laughter(dur, rate=5.0):
    """Voiced-ish noise chopped at the syllable rate: mid flatness, 5 Hz beat."""
    t = np.arange(int(dur * SR)) / SR
    carrier = rng.standard_normal(len(t)) + 0.7 * np.sin(2 * np.pi * 240 * t)
    beat = np.clip(np.sin(2 * np.pi * rate * t), 0, None) ** 1.5
    y = carrier * (0.15 + beat)
    return (0.8 * y / (np.abs(y).max() + 1e-9)).astype(np.float32)


def silence(dur):
    return (0.004 * rng.standard_normal(int(dur * SR))).astype(np.float32)


print("speech alone")
ev = detect(np.concatenate([speech(3), silence(0.5), speech(3)]))
check(len(ev) == 0, f"no reactions found in pure speech ({len(ev)} found)")

print("\napplause")
ev = detect(np.concatenate([speech(2), silence(0.3), applause(2.0), silence(0.3),
                            speech(2)]))
check(len(ev) == 1, f"exactly one reaction ({len(ev)})")
check(bool(ev) and ev[0]["kind"] == "applause",
      f"classified as applause ({ev[0]['kind'] if ev else '-'})")
check(bool(ev) and 2.0 < ev[0]["start"] < 3.0,
      f"located after the speech ({ev[0]['start'] if ev else '-'}s)")

print("\nlaughter")
ev = detect(np.concatenate([speech(2), silence(0.3), laughter(1.6), silence(0.3),
                            speech(2)]))
check(len(ev) == 1, f"exactly one reaction ({len(ev)})")
check(bool(ev) and ev[0]["kind"] == "laughter",
      f"classified as laughter ({ev[0]['kind'] if ev else '-'})")
check(bool(ev) and ev[0]["modulation"] > 0.14,
      f"picked up the syllable rhythm (mod={ev[0]['modulation'] if ev else '-'})")

print("\nstrength tracks how long the room goes")
short = detect(np.concatenate([speech(2), silence(0.3), laughter(0.7), silence(0.3),
                               speech(2)]))
long_ = detect(np.concatenate([speech(2), silence(0.3), laughter(3.0), silence(0.3),
                               speech(2)]))
check(bool(short) and bool(long_) and long_[0]["strength"] > short[0]["strength"],
      f"3.0s beats 0.7s ({long_[0]['strength'] if long_ else '-'} > "
      f"{short[0]['strength'] if short else '-'})")

print("\na whole set")
parts, truth = [], []
t = 0.0
for i in range(4):
    d = 3.0 + i
    parts.append(speech(d)); t += d
    parts.append(silence(0.25)); t += 0.25
    kind = "applause" if i == 3 else "laughter"
    parts.append(applause(1.8) if i == 3 else laughter(1.4 + 0.3 * i))
    truth.append((round(t, 2), kind))
    t += 1.8 if i == 3 else 1.4 + 0.3 * i
    parts.append(silence(0.25)); t += 0.25
ev = detect(np.concatenate(parts))
check(len(ev) == 4, f"found all four reactions ({len(ev)})")
kinds_ok = [e["kind"] for e in ev] == [k for _, k in truth]
check(kinds_ok, f"kinds correct: {[e['kind'] for e in ev]}")
if len(ev) == len(truth):
    drift = max(abs(e["start"] - s) for e, (s, _) in zip(ev, truth))
    check(drift < 0.35, f"timings within 350 ms (worst {drift*1000:.0f} ms)")

print("\nround-trips through a real file")
with tempfile.TemporaryDirectory() as td:
    wav = Path(td) / "set.wav"
    sf.write(wav, np.concatenate([speech(2), silence(0.3), laughter(1.6),
                                  silence(0.3), speech(2)]), SR)
    y, sr = load(wav)
    check(sr == SR and len(y) > 0, "wav loads to mono at 16k")
    check(len(detect(y)) == 1, "same result off disk as in memory")

    stereo = Path(td) / "stereo48.wav"
    mono = np.concatenate([speech(2), silence(0.3), applause(1.8), silence(0.3)])
    up = np.repeat(mono, 3)  # 48k
    sf.write(stereo, np.stack([up, up], axis=1), SR * 3)
    y2, sr2 = load(stereo)
    check(sr2 == SR and abs(len(y2) - len(mono)) < 100,
          "48k stereo is downmixed and decimated to 16k mono")
    check(len(detect(y2)) == 1, "detection survives the conversion")

    p = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "reactions", str(wav)],
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    check(p.returncode == 0 and "laughter" in p.stdout, "CLI prints what it hears")

print("\naligning audio to a marker-free transcript")
LINES = ["So I moved to a new city last year.",
         "Everyone told me I would love it here.",
         "I have made exactly one friend and he is my landlord.",
         "He does not know that we are friends.",
         "I have not brought it up with him yet.",
         "My mother calls me every single Sunday.",
         "She opens with are you sitting down and then describes a sale."]
LAUGH_AFTER = {2: 1.5, 4: 2.6, 6: 1.2}


def _ts(x):
    h, r = divmod(x, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


import shutil  # noqa: E402
PACKS = ROOT / "packs"
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    audio, srt, t = [], [], 0.0
    for i, text in enumerate(LINES):
        d = 1.4 + 0.12 * len(text.split())
        srt.append(f"{len(srt)+1}\n{_ts(t)} --> {_ts(t+d)}\n{text}\n")
        audio.append(speech(d)); t += d
        audio.append(silence(0.25)); t += 0.25
        if i in LAUGH_AFTER:
            audio.append(laughter(LAUGH_AFTER[i])); t += LAUGH_AFTER[i]
            audio.append(silence(0.25)); t += 0.25
    wav, sub = td / "s.wav", td / "s.srt"
    sf.write(wav, np.concatenate(audio), SR)
    sub.write_text("\n".join(srt), encoding="utf-8")
    check("[laughter]" not in sub.read_text(encoding="utf-8"),
          "the transcript really has no reaction markers")

    # Imports go to ~/.humor-mcp/packs by default; pin them to this checkout so
    # the test does not write into a real user corpus.
    import os
    IENV = {**os.environ, "HUMOR_PACKS": str(PACKS)}

    def imp(*extra, expect_ok=True):
        p = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "import-audio",
                            "--audio", str(wav), *extra],
                           capture_output=True, text=True, encoding="utf-8", timeout=300,
                           env=IENV)
        if expect_ok and p.returncode != 0:
            print(p.stdout, p.stderr)
        return p

    r = imp("--dry-run")
    check(r.returncode == 0 and "3 reaction" in r.stdout, "dry run reports 3 reactions")
    check(not (PACKS / "a-demo").exists(), "dry run writes nothing")

    try:
        r = imp("--id", "a-demo", "--transcript", str(sub), "--performer", "A. Comic",
                "--title", "Test set", "--i-own-this")
        rows = [json.loads(l) for l in
                (PACKS / "a-demo" / "lines.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        got = [x["text"] for x in rows]
        want = [LINES[i] for i in sorted(LAUGH_AFTER)]
        check(got == want, f"picked exactly the punchlines: {got == want}")
        check(all(x["kind"] == "joke" for x in rows), "anchored lines are jokes")
        check(rows[1]["laugh"] > rows[2]["laugh"],
              f"2.6s laugh outranks 1.2s ({rows[1]['laugh']} > {rows[2]['laugh']})")
        check(all('A. Comic — "Test set"' in x["attribution"] for x in rows),
              "credit stamped per line")
        meta = json.loads(rows[0]["meta"])
        check(meta["detector"] == "audio" and "reaction_at" in meta,
              "provenance records that the anchor was measured, not typed")
        check(LINES[0] in rows[0]["context"], "context comes from the preceding cues")

        # --reactions: a timeline from a real classifier, bypassing the detector
        print("\na reaction timeline from a real classifier")
        rj = td / "reactions.json"
        want = [LINES[i] for i in sorted(LAUGH_AFTER)]
        # times taken from the same synthetic layout, as an external tool would give
        marks, t2 = [], 0.0
        for i, text in enumerate(LINES):
            t2 += 1.4 + 0.12 * len(text.split()) + 0.25
            if i in LAUGH_AFTER:
                marks.append({"start": round(t2, 2),
                              "end": round(t2 + LAUGH_AFTER[i], 2),
                              "kind": "laughter",
                              "strength": round(0.3 + 0.2 * len(marks), 2)})
                t2 += LAUGH_AFTER[i] + 0.25
        rj.write_text(json.dumps(marks), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "import-audio",
                            "--id", "a-react", "--reactions", str(rj),
                            "--transcript", str(sub), "--performer", "A. Comic",
                            "--title", "Test set", "--i-own-this"],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=300, env=IENV)
        if r.returncode != 0:
            print(r.stdout, r.stderr)
        rows2 = [json.loads(l) for l in
                 (PACKS / "a-react" / "lines.jsonl").read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        check(r.returncode == 0, "imports with no audio file at all")
        check([x["text"] for x in rows2] == want,
              f"same punchlines as the audio path: {[x['text'] for x in rows2] == want}")
        check([x["laugh"] for x in rows2] == [m["strength"] for m in marks],
              "the classifier's strengths are carried through unchanged")
        check("does not work" not in r.stdout and "F1 0.24" not in r.stdout,
              "no detector warning on this path — the detector was not used")
        shutil.rmtree(PACKS / "a-react", ignore_errors=True)

        bad = td / "bad.json"
        bad.write_text(json.dumps([{"kind": "laughter"}]), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "humor_mcp.cli", "import-audio",
                            "--id", "a-bad", "--reactions", str(bad),
                            "--transcript", str(sub), "--performer", "X"],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=300, env=IENV)
        check(r.returncode != 0 and "start" in (r.stdout + r.stderr),
              "a reaction with no timestamp is rejected by name")

        # a transcript with no timings cannot be aligned and must say so
        flat = td / "s.txt"
        flat.write_text("\n".join(LINES), encoding="utf-8")
        r = imp("--id", "a-nope", "--transcript", str(flat), "--performer", "X",
                expect_ok=False)
        check(r.returncode != 0 and "timestamp" in (r.stdout + r.stderr),
              "refuses an untimed transcript with a useful message")
        check(not (PACKS / "a-nope").exists(), "and writes nothing")
    finally:
        shutil.rmtree(PACKS / "a-demo", ignore_errors=True)
        shutil.rmtree(PACKS / "a-nope", ignore_errors=True)
        subprocess.run([sys.executable, "-m", "humor_mcp.cli", "build"], capture_output=True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
