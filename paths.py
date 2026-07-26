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
  HUMOR_DB      build/serve a database somewhere other than <repo>/humor.db
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HUMOR_HOME") or (Path.home() / ".humor-mcp"))
USER_PACKS = HOME / "packs"
BUNDLED_PACKS = ROOT / "packs"


def _override():
    raw = os.environ.get("HUMOR_PACKS")
    if not raw:
        return None
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def pack_dirs():
    """Directories to read packs from. Later entries win on an id collision."""
    ov = _override()
    if ov is not None:
        return ov
    return [d for d in (BUNDLED_PACKS, USER_PACKS) if d.is_dir()]


def import_target():
    """Where a newly imported pack is written. Yours, not the repo's."""
    ov = _override()
    return ov[0] if ov else USER_PACKS


def db_path():
    return Path(os.environ.get("HUMOR_DB") or (ROOT / "humor.db"))
