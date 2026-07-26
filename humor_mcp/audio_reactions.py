#!/usr/bin/env python3
"""
Find audience reactions in a recording — laughter and applause — from the
waveform alone.

    python audio_reactions.py set.mp3            # list what it hears

This is the signal a transcript's [laughter] markers stand in for, except
measured: real timings, real durations, and a strength that reflects how long
and how hard the room actually went, rather than which word the captioner typed.

WHAT IT IS
  A heuristic signal detector, not a trained classifier. Two features do the
  work, in this order:

    spectral flatness  is this speech or the room? A voice has pitch, so its
                       spectrum is peaky; audience noise is broadband and reads
                       nearly flat. Measured on synthetic signals: speech 0.004,
                       laughter 0.47, applause 0.56 — two orders of magnitude,
                       which is why this and not volume is the gate.
    4-8 Hz modulation  laughter or applause? Laughter is rhythmic, the "ha-ha-ha"
                       syllable rate sitting around 5 Hz; applause is a wash.

  Loudness is used ONLY to rule out silence. It is deliberately not a gate:
  the performer is mic'd and the room is not, so real applause runs only about
  3 dB above speech and any volume threshold either finds everything or nothing.
  (Silence is also spectrally flat, being noise, which is what the energy floor
  is there to catch.)

  It will be fooled by music, by a noisy room, and by a comic who laughs at
  their own joke into the mic. Run it on your audio and look at the output
  before trusting it — that is what the bare CLI above is for.

Needs numpy, scipy and soundfile. Reads wav/flac/ogg/mp3 through libsndfile;
m4a and aac need ffmpeg to convert first.
"""
import sys
from pathlib import Path

try:
    import numpy as np
    import soundfile as sf
    from scipy.signal import get_window
except ImportError as e:  # pragma: no cover - environment dependent
    raise SystemExit(
        f"audio support needs numpy, scipy and soundfile ({e}).\n"
        "    pip install numpy scipy soundfile\n"
        "The rest of humor-mcp has no dependencies; only audio does.")

from ._utf8 import force_utf8
force_utf8()

SR = 16000
WIN = 512          # 32 ms
HOP = 160          # 10 ms -> 100 frames/sec envelope, enough for 4-8 Hz
EPS = 1e-10


def load(path, sr=SR):
    """Mono float32 at `sr`. Decimates rather than resampling properly, which is
    fine here: every feature is spectral-shape or envelope based."""
    y, in_sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    if in_sr != sr:
        # integer decimation where possible, linear interpolation otherwise
        if in_sr % sr == 0:
            y = y[:: in_sr // sr]
        else:
            n = int(len(y) * sr / in_sr)
            y = np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y)
    return y.astype(np.float32), sr


def frames(y, win=WIN, hop=HOP):
    if len(y) < win:
        y = np.pad(y, (0, win - len(y)))
    n = 1 + (len(y) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return y[idx]


def features(y, sr=SR):
    f = frames(y)
    w = get_window("hann", f.shape[1], fftbins=True)
    spec = np.abs(np.fft.rfft(f * w, axis=1)) ** 2 + EPS
    # ignore the lowest bins: room rumble and DC skew flatness badly
    band = spec[:, 4:]
    flatness = np.exp(np.log(band).mean(axis=1)) / band.mean(axis=1)
    rms = np.sqrt((f ** 2).mean(axis=1) + EPS)
    db = 20 * np.log10(rms + EPS)
    return db, flatness, rms


def modulation(env, fps, lo=3.5, hi=8.0):
    """Fraction of envelope energy in the laughter syllable band."""
    if len(env) < 8:
        return 0.0
    e = env - env.mean()
    if not np.any(e):
        return 0.0
    spec = np.abs(np.fft.rfft(e * np.hanning(len(e)))) ** 2
    freqs = np.fft.rfftfreq(len(e), 1.0 / fps)
    total = spec[freqs > 0.5].sum() + EPS
    return float(spec[(freqs >= lo) & (freqs <= hi)].sum() / total)


def detect(y, sr=SR, min_dur=0.45, max_gap=0.20, flat_speech_max=0.22,
           mod_laughter=0.14, silence_margin=8.0):
    """-> [{start, end, dur, kind, strength, flatness, modulation}]

    Loudness deliberately is NOT the gate. A performer is mic'd and the room is
    not, so measured applause runs only ~3 dB above speech — thresholding on
    volume finds nothing. Flatness separates them by two orders of magnitude,
    so it does the work, and energy is used only to rule out silence (which is
    also spectrally flat, being noise).

    flat_speech_max is the knob to tune on real recordings: synthetic speech is
    perfectly harmonic, a real room is not, so a noisy source may need this
    raised. Run the CLI and look at the reported flatness values.
    """
    db, flat, rms = features(y, sr)
    fps = sr / HOP

    quiet = np.percentile(db, 10)
    active = db > quiet + silence_margin
    # a voice is harmonic and so has a peaky spectrum; audience noise is broadband
    cand = active & (flat > flat_speech_max)

    segs, i, n = [], 0, len(cand)
    gap_frames = int(max_gap * fps)
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j + 1 < n:
            if cand[j + 1]:
                gap = 0
            else:
                gap += 1
                if gap > gap_frames:
                    break
            j += 1
        j -= gap
        if (j - i) / fps >= min_dur:
            segs.append((i, j))
        i = j + gap_frames + 1

    active_db = db[active]
    lo_db = float(np.percentile(active_db, 5)) if active_db.size else quiet
    hi_db = float(np.percentile(active_db, 99)) if active_db.size else lo_db + 1
    out = []
    for a, b in segs:
        f_mean = float(flat[a:b + 1].mean())
        mod = modulation(rms[a:b + 1], fps)
        dur = (b - a) / fps
        # Both are broadband; what separates them is rhythm. Laughter has a
        # syllable rate, applause is a wash.
        kind = "laughter" if mod >= mod_laughter else "applause"
        loud_norm = float(np.clip(
            (db[a:b + 1].mean() - lo_db) / max(hi_db - lo_db, 1.0), 0, 1))
        # how long the room went matters as much as how loud
        dur_norm = float(np.clip(dur / 2.5, 0, 1))
        out.append({
            "start": round(a / fps, 3), "end": round(b / fps, 3),
            "dur": round(dur, 3), "kind": kind,
            "strength": round(0.5 * loud_norm + 0.5 * dur_norm, 3),
            "flatness": round(f_mean, 4), "modulation": round(mod, 4),
        })
    return out


def detect_file(path, **kw):
    y, sr = load(path)
    return detect(y, sr, **kw), len(y) / sr


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        ev, dur = detect_file(p)
        print(f"\n{Path(p).name}  ({dur/60:.1f} min)  -> {len(ev)} reaction(s)")
        for e in ev:
            mm, ss = divmod(e["start"], 60)
            print(f"  {int(mm):02d}:{ss:05.2f}  {e['kind']:9s} "
                  f"{e['dur']:5.2f}s  strength={e['strength']:.2f} "
                  f"(flat={e['flatness']:.3f} mod={e['modulation']:.3f})")
        if not ev:
            print("  nothing detected — if the room clearly reacts, the audience may be "
                  "mixed too low to separate from the mic.")


if __name__ == "__main__":
    main()
