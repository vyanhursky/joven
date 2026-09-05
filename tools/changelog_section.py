"""Print the CHANGELOG section for one version.

The GitHub release notes are the CHANGELOG entry, not a second copy of it, and
this is what extracts one. It is also a guard: a tag whose version has no section
exits non-zero, which CI runs *before* publishing -- after the PyPI upload the
version number is spent whether or not the notes existed.

    python tools/changelog_section.py 1.0.0b4          # prints the section body
    python tools/changelog_section.py 1.0.0b4 > notes.md

Headings are matched as ``## v1.0.0b4 -- 2026-09-02``; the ``v`` and the date are
both optional, so ``## Unreleased`` is a heading too, and correctly ends the
section above it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from joven.console import force_utf8_output

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

# "## v1.0.0b4 — 2026-09-02", "## 1.0.0 - 2027-01-01", "## Unreleased"
HEADING = re.compile(r"^## v?(?P<version>\S+)(?:\s+[—–-]+\s+(?P<date>.*?))?\s*$")


def section(changelog: str, version: str) -> str:
    """The body under ``## v{version}``, up to the next ``##`` heading."""
    body: list[str] = []
    collecting = False
    for line in changelog.splitlines():
        heading = HEADING.match(line)
        if heading:
            if collecting:
                break
            collecting = heading.group("version") == version
            continue
        if collecting:
            body.append(line)
    if not collecting:
        raise LookupError(f"CHANGELOG.md has no section for v{version}")
    return "\n".join(body).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    force_utf8_output()  # the CHANGELOG has em dashes; a cp1252 stdout would drop them
    try:
        sys.stdout.write(section(CHANGELOG.read_text(encoding="utf-8"), argv[1]))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
