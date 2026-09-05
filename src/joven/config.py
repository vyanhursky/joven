"""Configuration: where a setting comes from, and which one wins.

Precedence, highest first:

1. a command-line flag
2. a ``JOVEN_*`` environment variable — ``JOVEN_MODEL``, ``JOVEN_WORKERS``, …
3. ``./joven.toml`` in the working directory
4. the user config file — ``~/.config/joven/config.toml``, or
   ``%APPDATA%\\joven\\config.toml`` on Windows
5. the built-in default

Every setting is a flat key in the TOML file, spelled exactly as the field on
:class:`Settings`::

    model = "qwen3:8b"
    workers = 4
    ollama_url = "http://192.168.1.20:11434"

``JOVEN_CONFIG`` names one file to read *instead of* the two above, which is how
the tests keep a real filesystem out of the picture and how a one-off experiment
can carry its own settings without touching the ones you use.

The thresholds are here so an experiment can move them without editing source, but
the shipped defaults are the measured ones (DESIGN.md §2, model-selection.md) and
``joven config`` prints every value with its source so a stale experiment cannot
masquerade as the default.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OPENAI_URL = "http://localhost:8080/v1"

ENV_PREFIX = "JOVEN_"
EXPLICIT_FILE_ENV = "JOVEN_CONFIG"


class ConfigError(Exception):
    """A config file or variable says something that cannot be used."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Every tunable, with the value used when nothing sets it."""

    backend: str = "ollama"
    """``ollama`` | ``openai`` (any OpenAI-compatible local server) | ``stub`` | ``none``."""
    model: str = DEFAULT_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    base_url: str = DEFAULT_OPENAI_URL
    """The OpenAI-compatible server, for ``backend = "openai"``. Ends in ``/v1``."""
    api_key: str = ""
    """Bearer token, only for servers that insist on one. Still a local server."""
    workers: int = 1
    """Paragraphs in flight at once. Ollama needs ``OLLAMA_NUM_PARALLEL`` to match."""
    epubcheck_jar: str = ""
    """Path to ``epubcheck.jar`` when there is no launcher on PATH."""
    context_chars: int = 400
    """Preceding prose handed to the model with each paragraph."""
    accept_spanish: float = 0.90
    reject_english: float = 0.90
    accept_spanish_stripped: float = 0.95
    similarity_veto: float = 0.75


@dataclass(frozen=True, slots=True)
class Resolved:
    """The effective settings and, for each, what set it."""

    settings: Settings
    sources: dict[str, str]
    files: tuple[Path, ...]
    """The files that were consulted, whether or not they existed."""


def user_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "joven" / "config.toml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / "joven.toml"


def _coerce(name: str, value: Any, source: str) -> Any:
    """Turn a string from the environment, or a TOML value, into the field's type."""
    default = getattr(Settings(), name)
    kind = type(default)
    if kind is bool:  # pragma: no cover - no bool settings yet, kept for when there are
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{source}: {name} must be true or false, not {value!r}")
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ConfigError(f"{source}: {name} must be an integer, not {value!r}")
        try:
            return int(value)
        except ValueError:
            raise ConfigError(f"{source}: {name} must be an integer, not {value!r}") from None
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ConfigError(f"{source}: {name} must be a number, not {value!r}")
        try:
            return float(value)
        except ValueError:
            raise ConfigError(f"{source}: {name} must be a number, not {value!r}") from None
    if not isinstance(value, str):
        raise ConfigError(f"{source}: {name} must be a string, not {value!r}")
    return value


def _read_file(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    known = {f.name for f in fields(Settings)}
    for key in data:
        if key not in known:
            raise ConfigError(
                f"{path}: unknown setting {key!r} (known: {', '.join(sorted(known))})"
            )
    return data


def load(*, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Resolved:
    """Resolve the settings from files and environment. Flags are the caller's job.

    Raises :class:`ConfigError` for a file that does not parse, a key that does not
    exist, or a value of the wrong type — loudly, because a silently ignored
    ``worker = 4`` (singular) is a run that takes four times longer than intended
    and gives no hint why.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {f.name: getattr(Settings(), f.name) for f in fields(Settings)}
    sources = dict.fromkeys(values, "default")

    if explicit := env.get(EXPLICIT_FILE_ENV):
        files: tuple[Path, ...] = (Path(explicit),)
    else:
        files = (user_config_path(), project_config_path(cwd))

    for path in files:
        if not path.is_file():
            if explicit:
                raise ConfigError(f"{EXPLICIT_FILE_ENV} points at {path}, which does not exist")
            continue
        for key, raw in _read_file(path).items():
            values[key] = _coerce(key, raw, str(path))
            sources[key] = str(path)

    for name in values:
        var = ENV_PREFIX + name.upper()
        if var in env:
            values[name] = _coerce(name, env[var], var)
            sources[name] = var

    return Resolved(Settings(**values), sources, files)
