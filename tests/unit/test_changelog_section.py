"""The release notes come from the CHANGELOG, so the extraction has to be exact."""

from __future__ import annotations

import pytest

# `tools` is on the path via [tool.pytest.ini_options] pythonpath in pyproject.
from changelog_section import CHANGELOG, section

SAMPLE = """\
# Changelog

## Unreleased

- not yet

## v1.0.0b4 — 2026-09-02

**Windows is a supported platform.**

### Fixed

- one thing

## v1.0.0b3 — 2026-08-28

- older
"""


def test_returns_only_the_requested_section() -> None:
    body = section(SAMPLE, "1.0.0b4")
    assert body.startswith("**Windows is a supported platform.**")
    assert "- one thing" in body
    assert "older" not in body
    assert "not yet" not in body


def test_unreleased_is_a_boundary_not_a_body() -> None:
    """The heading above the version must not leak its notes into the release."""
    assert section(SAMPLE, "1.0.0b3") == "- older\n"


def test_missing_version_is_an_error() -> None:
    with pytest.raises(LookupError, match="no section for v9.9.9"):
        section(SAMPLE, "9.9.9")


def test_the_real_changelog_has_the_current_version() -> None:
    """What CI checks on a tag, run here against the working tree."""
    import tomllib

    version = tomllib.loads(
        (CHANGELOG.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert section(CHANGELOG.read_text(encoding="utf-8"), version)
