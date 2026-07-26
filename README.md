# humor-mcp

**An MCP server over a local humor corpus. Every line it returns says who wrote it.**

[![PyPI](https://img.shields.io/pypi/v/humor-mcp?color=2b7489)](https://pypi.org/project/humor-mcp/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Corpora](https://img.shields.io/badge/corpora-licensed%20per%20pack-orange)](NOTICE)

Point a model at a body of jokes and it will happily launder them: you get the
material back with no idea whose it was, whether you may publish it, or whether
you may sell it. This server treats that as the primary problem. Every result
carries the credit for the line it came from, material that may not be
redistributed is withheld unless you ask for it by name, and the exporter
refuses to hand out anything whose licence does not permit it.

No database server, no network, no required dependencies — Python stdlib and one
SQLite file. It runs the same whether or not the machine that built the corpus is
awake.

(That holds for the server and every text path. Audio ingest is the one
exception: it needs `numpy`, `scipy` and `soundfile`, and is only imported when
you actually use it.)

```
packs/<id>/pack.json          who made this material, under what licence
packs/<id>/lines.jsonl        the lines
packs/<id>/pairs.jsonl        chosen/rejected preference pairs (optional)
humor_mcp/cli.py              the one entry point; subcommands below
          server.py           the MCP server (stdio, read-only)
          build.py            packs -> humor.db  (SQLite + FTS5)
          import_audio.py     audio + timed transcript (measured reactions)
          import_transcript.py transcript (text reaction markers)
          import_corpus.py    a jokes file
          audio_reactions.py  the laughter/applause detector
          transcripts.py      shared .srt/.vtt/.json parsing
          paths.py            where the bundled and user corpora live
server.py                     shim, so a checkout runs with nothing installed
```

## Install

Python 3.9+, no required dependencies. Two ways, depending on whether you want
the bundled corpus.

**Installed** — the tool only; bring your own corpus:

```bash
uvx humor-mcp where            # or: pip install humor-mcp
claude mcp add humor -- uvx humor-mcp
```

No account or checkout needed; `uvx` fetches it from PyPI. To run the unreleased
tip instead, point uvx at the repo:

```bash
uvx --from git+https://github.com/zoombulous/humor-mcp humor-mcp
```

**Cloned** — also gets the bundled packs, and needs nothing installed:

```bash
git clone https://github.com/zoombulous/humor-mcp
cd humor-mcp
python -m humor_mcp.cli build
claude mcp add humor -- python "$(pwd)/server.py"
```

On Windows use the full path in place of `$(pwd)`. Any MCP client works; this
speaks plain stdio JSON-RPC over stdin/stdout, and running the command with no
arguments is what starts the server.

Audio ingest is the one optional extra:

```bash
pip install "humor-mcp[audio]"     # or: pip install numpy scipy soundfile
```

### Commands

```
humor-mcp                     serve on stdio   <- what an MCP client runs
humor-mcp where               which corpus directories are in use
humor-mcp build               compile packs -> humor.db
humor-mcp build --export DIR  copy out only what may be redistributed
humor-mcp import-corpus       a .txt/.csv/.jsonl of jokes
humor-mcp import-transcript   a transcript
humor-mcp import-audio        audio + a timed transcript
humor-mcp reactions FILE      what the detector hears in a recording
```

From a clone with nothing installed, `python -m humor_mcp.cli ...` is the same
thing.

Check it:

```bash
python test_server.py      # 61 checks over real stdio
python test_import.py      # 52 checks over the importers and the pack model
python test_audio.py       # 27 checks against synthesized audio (needs numpy)
```

The suites build their own temporary corpus, so they pass on a fresh clone
regardless of which packs you have.

## Your corpus lives in your own folder

There are two pack directories, and the second one is yours:

```
<checkout>/packs        the packs that ship with this repo (clone only)
~/.humor-mcp/packs      YOUR corpus  <- imports go here by default
~/.humor-mcp/humor.db   the built database
```

If you pip-installed rather than cloned, the first of those does not exist and
your corpus is the only one — the wheel deliberately carries no packs, since
they are ~9 MB of other people's material and two of those packs are
non-commercial.

Both are read and merged at build time, so your material is searchable next to
the bundled packs without ever being mixed into the checkout. It survives
`git pull`, nothing in this repo touches it, and you will never resolve a merge
conflict over a joke file.

```bash
humor-mcp import-corpus --id my-sets --input jokes.txt \
    --title "My tight five" --authors "Your Name" --license CC-BY-4.0
# -> ~/.humor-mcp/packs/my-sets
humor-mcp build
```

If a pack of yours claims the same `id` as a bundled one, yours wins and the
build says so — that is how you replace a shipped pack rather than working
around it.

Every importer takes `--packs-dir` if you want a pack somewhere specific.

### Environment

| variable | what it does |
|---|---|
| `HUMOR_HOME` | move your corpus somewhere other than `~/.humor-mcp` |
| `HUMOR_PACKS` | read packs from exactly these directories instead (`os.pathsep` separated); the first is also where imports are written |
| `HUMOR_DB` | build or serve a database somewhere other than `<checkout>/humor.db` |

Together these let one checkout serve several corpora, or keep everything
outside the repo entirely.

## Tools

| tool | what it gives you |
|---|---|
| `search_humor` | full-text search, filtered by pack / kind / minimum human score |
| `top_rated` | the lines a human actually scored highly |
| `taste_profile` | liked vs disliked with the score distribution — calibration, not just examples |
| `breakdown` | the structural analysis of a joke: mechanism, setup/turn, technique |
| `preference_pairs` | what won head-to-head and what lost |
| `style_pack` | a paste-ready brief: exemplars + calibration + the attribution block |
| `sources` | every pack: authors, licence, whether it may be shared or sold, how to cite |
| `corpus_stats` | what is loaded |

Every result carries a `credit` object. Packs whose licence bars redistribution
are hidden unless a call passes `include_restricted: true`. `limit` is clamped to
200 — tool results land straight in the calling model's context, and a response
should not be able to flood it.

## Bring your own corpus

### From audio + a transcript (best)

```bash
humor-mcp reactions set.mp3          # what does it hear? look first
humor-mcp import-audio --id tight-five --audio set.mp3 --transcript set.srt \
    --performer "Your Name" --title "Comedy Cellar, March" --i-own-this
humor-mcp build
```

The audio supplies the reactions, the transcript supplies the words. The
detector finds laughter and applause in the waveform — real timings, real
durations, a strength that reflects how long the room actually went — and each
reaction is anchored to the line that earned it.

This is the strongest path because **it does not need the captioner to have
typed `[laughter]`**. Auto-generated captions never do, and that is most of what
people have. Needs a timestamped transcript (`.srt`, `.vtt`, Whisper `.json`);
a plain `.txt` carries no timings and cannot be aligned.

It does **not** transcribe. Nothing downloads a model or calls a service — bring
a transcript from whatever you already use. Audio needs `numpy`, `scipy` and
`soundfile`; nothing else in this repo has dependencies. wav/flac/ogg/mp3 read
directly, m4a and aac need converting with ffmpeg first.

**How the detector works, and where it breaks.** Two features do the work:
*spectral flatness* separates the room from the voice (a voice has pitch so its
spectrum is peaky; audience noise is broadband — measured on synthetic signals,
speech 0.004 against laughter 0.47 and applause 0.56), then *4–8 Hz modulation*
separates laughter from applause, since laughter has a syllable rhythm and
applause is a wash. Loudness is used only to rule out silence: the performer is
mic'd and the room is not, so real applause runs about 3 dB above speech and any
volume threshold finds either everything or nothing.

It is a heuristic detector, not a trained model. Music, a noisy room, or a comic
laughing into their own mic will fool it. Run `humor-mcp reactions` on your file
and read the flatness values before trusting it; `--flat-speech-max` is the knob.

### From a transcript

The usual case — you have a recording of a set, not a tidy file of jokes:

```bash
humor-mcp import-transcript --id tight-five --input set.srt \
    --performer "Your Name" --title "Comedy Cellar, March" --i-own-this
humor-mcp build
```

`.srt`, `.vtt`, Whisper `.json` and plain `.txt` all work, with or without
`SPEAKER:` labels. Timestamps, cue numbers, music cues and stage directions are
stripped. Pass several files at once, or `--append` to add another set later;
`--performer` is recorded per line, so one pack can hold several people.

**How it finds the jokes.** If the transcript keeps its reaction cues —
`[laughter]`, `(laughs)`, `[applause]` — they are used the way an audio pipeline
uses the waveform: the sentence before the cue is the line, what came before it
is the context, and the cue's strength becomes a `laugh` weight (a chuckle
ranks below laughter, which ranks below laughter-and-applause). That weight is
ordering, not measurement.

If there are no cues, the transcript is segmented into sentences with a rolling
context window and **nothing is marked as a punchline**, because nothing in the
text says which parts landed. You get material, not judgements, and the importer
tells you so rather than quietly presenting utterances as jokes.

Transcripts of other people's performances default to **not redistributable** —
that is what a transcript of a copyrighted set is. `--i-own-this` flips it to
CC-BY-4.0, or set `--license` explicitly.

### From a structured file

If you already have jokes in a file:

```bash
humor-mcp import-corpus --id my-sets --input jokes.txt \
    --title "My tight five" --authors "Your Name" --license CC-BY-4.0
humor-mcp build
```

`.txt` (blank-line separated), `.csv` (a `text`/`line`/`joke` column, optionally
`context`, `score`, `note`) and `.jsonl` all work. `--authors` and `--license`
are required. If you don't know the licence, pass `--license UNKNOWN`: the pack
still loads and is still searchable locally, but it is flagged everywhere it
surfaces and refused by the exporter.

Or skip the importer and write `packs/<id>/pack.json` plus a `lines.jsonl` by
hand — that is the whole contract:

```json
{"text": "...", "context": "...", "score": 3, "note": "...", "attribution": "..."}
```

## The pack model

`packs/*/pack.json` is a declarative model and `humor-mcp build` is its compiler, so the
model is checked before anything is built — every field has a declared type with
a value predicate, and a bad manifest fails at author time with **all** its
problems listed at once, not one per rebuild.

```
$ humor-mcp build
6 problem(s) in the corpus model — nothing was built:
  - packs/zz/pack.json: unknown field 'redistributible' — did you mean 'redistributable'?
  - packs/zz/pack.json: 'redistributable' must be a real boolean — true/false, not "true"/"false", got 'false'
  - packs/zz/pack.json: licence 'CC-BY-NC-4.O' is not a recognised identifier. Fix the
    typo, or set "custom_license": true to state that you meant it.
  - packs/zz/pack.json: default_hidden is true but hidden_reason is empty — record why,
    or the reason lives only in someone's memory
```

This matters more than ordinary input validation, because the fields *are* the
licence policy. `"redistributable": "false"` is a non-empty string, so it used to
coerce to `True` and the exporter would ship material whose manifest said three
separate times not to. Typed fields make that unrepresentable.

Cross-field rules are checked too: `ALL-RIGHTS-RESERVED` cannot be
`redistributable`, an `-NC-` licence cannot allow `commercial_use`, a placeholder
licence cannot be `license_verified`, and `default_hidden` requires a
`hidden_reason`.

The tool schemas the MCP advertises are derived from the Python function
signatures rather than maintained alongside them, so a parameter cannot exist in
one and not the other; an undocumented parameter raises at import.

## Sharing a corpus

```bash
humor-mcp build --export ./share
```

Copies out only the packs whose licence actually permits it, and writes a
`CREDITS.md` covering exactly what went. It refuses anything non-redistributable,
anything with an unverified licence, and anything whose licence field is still
`UNSET` — so an unlabelled pack can't leak by accident.

## What ships with this repo

Cloning gets you a working corpus, not an empty shell — but read the licence
column before you build anything on top of it. **Two of these packs are
non-commercial**, and the credit attached to every result will tell you so.

| pack | lines / pairs | licence | |
|---|---|---|---|
| `mallard` | 1,248 + 463 | CC-BY-4.0 | © James Barker — engine output plus his own ratings, h2h picks and eval verdicts |
| `cup` | 2,753 | CC-BY-NC-4.0 | Context-Situated Pun dataset, Sun et al., EMNLP 2022 |
| `expun` | 1,899 | CC-BY-NC-4.0 | ExPUNations, Sun et al., EMNLP 2022 |
| `rjokes` | 4,000 pairs | CC-BY-4.0 | Reddit r/Jokes via SocialGrep. *Off-rubric — hidden by default.* |
| `nycc` | 1,500 pairs | CC-BY-4.0 | New Yorker Caption Contest, Hessel et al., ACL 2023. Text only, no cartoons. *Off-rubric — hidden by default.* |

The author's own local corpus also holds a `standup` pack of transcribed
crowd work, which is **not** in this repo: its licence does not permit
redistribution, so `.gitignore` keeps it out and `humor-mcp build --export` refuses it.
That asymmetry is the point of the tool — the same rules that hide it from you
hide your restricted material from everyone else.

## Two gates, deliberately separate

A pack can be withheld from default results for two unrelated reasons, and
collapsing them into one switch loses information:

- **`redistributable: false`** — a **licence** question. You may not pass this
  material on. `standup` is the only such pack. Reachable with
  `include_restricted: true`, never exported.
- **`default_hidden: true`** — a **relevance** question. The licence is fine and
  the exporter will happily ship it; the corpus owner judged it unrepresentative.
  `rjokes` and `nycc` are 92% of all pairs and were excluded from DPO v6 for
  pulling the model off Mallard's voice, so they are held back rather than
  drowning the 463 on-rubric pairs 12:1. Reachable with `include_hidden: true`,
  or by naming the pack — asking for it by name is answer enough.

`preference_pairs` reports what it withheld in a `withheld` field rather than
quietly returning less than you asked for.

## A note on de-duplication

The ingest merges duplicate lines **within** a pack, because some source tables
overlap — ExPUN arrives once carrying its structural breakdown and again carrying
its explanation, so every pun would otherwise appear twice with half its fields
each.

The merge key is `(text, attribution)`, never text alone. Two people can deliver
the same line, and collapsing those would credit one of them for the other's
material; 25 texts in the `standup` pack already appear under more than one
credit and are kept separate. Identical lines in *different* packs are never
merged at all, since they carry different credit by definition.

## Licences

Two, as is normal for a repo that ships both software and data:

- **The code** — the `humor_mcp` package and the test suites — is MIT,
  © 2026 James Barker. See [LICENSE](LICENSE).
- **The corpora** are licensed individually, per pack, in `packs/<id>/pack.json`.
  They are **not** covered by the MIT licence, two of them forbid commercial
  use, and some may not be redistributed at all. See [NOTICE](NOTICE) for the
  summary; the `sources` tool prints the live state of all of them.

Owning something and licensing it are different: the `authors` field in each
pack records who owns the material, and the `license` field records what
everyone else is permitted to do with it.
