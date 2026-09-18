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

import os
import shutil
import subprocess
import sys
from pathlib import Path

VENDOR_ENV = "JOVEN_VENDOR"


def vendor_dir() -> Path | None:
    """Where the packaged app keeps the tools it ships with, or None if it is not one.

    The downloadable app exists so that a reader never installs anything by hand,
    which means it has to carry ``kepubify`` itself. PyInstaller unpacks its data
    under ``sys._MEIPASS`` in both onefile and onedir builds, so one lookup covers
    both. ``JOVEN_VENDOR`` names a directory instead, which is how this is tested
    without building an app.
    """
    if override := os.environ.get(VENDOR_ENV):
        path = Path(override)
        return path if path.is_dir() else None
    root = getattr(sys, "_MEIPASS", None)
    if root is None:
        return None
    path = Path(root) / "vendor"
    return path if path.is_dir() else None


def resolve(name: str) -> str | None:
    """The full path to ``name``, or None if it is not installed.

    The app's own copy wins over ``PATH``. That order is deliberate: the bundled
    binary is the version this release was tested against, and a reader who also
    has an older ``kepubify`` from somewhere else should not silently get it.

    This returns a path rather than a bool precisely so a caller cannot reintroduce
    trap 1 by testing with this and then running the bare name — and the bundled
    branch keeps that promise, returning the file itself rather than a name that
    happens to sit in a directory.
    """
    if (vendor := vendor_dir()) is not None:
        for candidate in (vendor / name, vendor / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
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


def java_runtime() -> str | None:
    """The path to a JVM that actually starts, or None.

    Trap 1 again, in the one place resolving is genuinely not enough. Every macOS
    install carries ``/usr/bin/java`` whether or not a JDK was ever installed: a
    stub whose entire job is to say "Unable to locate a Java Runtime" and exit 1.
    It is a real executable, so ``shutil.which`` finds it and ``resolve`` hands it
    back — and then ``doctor`` reported ``java`` and ``epubcheck`` OK on a stock
    Mac, promised twelve integrity checks, and the render ended in ``1 of 12
    checks FAILED`` with the stub's message quoted back at a reader who never
    installed Java and was told they did not need to.

    That is the same shape as the ``.CMD`` bug above — availability said yes and
    the invocation died — so it gets the same answer: ask the tool, do not ask
    ``PATH`` about the tool. One JVM start, and only on the jar route; a launcher
    on ``PATH`` never reaches here. Deliberately uncached, because the Setup tab's
    *Check again* has to see a JDK that was installed a minute ago.
    """
    java = resolve("java")
    if java is None:
        return None
    try:
        started = run([java, "-version"]).returncode == 0
    except OSError:
        # A resolved path that will not spawn is trap 1 exactly, and the answer is
        # the same as a stub that starts and refuses: there is no JVM here.
        return None
    return java if started else None


def java_command(jar: Path) -> list[str] | None:
    """The argv that runs ``jar``, or None if there is no JVM that starts."""
    java = java_runtime()
    return [java, "-jar", str(jar)] if java else None
