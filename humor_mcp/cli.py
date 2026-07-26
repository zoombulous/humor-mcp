#!/usr/bin/env python3
"""
Single entry point.

    humor-mcp                     run the MCP server on stdio  <- what a client invokes
    humor-mcp build               compile packs -> humor.db
    humor-mcp build --export DIR  copy out only what may be redistributed
    humor-mcp import-corpus ...   a .txt/.csv/.jsonl of jokes
    humor-mcp import-transcript   a transcript
    humor-mcp import-audio ...    audio + a timed transcript
    humor-mcp reactions FILE...   what the detector hears in a recording
    humor-mcp where               show which corpus directories are in use

No arguments means "serve", because that is how MCP clients launch it: they
exec the command and speak JSON-RPC over stdin/stdout, with no argv to spare.
"""
import sys

from ._utf8 import force_utf8

SUBCOMMANDS = {
    "build": ("humor_mcp.build", "compile packs into the database"),
    "import-corpus": ("humor_mcp.import_corpus", "import a jokes file"),
    "import-transcript": ("humor_mcp.import_transcript", "import a transcript"),
    "import-audio": ("humor_mcp.import_audio", "import audio + a timed transcript"),
    "reactions": ("humor_mcp.audio_reactions", "list detected audience reactions"),
}


def _usage():
    print(__doc__.strip())
    print("\nsubcommands:")
    for name, (_mod, desc) in SUBCOMMANDS.items():
        print(f"  {name:20s} {desc}")
    print("\nEnvironment: HUMOR_HOME, HUMOR_PACKS, HUMOR_DB "
          "(see `humor-mcp where`).")


def _where():
    from . import paths
    print(f"corpus home   {paths.HOME}")
    print(f"your packs    {paths.import_target()}")
    print("reading from:")
    for d in paths.candidate_dirs():
        if d.is_dir():
            n = len([x for x in d.iterdir() if x.is_dir()])
            print(f"  {d}  ({n} pack(s))")
        else:
            print(f"  {d}  [does not exist yet]")
    db = paths.db_path()
    print(f"database      {db}" + ("" if db.exists() else "  [not built yet]"))


def _autobuild():
    """Compile the corpus on first launch, so installing is the whole install.

    An MCP client's first contact with this program is `exec humor-mcp` — there
    is no earlier moment in which to run a setup step, and a client that gets an
    error instead of a handshake just reports the server as broken. Since the
    wheel now carries a corpus, "no database yet" is a first run rather than a
    misconfiguration, and the honest response is to build it.

    Deliberately quiet and deliberately non-fatal: stdout belongs to JSON-RPC and
    must not be polluted, and a build that fails should still leave the server to
    start and explain itself properly rather than dying here.
    """
    from . import paths
    if paths.db_path().exists():
        return
    try:
        from .build import build
        _out = sys.stdout
        sys.stdout = sys.stderr          # keep the protocol channel clean
        try:
            build()
        finally:
            sys.stdout = _out
    except Exception as e:               # noqa: BLE001 - never block the handshake
        print(f"humor-mcp: first-run build failed ({e}); serving anyway",
              file=sys.stderr)


def main(argv=None):
    force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("serve", "server"):
        _autobuild()
        from .server import main as serve
        return serve()
    cmd = argv[0]
    if cmd in ("-h", "--help", "help"):
        _usage()
        return 0
    if cmd == "where":
        _where()
        return 0
    if cmd in ("-V", "--version", "version"):
        from . import __version__
        print(__version__)
        return 0
    if cmd not in SUBCOMMANDS:
        print(f"unknown subcommand {cmd!r}\n", file=sys.stderr)
        _usage()
        return 2

    # each subcommand module owns its own argparse; hand it a clean argv
    import importlib
    mod = importlib.import_module(SUBCOMMANDS[cmd][0])
    sys.argv = [f"humor-mcp {cmd}"] + argv[1:]
    return mod.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
