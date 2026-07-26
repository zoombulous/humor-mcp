"""Timestamped transcript parsing, shared by the transcript and audio importers.

One parser, two consumers: import_transcript.py throws the timings away and
works from reaction markers in the text, import_audio.py keeps them and aligns
against reactions measured in the waveform.
"""
import json, re
from pathlib import Path

TS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
                r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")
TS_SHORT = re.compile(r"(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
                      r"(\d{1,2}):(\d{2})[.,](\d{1,3})")


class Cue:
    __slots__ = ("start", "end", "text")

    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text

    def __repr__(self):
        return f"Cue({self.start:.2f}-{self.end:.2f}, {self.text[:40]!r})"


def _secs(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def read_srt_vtt(path):
    out = []
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    for block in re.split(r"\n\s*\n", raw):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        start = end = None
        text = []
        # A bare number is a cue index only in the lead position of a timed
        # block. Elsewhere it is content — a caption reading "1995" was being
        # silently dropped.
        timed_block = any("-->" in l for l in lines)
        for pos, l in enumerate(lines):
            if l.upper().startswith("WEBVTT"):
                continue
            if pos == 0 and timed_block and re.fullmatch(r"\d+", l):
                continue
            m = TS.search(l)
            if m:
                g = m.groups()
                start, end = _secs(*g[:4]), _secs(*g[4:])
                continue
            m = TS_SHORT.search(l)
            if m:
                g = m.groups()
                start, end = _secs(0, *g[:3]), _secs(0, *g[3:])
                continue
            text.append(l)
        if text:
            out.append(Cue(start, end, " ".join(text)))
    return out


def read_json(path):
    """Whisper-style output, and the shape mallard's own transcriber writes."""
    d = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    meta = {}
    if isinstance(d, dict):
        segs = d.get("segments") or d.get("chunks") or []
        m = d.get("metadata") or {}
        meta = {"url": d.get("source_url") or m.get("audio_url") or "",
                "title": m.get("title") or d.get("episode_id") or ""}
    else:
        segs, d = d, {}
    out = []
    for s in segs:
        if not isinstance(s, dict):
            if str(s).strip():
                out.append(Cue(None, None, str(s).strip()))
            continue
        t = (s.get("text") or "").strip()
        if not t:
            continue
        start, end = s.get("start"), s.get("end")
        if start is None and isinstance(s.get("timestamp"), (list, tuple)):
            start, end = (list(s["timestamp"]) + [None, None])[:2]
        out.append(Cue(start, end, t))
    return out, meta


def read_txt(path):
    return [Cue(None, None, l.strip()) for l in
            Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            if l.strip()]


def read_any(path):
    """-> (cues, meta). Timings are None for formats that carry none."""
    p = Path(path)
    sfx = p.suffix.lower()
    if sfx in (".srt", ".vtt"):
        return read_srt_vtt(p), {}
    if sfx == ".json":
        return read_json(p)
    if sfx in (".txt", ".md", ""):
        return read_txt(p), {}
    raise SystemExit(f"unsupported transcript type {sfx}; use .srt, .vtt, .json or .txt")


def has_timings(cues):
    return sum(1 for c in cues if c.start is not None) >= max(2, len(cues) // 2)
