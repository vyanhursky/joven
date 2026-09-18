"""Entry point for the packaged app.

PyInstaller freezes a *script*, not a console-script entry point, so this is the
one line that has to exist separately from ``pyproject.toml``'s ``joven =
joven.cli:main``. Both reach the same function: :func:`joven.cli.main` is what
decides that a frozen build started with no arguments should run ``ui`` rather
than print command-line help at someone who double-clicked an icon.

Keeping it to a call means there is no second copy of the startup behaviour to
drift out of step with the installed command.
"""

from __future__ import annotations

import multiprocessing

from joven.cli import main

if __name__ == "__main__":
    # Without this a frozen build on Windows re-executes the whole app in every
    # child process instead of starting a worker, which for `detect --workers`
    # means a fork bomb of browser windows.
    multiprocessing.freeze_support()
    main()
