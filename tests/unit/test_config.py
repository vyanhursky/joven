"""Configuration: five sources, one order, and loud failures for a wrong file."""

from __future__ import annotations

from pathlib import Path

import pytest

from joven import config
from joven.config import ConfigError, Settings, load


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user-level file somewhere disposable, so the real one cannot leak in."""
    user = tmp_path / "user" / "config.toml"
    monkeypatch.setattr(config, "user_config_path", lambda: user)
    return tmp_path


def test_defaults_when_nothing_is_set(home: Path) -> None:
    resolved = load(env={}, cwd=home)
    assert resolved.settings == Settings()
    assert set(resolved.sources.values()) == {"default"}


def test_project_file_overrides_the_default(home: Path) -> None:
    (home / "joven.toml").write_text('model = "gemma3:12b"\nworkers = 4\n', encoding="utf-8")
    resolved = load(env={}, cwd=home)
    assert resolved.settings.model == "gemma3:12b"
    assert resolved.settings.workers == 4
    assert resolved.sources["model"] == str(home / "joven.toml")
    assert resolved.sources["backend"] == "default"


def test_project_file_outranks_the_user_file(home: Path) -> None:
    user = home / "user" / "config.toml"
    user.parent.mkdir()
    user.write_text('model = "from-user"\nworkers = 2\n', encoding="utf-8")
    (home / "joven.toml").write_text('model = "from-project"\n', encoding="utf-8")
    resolved = load(env={}, cwd=home)
    assert resolved.settings.model == "from-project"
    assert resolved.settings.workers == 2  # untouched by the project file, so the user's


def test_environment_outranks_every_file(home: Path) -> None:
    (home / "joven.toml").write_text('model = "from-file"\n', encoding="utf-8")
    resolved = load(env={"JOVEN_MODEL": "from-env"}, cwd=home)
    assert resolved.settings.model == "from-env"
    assert resolved.sources["model"] == "JOVEN_MODEL"


def test_environment_strings_take_the_fields_type(home: Path) -> None:
    resolved = load(env={"JOVEN_WORKERS": "4", "JOVEN_ACCEPT_SPANISH": "0.8"}, cwd=home)
    assert resolved.settings.workers == 4
    assert resolved.settings.accept_spanish == 0.8


def test_explicit_file_replaces_discovery(home: Path) -> None:
    """JOVEN_CONFIG is *the* file, not one more file."""
    (home / "joven.toml").write_text('model = "from-project"\n', encoding="utf-8")
    explicit = home / "experiment.toml"
    explicit.write_text("workers = 8\n", encoding="utf-8")
    resolved = load(env={"JOVEN_CONFIG": str(explicit)}, cwd=home)
    assert resolved.settings.workers == 8
    assert resolved.settings.model == Settings().model
    assert resolved.files == (explicit,)


def test_explicit_file_that_does_not_exist_is_an_error(home: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load(env={"JOVEN_CONFIG": str(home / "nope.toml")}, cwd=home)


def test_unknown_key_is_an_error_naming_it(home: Path) -> None:
    """`worker = 4` must not be silently ignored — that is a run four times slower."""
    (home / "joven.toml").write_text("worker = 4\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown setting 'worker'"):
        load(env={}, cwd=home)


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('workers = "four"\n', "workers must be an integer"),
        ("workers = 2.5\n", "workers must be an integer"),
        ('accept_spanish = "high"\n', "accept_spanish must be a number"),
        ("model = 8\n", "model must be a string"),
        ("workers = true\n", "workers must be an integer"),
    ],
)
def test_wrong_types_are_errors(home: Path, toml: str, message: str) -> None:
    (home / "joven.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load(env={}, cwd=home)


def test_wrong_type_in_the_environment_names_the_variable(home: Path) -> None:
    with pytest.raises(ConfigError, match="JOVEN_WORKERS: workers must be an integer"):
        load(env={"JOVEN_WORKERS": "four"}, cwd=home)


def test_unparseable_toml_is_an_error_naming_the_file(home: Path) -> None:
    (home / "joven.toml").write_text("model = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="joven.toml"):
        load(env={}, cwd=home)


def test_the_epubcheck_jar_variable_still_works(home: Path) -> None:
    """The pre-config spelling, JOVEN_EPUBCHECK_JAR, is just the setting's env name."""
    resolved = load(env={"JOVEN_EPUBCHECK_JAR": "C:/tools/epubcheck.jar"}, cwd=home)
    assert resolved.settings.epubcheck_jar == "C:/tools/epubcheck.jar"
