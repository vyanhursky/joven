"""Is this machine ready to annotate a book, and if not, what is missing?

Every dependency Joven has fails in its own way, and several fail quietly: a
missing ``epubcheck`` turns one of twelve integrity checks into ``SKIPPED`` and
says nothing else, a missing ``kepubify`` is only noticed at the end of a render,
and a model that was never pulled surfaces as an HTTP error from the first
paragraph. Someone who knows the tool reads those correctly. A reader who
downloaded an app does not.

So the checks live here rather than in the CLI, because the browser UI needs the
same answers — it opens on them when something required is missing. Nothing in
this module discovers anything on its own: it composes the availability helpers
that already exist beside each dependency (:mod:`joven.external`,
:mod:`joven.kepub`, :mod:`joven.verify`, :mod:`joven.translate`) so that what
``doctor`` reports and what a run actually does cannot disagree.

Severity is the honest consequence, not a mood:

``required``
    Detection cannot run at all. A model server, and the model itself.
``recommended``
    The workflow completes but the output is worse. Without ``kepubify`` there is
    no KEPUB, and the plain EPUB renders less well on a Kobo.
``optional``
    Something is not checked. Without Java and ``epubcheck``, eleven of the twelve
    integrity checks still run — the text-preservation invariant among them — so
    this is a narrower gate, not an unverified output.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass

from . import external
from .config import DEFAULT_MODEL, Resolved
from .config import load as load_config
from .kepub import kepubify_available
from .translate import (
    installed_models,
    ollama_available,
    openai_available,
    openai_models,
)
from .verify import JAR_ENV, epubcheck_command

REQUIRED = "required"
RECOMMENDED = "recommended"
OPTIONAL = "optional"

OK = "ok"
WARN = "warn"
FAIL = "fail"

OLLAMA_DOWNLOAD = "https://ollama.com/download"

UI_PORT = 8770


@dataclass(frozen=True, slots=True)
class Check:
    """One dependency, what it found, and what to do when it found nothing."""

    name: str
    state: str
    """``ok`` | ``warn`` | ``fail``."""
    detail: str
    """What was found, in a phrase. Shown next to the name."""
    hint: str = ""
    """What to do about it. Empty when there is nothing to do."""
    severity: str = OPTIONAL
    fix: str = ""
    """A remedy the UI can offer as a button: ``pull-model`` or empty."""

    @property
    def blocking(self) -> bool:
        """A failure that stops a book being annotated at all."""
        return self.state == FAIL and self.severity == REQUIRED

    def __str__(self) -> str:
        mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}[self.state]
        line = f"[{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")
        return line + (f"\n         {self.hint}" if self.hint else "")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "hint": self.hint,
            "severity": self.severity,
            "fix": self.fix,
            "blocking": self.blocking,
        }


def _model_server(resolved: Resolved) -> list[Check]:
    """The model server and the model on it — the only genuinely required pair.

    Two checks, not one, because "Ollama is running" and "the model you configured
    exists on it" fail for different reasons and have different remedies, and
    collapsing them produces the unhelpful kind of error message.
    """
    settings = resolved.settings
    backend = settings.backend

    if backend == "stub":
        return [
            Check(
                "model server",
                WARN,
                "backend = 'stub' — translations are canned",
                "Set backend to 'ollama' for real translations.",
                REQUIRED,
            )
        ]
    if backend == "none":
        return [
            Check("model server", WARN, "backend = 'none' — detection is disabled", "", REQUIRED)
        ]

    if backend == "openai":
        url = settings.base_url
        if not openai_available(url, settings.api_key):
            return [
                Check(
                    "model server",
                    FAIL,
                    f"nothing answers at {url}",
                    "Start your OpenAI-compatible server, or point base_url at one.",
                    REQUIRED,
                )
            ]
        served = openai_models(url, settings.api_key)
        server = Check("model server", OK, f"OpenAI-compatible at {url}", "", REQUIRED)
        if served and settings.model not in served:
            listed = ", ".join(served[:4])
            return [
                server,
                Check(
                    "model",
                    FAIL,
                    f"{settings.model!r} is not served by {url}",
                    f"The server lists: {listed}",
                    REQUIRED,
                ),
            ]
        return [server, Check("model", OK, settings.model, "", REQUIRED)]

    url = settings.ollama_url
    if not ollama_available(url):
        return [
            Check(
                "model server",
                FAIL,
                f"Ollama is not answering at {url}",
                f"Install it from {OLLAMA_DOWNLOAD}, then start it and run this again.",
                REQUIRED,
            )
        ]
    server = Check("model server", OK, f"Ollama at {url}", "", REQUIRED)

    # Ollama reports 'qwen3:8b'; a bare 'qwen3' means the ':latest' tag, and the
    # two spellings have to match the way the server spells them or a pulled model
    # is reported missing.
    pulled = installed_models(url)
    wanted = settings.model if ":" in settings.model else f"{settings.model}:latest"
    if wanted not in pulled and settings.model not in pulled:
        # The size is only quoted for the model we actually measured; guessing at
        # the download for someone else's choice of model is worse than silence.
        size = "  (about 6 GB)" if settings.model.startswith(DEFAULT_MODEL) else ""
        return [
            server,
            Check(
                "model",
                FAIL,
                f"{settings.model!r} has not been pulled",
                f"Run: ollama pull {settings.model}{size}",
                REQUIRED,
                fix="pull-model",
            ),
        ]
    return [server, Check("model", OK, settings.model, "", REQUIRED)]


def _kepubify() -> Check:
    if kepubify_available():
        return Check("kepubify", OK, "found", "", RECOMMENDED)
    return Check(
        "kepubify",
        WARN,
        "not found",
        "Without it you get an EPUB but no KEPUB, which renders worse on a Kobo.",
        RECOMMENDED,
    )


def _java_hint() -> str:
    """How to get a JVM, on the platform actually running.

    Worth the branch: a hint is only useful if it names a command that exists
    here, and telling a Linux reader to run ``winget`` is worse than saying
    nothing.
    """
    if sys.platform == "win32":
        return "epubcheck is a JAR and needs a JVM: winget install Microsoft.OpenJDK.21"
    if sys.platform == "darwin":
        return "epubcheck is a JAR and needs a JVM: brew install openjdk"
    return "epubcheck is a JAR and needs a JVM: install your distribution's JRE (e.g. default-jre)"


def _epubcheck() -> list[Check]:
    """Java and epubcheck, reported separately because the jar is useless alone."""
    java = external.resolve("java")
    command = epubcheck_command()
    if command is not None:
        return [
            Check("java", OK, "found", "", OPTIONAL),
            Check("epubcheck", OK, "found", "", OPTIONAL),
        ]
    if java is None:
        return [
            Check("java", WARN, "not found", _java_hint(), OPTIONAL),
            Check(
                "epubcheck",
                WARN,
                "cannot run without Java",
                "Eleven of the twelve integrity checks still run.",
                OPTIONAL,
            ),
        ]
    return [
        Check("java", OK, "found", "", OPTIONAL),
        Check(
            "epubcheck",
            WARN,
            "not found",
            f"Put epubcheck on PATH, or set {JAR_ENV} to epubcheck.jar.",
            OPTIONAL,
        ),
    ]


def _home() -> Check:
    """The books directory has to be writable before a book can be dropped on it."""
    # Imported here, not at module scope: ``joven.ui`` imports the server, which
    # imports this module for /api/doctor, so a top-level import of anything under
    # ``joven.ui`` is a cycle. By the time this runs, both modules exist.
    from .ui.workspace import default_home

    home = default_home()
    probe = home / ".writable"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return Check(
            "books directory",
            FAIL,
            f"{home} is not writable ({exc.strerror or exc})",
            "Set JOVEN_HOME to somewhere you can write.",
            REQUIRED,
        )
    return Check("books directory", OK, str(home), "", OPTIONAL)


def _port(port: int = UI_PORT) -> Check:
    """Whether the UI's default port is free.

    Informational only: ``joven ui --port`` moves it, and the UI asking this
    question about the port it is already serving on would answer 'in use'.
    """
    with socket.socket() as sock:
        sock.settimeout(0.5)
        free = sock.connect_ex(("127.0.0.1", port)) != 0
    if free:
        return Check("port", OK, f"{port} is free", "", OPTIONAL)
    return Check(
        "port",
        WARN,
        f"something is already listening on {port}",
        f"That may be Joven itself. Otherwise use: joven ui --port {port + 1}",
        OPTIONAL,
    )


def _python() -> Check:
    """Which interpreter is running, and whether it came from the bundled app."""
    version = ".".join(str(n) for n in sys.version_info[:3])
    if getattr(sys, "frozen", False):
        return Check("python", OK, f"{version} (bundled with the app)", "", OPTIONAL)
    return Check("python", OK, f"{version} at {sys.executable}", "", OPTIONAL)


def run_checks(*, port: int = UI_PORT, check_port: bool = True) -> list[Check]:
    """Every check, in the order a reader should read them.

    A broken config file is itself a finding rather than an exception, because
    ``doctor`` is the command someone runs precisely when something is wrong.
    """
    try:
        resolved = load_config()
    except Exception as exc:  # noqa: BLE001 - ConfigError, but a bad file can surprise
        return [
            Check(
                "configuration",
                FAIL,
                str(exc),
                "Fix or delete the file, then run this again.",
                REQUIRED,
            )
        ]

    checks = [_python(), *_model_server(resolved), _kepubify(), *_epubcheck(), _home()]
    if check_port:
        checks.append(_port(port))
    return checks


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking]
