"""The readiness checks, and the one property that makes them worth running.

``doctor`` exists because the dependencies fail quietly. That only helps if what
it reports and what a run does agree, so the tests here assert the agreement
rather than the wording: a check that says a tool is available must be backed by
something the rest of the code can actually invoke, and a check that blocks must
be one that genuinely stops a book being annotated.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from joven import preflight
from joven.cli import app
from joven.preflight import FAIL, OK, OPTIONAL, REQUIRED, WARN, Check, blocking, run_checks
from joven.verify import JAR_ENV

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """No real config file, no inherited config, and a home the test owns.

    JOVEN_EPUBCHECK_JAR has to be cleared, not just left to the config file:
    the variable outranks the file on purpose, and CI's Windows job exports it
    so that the jar route is exercised there. A preflight test that does not
    clear it asks a different question on Windows than it does anywhere else --
    which is exactly how this fixture earned the extra line.
    """
    config = tmp_path / "joven.toml"
    config.write_text('backend = "ollama"\n', encoding="utf-8")
    monkeypatch.setenv("JOVEN_CONFIG", str(config))
    monkeypatch.setenv("JOVEN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(JAR_ENV, raising=False)
    return config


def _ollama(monkeypatch: pytest.MonkeyPatch, *, up: bool, models: list[str]) -> None:
    monkeypatch.setattr(preflight, "ollama_available", lambda *_a, **_k: up)
    monkeypatch.setattr(preflight, "installed_models", lambda *_a, **_k: models)


# ------------------------------------------------------------------ the pair invariant


def test_a_tool_reported_ok_is_a_tool_that_can_be_run() -> None:
    """The invariant, stated once.

    This is the shape of bug the checks exist to prevent — a report of "installed"
    that a later invocation contradicts. Rather than asserting a fixed outcome
    (which depends on what is installed on the machine running the tests), assert
    that every OK claim is backed by the same lookup the runtime uses.
    """
    from joven.kepub import kepubify_available
    from joven.verify import epubcheck_command

    backing = {
        "kepubify": lambda: kepubify_available(),
        "epubcheck": lambda: epubcheck_command() is not None,
        "java": lambda: preflight.external.resolve("java") is not None,
    }
    for check in run_checks(check_port=False):
        if check.name in backing and check.state == OK:
            assert backing[check.name](), f"{check.name} reported OK but cannot be run"


# ------------------------------------------------------------------ the required pair


def test_no_model_server_blocks_and_names_the_download(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama(monkeypatch, up=False, models=[])
    checks = run_checks(check_port=False)
    server = next(c for c in checks if c.name == "model server")
    assert server.state == FAIL
    assert server.severity == REQUIRED
    assert preflight.OLLAMA_DOWNLOAD in server.hint
    assert blocking(checks) == [server]


def test_a_running_server_without_the_model_blocks_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two findings, not one: the server is fine and the model is the problem."""
    _ollama(monkeypatch, up=True, models=["llama3:8b"])
    checks = run_checks(check_port=False)
    assert next(c for c in checks if c.name == "model server").state == OK
    model = next(c for c in checks if c.name == "model")
    assert model.state == FAIL
    assert model.blocking
    assert "ollama pull qwen3:8b  (about 6 GB)" in model.hint
    assert model.fix == "pull-model", "the UI needs a remedy it can offer as a button"


def test_the_latest_tag_counts_as_pulled(monkeypatch: pytest.MonkeyPatch, isolated_config) -> None:
    """``model = "qwen3"`` is served by Ollama as ``qwen3:latest``.

    Spelling the same model two ways is how a pulled model gets reported missing.
    """
    isolated_config.write_text('backend = "ollama"\nmodel = "qwen3"\n', encoding="utf-8")
    _ollama(monkeypatch, up=True, models=["qwen3:latest"])
    assert next(c for c in run_checks(check_port=False) if c.name == "model").state == OK


def test_an_exact_tag_counts_as_pulled(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama(monkeypatch, up=True, models=["qwen3:8b", "llama3:8b"])
    assert next(c for c in run_checks(check_port=False) if c.name == "model").state == OK


# ------------------------------------------------------------------ severities


def test_kepubify_missing_warns_but_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama(monkeypatch, up=True, models=["qwen3:8b"])
    monkeypatch.setattr(preflight, "kepubify_available", lambda: False)
    checks = run_checks(check_port=False)
    kepubify = next(c for c in checks if c.name == "kepubify")
    assert (kepubify.state, kepubify.severity) == (WARN, preflight.RECOMMENDED)
    assert not blocking(checks)
    assert "KEPUB" in kepubify.hint


def test_epubcheck_missing_is_one_optional_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing epubcheck costs the 12th check and nothing else.

    One row, not two. Java used to get its own line, which was right while the app
    shipped the jar and the JVM really was the missing piece; now a bare
    "java — not found" names something the reader never asked for.
    """
    _ollama(monkeypatch, up=True, models=["qwen3:8b"])
    monkeypatch.setattr(preflight, "epubcheck_command", lambda: None)
    monkeypatch.setattr(preflight.external, "resolve", lambda name: None)
    checks = run_checks(check_port=False)

    assert [c.name for c in checks].count("java") == 0
    epubcheck = next(c for c in checks if c.name == "epubcheck")
    assert epubcheck.severity == OPTIONAL
    assert not epubcheck.blocking
    assert "12th" in epubcheck.detail


def test_a_configured_jar_with_no_jvm_is_the_one_case_that_names_java(
    monkeypatch: pytest.MonkeyPatch, isolated_config
) -> None:
    """Java is only worth naming when it is genuinely the missing piece."""
    _ollama(monkeypatch, up=True, models=["qwen3:8b"])
    isolated_config.write_text(
        'backend = "ollama"\nepubcheck_jar = "/tmp/epubcheck.jar"\n', encoding="utf-8"
    )
    monkeypatch.setattr(preflight, "epubcheck_command", lambda: None)
    monkeypatch.setattr(preflight.external, "java_runtime", lambda: None)
    checks = run_checks(check_port=False)

    epubcheck = next(c for c in checks if c.name == "epubcheck")
    assert "Java" in epubcheck.detail
    assert "Eleven" in epubcheck.hint


def test_a_broken_config_file_is_a_finding_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, isolated_config
) -> None:
    """``doctor`` is what you run when something is wrong, so it cannot crash."""
    isolated_config.write_text("backend = [unclosed\n", encoding="utf-8")
    checks = run_checks(check_port=False)
    assert len(checks) == 1
    assert (checks[0].name, checks[0].state) == ("configuration", FAIL)
    assert checks[0].blocking


def test_an_unwritable_home_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A home under a *file* cannot be created, on every platform we support."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    _ollama(monkeypatch, up=True, models=["qwen3:8b"])
    monkeypatch.setenv("JOVEN_HOME", str(blocker / "books"))
    checks = run_checks(check_port=False)
    home = next(c for c in checks if c.name == "books directory")
    assert home.state == FAIL
    assert home.blocking


# ------------------------------------------------------------------ the command


def test_doctor_exits_zero_when_only_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama(monkeypatch, up=True, models=["qwen3:8b"])
    monkeypatch.setattr(preflight, "kepubify_available", lambda: False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ready, with" in result.output


def test_doctor_exits_one_when_a_required_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama(monkeypatch, up=False, models=[])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "not ready" in result.output
    assert "model server" in result.output


def test_check_renders_its_hint_on_its_own_line() -> None:
    text = str(Check("kepubify", WARN, "not found", "install it", preflight.RECOMMENDED))
    assert text.splitlines()[0].startswith("[WARN] kepubify — not found")
    assert text.splitlines()[1].strip() == "install it"


def test_as_dict_carries_everything_the_page_needs() -> None:
    payload = Check("model", FAIL, "missing", "pull it", REQUIRED, fix="pull-model").as_dict()
    assert payload == {
        "name": "model",
        "state": FAIL,
        "detail": "missing",
        "hint": "pull it",
        "severity": REQUIRED,
        "fix": "pull-model",
        "blocking": True,
    }


def test_the_install_hint_names_a_command_that_exists_on_this_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Linux reader told to run `scoop` is worse off than one told nothing."""
    expected = {"win32": "scoop", "darwin": "brew"}
    for platform, command in (("win32", "scoop"), ("darwin", "brew"), ("linux", "distribution")):
        monkeypatch.setattr(preflight.sys, "platform", platform)
        hint = preflight._install_hint()
        assert command in hint
        for other in set(expected.values()) - {command}:
            assert other not in hint
