"""The command surface: what the commands say, not what the pipeline does.

Everything here runs the real Typer app in-process with the stub backend or no
backend at all, so it stays offline and fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from joven.cli import app
from joven.model import Annotation, Sidecar, file_sha256

runner = CliRunner()


def _sidecar_for(sample_epub: Path, tmp_path: Path, *, sha256: str) -> Path:
    """One annotation on a paragraph the synthetic book really contains."""
    sidecar = Sidecar(
        source_sha256=sha256,
        annotations=[
            Annotation.create(
                href="OEBPS/part1.xhtml",
                para_index=2,
                source_text="Se fué.",
                spans=[(0, 7)],
                translation="He is gone.",
            )
        ],
    )
    path = tmp_path / "annotations.json"
    sidecar.save(path)
    return path


def test_render_warns_when_the_sidecar_came_from_another_file(
    sample_epub: Path, tmp_path: Path
) -> None:
    """The hash was recorded on every detect and then never read back.

    A sidecar from a different edition used to surface only as a per-paragraph
    "source text drifted" error deep inside rendering. Now the plain fact -- these
    are not the same file -- is said first.
    """
    sidecar = _sidecar_for(sample_epub, tmp_path, sha256="0" * 64)
    result = runner.invoke(
        app, ["render", str(sample_epub), str(sidecar), "-o", str(tmp_path / "out"), "--no-kepub"]
    )
    assert result.exit_code == 0, result.output
    assert "detected from a different file" in result.output


def test_render_is_quiet_when_the_sidecar_matches(sample_epub: Path, tmp_path: Path) -> None:
    sidecar = _sidecar_for(sample_epub, tmp_path, sha256=file_sha256(sample_epub))
    result = runner.invoke(
        app, ["render", str(sample_epub), str(sidecar), "-o", str(tmp_path / "out"), "--no-kepub"]
    )
    assert result.exit_code == 0, result.output
    assert "different file" not in result.output


def test_render_does_not_warn_when_no_hash_was_recorded(
    sample_epub: Path, tmp_path: Path
) -> None:
    """A hand-built sidecar has nothing to compare, and must not be nagged about it."""
    sidecar = _sidecar_for(sample_epub, tmp_path, sha256="")
    result = runner.invoke(
        app, ["render", str(sample_epub), str(sidecar), "-o", str(tmp_path / "out"), "--no-kepub"]
    )
    assert result.exit_code == 0, result.output
    assert "different file" not in result.output


def test_detect_uses_the_ollama_url_it_was_given(
    sample_epub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL used to be a constant, so a non-default Ollama was unreachable."""
    asked: list[str] = []

    def unavailable(base_url: str) -> bool:
        asked.append(base_url)
        return False

    monkeypatch.setattr("joven.cli.ollama_available", unavailable)
    result = runner.invoke(
        app, ["detect", str(sample_epub), "--ollama-url", "http://elsewhere:11434"]
    )
    assert result.exit_code == 2
    assert asked == ["http://elsewhere:11434"]
    assert "http://elsewhere:11434" in result.output


def test_detect_reads_the_ollama_url_from_the_environment(
    sample_epub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    monkeypatch.setattr("joven.cli.ollama_available", lambda url: asked.append(url) or False)
    monkeypatch.setenv("JOVEN_OLLAMA_URL", "http://from-env:11434")

    result = runner.invoke(app, ["detect", str(sample_epub)])
    assert result.exit_code == 2
    assert asked == ["http://from-env:11434"]
