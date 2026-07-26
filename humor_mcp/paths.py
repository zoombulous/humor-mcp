"""Where the corpus lives.

Two places, deliberately:

  <repo>/packs          the packs that ship with this checkout
  ~/.humor-mcp/packs    YOUR corpus

Your material belongs in the second one. It survives `git pull`, it is never
touched by anything in this repo, and it is where the importers write by
default — so cloning this and adding your own jokes does not entangle the two
or leave you resolving merge conflicts over a joke file.

Both are read at build time and merged, with your packs winning if an id
collides, so you can shadow a bundled pack with your own version of it.

Overrides:
  HUMOR_HOME    move your corpus somewhere other than ~/.humor-mcp
  HUMOR_PACKS   read packs from exactly these directories instead (os.pathsep
                separated); the first is also where imports are written
  HUMOR_DB      build/serve a database somewhere other than ~/.humor-mcp/humor.db

There is ONE database per HUMOR_HOME and every checkout shares it, but packs are
read from whichever checkout you are standing in. So `build` from a second clone
rewrites that one database from a different set of packs, and anything held only
in the other checkout drops out of it. This is the practical reason your material
belongs in ~/.humor-mcp/packs rather than <repo>/packs: it is read no matter
where you build from, so no clone can quietly build a corpus without it. Set
HUMOR_DB when you want a build that cannot touch your real one.
"""
import os
from pathlib import Path

PKG = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HUMOR_HOME") or (Path.home() / ".humor-mcp"))
USER_PACKS = HOME / "packs"
# INSIDE the package, so it travels in the wheel: `pip install humor-mcp` or
# `uvx humor-mcp` arrives with a working corpus and needs no download, no build
# step and nothing to wire up. Only the author's own CC-BY-4.0 pack lives here —
# it carries every human rating and every best-of-N pick, and it may be used
# commercially, which the third-party packs below may not.
SHIPPED_PACKS = PKG / "packs"
# Beside the package in a checkout; absent once pip-installed. The third-party
# packs are big and two of them are non-commercial, so they stay clone-only.
BUNDLED_PACKS = PKG.parent / "packs"


def _override():
    raw = os.environ.get("HUMOR_PACKS")
    if not raw:
        return None
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def candidate_dirs():
    """Every directory that COULD hold packs, whether or not it exists — so an
    error message can name the place you were expected to put something."""
    ov = _override()
    return ov if ov is not None else [SHIPPED_PACKS, BUNDLED_PACKS, USER_PACKS]


def pack_dirs():
    """Directories to read packs from. Later entries win on an id collision."""
    return [d for d in candidate_dirs() if d.is_dir()]


def import_target():
    """Where a newly imported pack is written. Yours, not the repo's."""
    ov = _override()
    return ov[0] if ov else USER_PACKS


def db_path():
    """Built database. Lives with YOUR corpus, not with the code — site-packages
    is not writable and a checkout should not accumulate build artefacts."""
    return Path(os.environ.get("HUMOR_DB") or (HOME / "humor.db"))
