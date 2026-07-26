"""Force UTF-8 on the standard streams.

Windows consoles default to cp1252. A humor corpus is full of em dashes and
curly quotes, so without this the scripts crash or emit mojibake the moment a
line contains one — and anything capturing their output as UTF-8 fails too.
"""
import sys


def force_utf8():
    for s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
