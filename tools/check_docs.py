"""Fail if the docs describe a CLI that does not exist.

Docs and code move at different speeds, and the gap is invisible to a human
rereading their own prose. This diffs every ``joven ...`` invocation in the
Markdown against the live ``--help`` output.

It has caught real drift twice: a ``--html coverage.html`` report that was built
as a localhost server instead, and a ``--escalate-below`` flag that never existed.
Both had survived a careful manual read of the same files minutes earlier.

    python tools/check_docs.py

A line preceded by a comment containing ``NOT IMPLEMENTED`` is allowed through, so
a design document can show a sketch of something unbuilt without lying about it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "DESIGN.md", "docs/model-selection.md", "docs/troubleshooting.md"]
INVOCATION = re.compile(r"\bjoven ([a-z][a-z-]*)((?: [^\n#]*)?)")
FLAG = re.compile(r"--[a-z][a-z0-9-]+")
LINK = re.compile(r"\[[^\]]*\]\(([^)#][^)]*)\)")
# Screenshots are embedded as HTML so they can sit side by side, which puts their
# paths outside Markdown link syntax and therefore outside the check above.
IMG_SRC = re.compile(r"""<img[^>]*\bsrc=["']([^"']+)["']""")


def cli_surface(executable: str) -> dict[str, set[str]]:
    """Every command the CLI really has, and the flags each really accepts."""
    try:
        top = subprocess.run([executable, "--help"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise SystemExit(
            f"error: {executable!r} is not on PATH.\n"
            f"       Install the package first (pip install -e .), or point at the\n"
            f"       built script: --executable ./.venv/bin/joven"
        ) from None
    commands = set(re.findall(r"^\s*│\s+([a-z][a-z-]*)\s", top, re.M))
    if not commands:  # help rendering differs across typer/rich versions
        commands = set(re.findall(r"^\s{2,}([a-z][a-z-]*)\s{2,}\S", top, re.M))
    surface: dict[str, set[str]] = {}
    for cmd in sorted(commands):
        out = subprocess.run([executable, cmd, "--help"], capture_output=True, text=True).stdout
        surface[cmd] = set(FLAG.findall(out))
    return surface


def check_links() -> list[str]:
    """Relative links must point at files that exist.

    A dead link or a broken image is the most common defect in a public README and the
    easiest to ship, because the author knows what they meant and never clicks it.
    """
    problems: list[str] = []
    for name in [*DOCS, "CHANGELOG.md"]:
        path = ROOT / name
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            for pattern, label in ((LINK, "dead link"), (IMG_SRC, "missing image")):
                for match in pattern.finditer(line):
                    target = match.group(1).split("#")[0].strip()
                    if not target or target.startswith(("http://", "https://", "mailto:")):
                        continue
                    if not (path.parent / target).exists():
                        problems.append(f"{name}:{i + 1}: {label} -> {target}")
    return problems


def check(executable: str) -> list[str]:
    surface = cli_surface(executable)
    if not surface:
        return ["could not read the CLI surface — is the package installed?"]

    problems: list[str] = []
    for name in DOCS:
        path = ROOT / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith(">"):  # blockquotes are commentary
                continue
            match = INVOCATION.search(line)
            if not match:
                continue
            # An adjacent NOT IMPLEMENTED marker is an explicit, honest exception.
            context = " ".join(lines[max(0, i - 1):i + 1])
            if "NOT IMPLEMENTED" in context:
                continue
            cmd, rest = match.group(1), match.group(2)
            if cmd not in surface:
                problems.append(f"{name}:{i + 1}: no such command `joven {cmd}`")
                continue
            for flag in FLAG.findall(rest):
                if flag not in surface[cmd]:
                    problems.append(f"{name}:{i + 1}: `joven {cmd}` has no {flag}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default=sys.executable.replace("python", "joven"))
    args = parser.parse_args()

    problems = check(args.executable) + check_links()
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"\n  FAIL: {len(problems)} problem(s) — the docs describe something "
              f"that is not there.")
        return 1
    print("  PASS: documented commands all exist, and every relative link resolves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
