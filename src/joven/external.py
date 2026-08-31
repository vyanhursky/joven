"""Locating and invoking the external binaries.

Windows is why this module exists. Two portability traps live in the gap between
"is the tool installed?" and "run the tool", and both were live bugs:

1. **Resolve before you run.** ``shutil.which`` honours ``PATHEXT``, so on Windows
   it cheerfully returns ``epubcheck.CMD`` — but ``CreateProcess`` can only start a
   ``.exe``, so ``subprocess.run(["epubcheck", ...])`` raises ``FileNotFoundError``
   for a tool that is installed and on ``PATH``. That is the worst shape a
   dependency check can take: ``epubcheck_available()`` returned True and the run
   then died. It bites every ``.cmd``/``.bat`` launcher, which is how epubcheck,
   Chocolatey shims and most JVM tools arrive on Windows. So resolve the name once
   and invoke the *resolved path*.

2. **Say what encoding you meant.** ``text=True`` alone decodes with the locale
   encoding, which is cp1252 on a stock Windows install. epubcheck echoes the name
   of the file it validated, so one accented character in a book's filename turns a
   clean validation into mojibake — or a ``UnicodeDecodeError`` raised from inside
   our own error reporting. Every call here decodes UTF-8 explicitly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def resolve(name: str) -> str | None:
    """The full path to ``name`` on ``PATH``, or None if it is not installed.

    This returns a path rather than a bool precisely so a caller cannot reintroduce
    trap 1 by testing with this and then running the bare name.
    """
    return shutil.which(name)


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` with the text decoding pinned to UTF-8 (trap 2)."""
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def java_command(jar: Path) -> list[str] | None:
    """The argv that runs ``jar``, or None if there is no JVM on ``PATH``."""
    java = resolve("java")
    return [java, "-jar", str(jar)] if java else None
