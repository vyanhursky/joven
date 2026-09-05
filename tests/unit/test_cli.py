"""The command surface: what the commands say, not what the pipeline does.

Everything here runs the real Typer app in-process with the stub backend or no
backend at all, so it stays offline and fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from joven.cli import app
from joven.model import Annotation, Sidecar, Status, file_sha256

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


# ------------------------------------------------------------ sidecar commands


def _two_annotation_sidecar(tmp_path: Path) -> Path:
    sidecar = Sidecar(
        title="Test Book",
        annotations=[
            Annotation.create(
                href="OEBPS/part1.xhtml",
                para_index=2,
                source_text="Se fué.",
                spans=[(0, 7)],
                translation="He is gone.",
            ),
            Annotation.create(
                href="OEBPS/part1.xhtml",
                para_index=5,
                source_text="Cuántos años tienes? the old man said.",
                spans=[(0, 20)],
                translation="How old are you?",
            ),
        ],
    )
    path = tmp_path / "annotations.json"
    sidecar.save(path)
    return path


def test_reject_by_text_persists(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    result = runner.invoke(app, ["reject", str(path), "--find", "Se fué"])
    assert result.exit_code == 0, result.output
    statuses = {a.source_text: a.status for a in Sidecar.load(path).annotations}
    assert statuses["Se fué."] is Status.REJECTED
    assert statuses["Cuántos años tienes? the old man said."] is Status.AUTO


def test_reject_by_id(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    target = Sidecar.load(path).annotations[1]
    result = runner.invoke(app, ["reject", str(path), "--id", target.id])
    assert result.exit_code == 0, result.output
    assert Sidecar.load(path).annotations[1].status is Status.REJECTED


def test_reject_needs_exactly_one_selector(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    assert runner.invoke(app, ["reject", str(path)]).exit_code == 1
    assert runner.invoke(app, ["reject", str(path), "--find", "x", "--id", "y"]).exit_code == 1


def test_reject_refuses_an_ambiguous_match(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    result = runner.invoke(app, ["reject", str(path), "--find", "e"])  # in both paragraphs
    assert result.exit_code == 1
    assert "2 annotations match" in result.output
    assert all(a.status is Status.AUTO for a in Sidecar.load(path).annotations)


def test_reset_returns_a_decision_to_auto(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    runner.invoke(app, ["reject", str(path), "--find", "Se fué"])
    result = runner.invoke(app, ["reset", str(path), "--find", "Se fué"])
    assert result.exit_code == 0, result.output
    assert "rejected -> auto" in result.output
    assert Sidecar.load(path).annotations[0].status is Status.AUTO


def test_reset_warns_that_an_edited_wording_stays(tmp_path: Path) -> None:
    path = _two_annotation_sidecar(tmp_path)
    sidecar = Sidecar.load(path)
    sidecar.annotations[0].status = Status.EDITED
    sidecar.annotations[0].translation = "My wording."
    sidecar.save(path)
    result = runner.invoke(app, ["reset", str(path), "--find", "Se fué"])
    assert "edited wording stays" in result.output
    reloaded = Sidecar.load(path).annotations[0]
    assert reloaded.status is Status.AUTO and reloaded.translation == "My wording."


def test_status_reports_counts_and_whether_the_book_matches(
    sample_epub: Path, tmp_path: Path
) -> None:
    path = _two_annotation_sidecar(tmp_path)
    runner.invoke(app, ["reject", str(path), "--find", "Se fué"])
    result = runner.invoke(app, ["status", str(path), "--epub", str(sample_epub)])
    assert result.exit_code == 0, result.output
    assert "annotations   2" in result.output
    assert "rejected    1" in result.output
    assert "renderable    1" in result.output
    assert "no hash recorded" in result.output

    sidecar = Sidecar.load(path)
    sidecar.source_sha256 = "0" * 64
    sidecar.save(path)
    result = runner.invoke(app, ["status", str(path), "--epub", str(sample_epub)])
    assert "DIFFERENT FILE" in result.output


def test_diff_reports_every_kind_of_change(tmp_path: Path) -> None:
    old = _two_annotation_sidecar(tmp_path)
    later = Sidecar.load(old)
    later.annotations[0].translation = "He left."  # reworded
    later.annotations[1].status = Status.APPROVED  # changed status
    later.annotations.append(  # added
        Annotation.create(
            href="OEBPS/part1.xhtml",
            para_index=9,
            source_text="Vaya con Dios.",
            spans=[(0, 14)],
            translation="Go with God.",
        )
    )
    new = tmp_path / "later.json"
    later.save(new)

    result = runner.invoke(app, ["diff", str(old), str(new)])
    assert result.exit_code == 0, result.output
    assert "1 added, 0 removed, 1 reworded, 1 changed status" in result.output
    assert "was  'He is gone.'" in result.output
    assert "now  'He left.'" in result.output
    assert "auto -> approved" in result.output

    reverse = runner.invoke(app, ["diff", str(new), str(old)])
    assert "0 added, 1 removed" in reverse.output


# ------------------------------------------------------------------- config


def test_config_shows_values_and_their_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "joven.toml"
    toml.write_text('workers = 4\napi_key = "secret"\n', encoding="utf-8")
    monkeypatch.setenv("JOVEN_CONFIG", str(toml))
    monkeypatch.setenv("JOVEN_MODEL", "gemma3:12b")

    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert f"{toml}  (read)" in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "4" in lines["workers"] and str(toml) in lines["workers"]
    assert "gemma3:12b" in lines["model"] and "JOVEN_MODEL" in lines["model"]
    assert "secret" not in result.output and "••••" in lines["api_key"]
    assert lines["backend"].rstrip().endswith("default")


def test_a_broken_config_file_is_a_clear_error(
    sample_epub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "joven.toml"
    toml.write_text("worker = 4\n", encoding="utf-8")
    monkeypatch.setenv("JOVEN_CONFIG", str(toml))
    result = runner.invoke(app, ["detect", str(sample_epub), "--backend", "stub"])
    assert result.exit_code == 2
    assert "unknown setting 'worker'" in result.output


def test_detect_takes_its_backend_from_the_config_file(
    sample_epub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag, no Ollama: the file says stub, so nothing is contacted."""
    toml = tmp_path / "joven.toml"
    toml.write_text('backend = "stub"\n', encoding="utf-8")
    monkeypatch.setenv("JOVEN_CONFIG", str(toml))
    monkeypatch.setattr("joven.cli.ollama_available", lambda url: pytest.fail("contacted"))
    result = runner.invoke(
        app, ["detect", str(sample_epub), "-o", str(tmp_path / "a.json"), "--quiet"]
    )
    assert result.exit_code == 0, result.output
    assert Sidecar.load(tmp_path / "a.json").annotations


# ------------------------------------------------------------- detect scoping


def test_detect_unknown_backend_is_refused(sample_epub: Path) -> None:
    result = runner.invoke(app, ["detect", str(sample_epub), "--backend", "telepathy"])
    assert result.exit_code == 2
    assert "ollama, openai, stub, none" in result.output


def test_detect_href_limits_the_sidecar_to_that_document(
    sample_epub: Path, tmp_path: Path
) -> None:
    out = tmp_path / "a.json"
    args = [
        "detect", str(sample_epub), "--backend", "stub", "-o", str(out),
        "--href", "OEBPS/part1.xhtml", "--href", "nope.xhtml", "--quiet",
    ]  # fmt: skip
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "is not in the spine" in result.output
    assert {a.href for a in Sidecar.load(out).annotations} == {"OEBPS/part1.xhtml"}


def test_detect_range_scans_a_slice(sample_epub: Path, tmp_path: Path) -> None:
    args = [
        "detect", str(sample_epub), "--backend", "stub", "-o", str(tmp_path / "a.json"),
        "--range", "1:3", "--quiet",
    ]  # fmt: skip
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "paragraphs scanned    2" in result.output


def test_detect_rejects_a_malformed_range(sample_epub: Path) -> None:
    result = runner.invoke(app, ["detect", str(sample_epub), "--backend", "stub", "--range", "3"])
    assert result.exit_code == 1
    assert "START:STOP" in result.output


def test_detect_with_workers_matches_a_single_worker(sample_epub: Path, tmp_path: Path) -> None:
    one, four = tmp_path / "one.json", tmp_path / "four.json"
    for out, workers in ((one, "1"), (four, "4")):
        args = [
            "detect", str(sample_epub), "--backend", "stub", "-o", str(out),
            "--workers", workers, "--quiet",
        ]  # fmt: skip
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert [a.to_dict() for a in Sidecar.load(one).annotations] == [
        a.to_dict() for a in Sidecar.load(four).annotations
    ]
