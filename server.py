#!/usr/bin/env python3
"""Run the MCP server straight out of a checkout, with nothing installed.

    claude mcp add humor -- python /path/to/humor-mcp/server.py

Python puts this file's directory on sys.path, so `humor_mcp` resolves no
matter what the client's working directory is. If you pip-installed instead,
register the `humor-mcp` command and ignore this file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from humor_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
