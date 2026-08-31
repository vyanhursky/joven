"""Output-stream encoding.

Its own module so the dev tools in ``tools/`` can use it without importing the
CLI, which would drag in Typer and define every command as a side effect.
"""

from __future__ import annotations

import io
import sys


def force_utf8_output() -> None:
    """Pin stdout and stderr to UTF-8.

    On Windows, a console gets UTF-16 through the console API and behaves, but a
    *redirected* stream falls back to the locale encoding — cp1252 on a stock
    install. Two consequences, and this tool hits both: ``matríz`` written to a
    file comes back as ``matr?z``, and any character cp1252 has no room for
    raises ``UnicodeEncodeError`` from inside whatever was printing at the time.

    Unix already defaults to UTF-8, so this is a no-op there rather than a
    platform branch that has to be kept in sync.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
