"""Finding and invoking the external binaries, which is where Windows differs.

These are regression tests for a bug with a specific shape: the availability
check said yes and the invocation then died. Testing "is epubcheck installed"
would not have caught it, so what is asserted here is that the two agree.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from joven import external
from joven.console import force_utf8_output
from joven.verify import JAR_ENV, epubcheck_available, epubcheck_command


def test_resolve_returns_a_path_that_subprocess_can_actually_run() -> None:
    """The bug in one assertion.

    ``shutil.which`` honours PATHEXT, so on Windows it resolves launchers like
    ``epubcheck.CMD`` — which CreateProcess cannot start, because it only knows
    how to run a ``.exe``. Anything ``resolve`` reports has to be runnable as it
    stands, or the availability check is lying to its caller.
    """
    resolved = external.resolve("python") or external.resolve("python3")
    assert resolved is not None, "python must be on PATH for this test to mean anything"

    proc = external.run([resolved, "-c", "print('ran')"])
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ran"


def test_resolve_reports_a_missing_tool_as_none() -> None:
    assert external.resolve("joven-definitely-not-a-real-binary") is None


def test_run_decodes_utf8_output() -> None:
    """``text=True`` alone decodes with the locale encoding — cp1252 on Windows.

    The child here writes UTF-8 *bytes* deliberately, which is what the external
    tools emit; anything else would be testing the child's encoding rather than
    our decoding.
    """
    resolved = external.resolve("python") or external.resolve("python3")
    assert resolved is not None

    code = (
        "import sys; sys.stdout.buffer.write("
        "'matr\\u00edz caf\\u00e9 \\u2014 se fu\\u00e9'.encode('utf-8'))"
    )
    proc = external.run([resolved, "-c", code])
    assert proc.returncode == 0
    assert proc.stdout == "matríz café — se fué"


def test_run_replaces_undecodable_bytes_rather_than_raising() -> None:
    """The gate that matters: reporting a failure must not become a failure.

    epubcheck runs on a JVM whose stdout encoding on Windows is not guaranteed to
    be UTF-8, so bytes we cannot decode are a real possibility. Under the previous
    ``text=True`` the strict decoder raised UnicodeDecodeError from inside the
    error path — losing epubcheck's actual complaint behind a traceback about
    reading it.
    """
    resolved = external.resolve("python") or external.resolve("python3")
    assert resolved is not None

    # 0x93 0xF3 is legal cp1252 and not legal UTF-8.
    code = "import sys; sys.stdout.buffer.write(b'ERROR: \\x93\\xf3 bad')"
    proc = external.run([resolved, "-c", code])
    assert proc.returncode == 0
    assert "ERROR:" in proc.stdout
    assert "bad" in proc.stdout


def test_java_command_points_at_the_jar() -> None:
    command = external.java_command(__import__("pathlib").Path("/tmp/epubcheck.jar"))
    if command is None:
        pytest.skip("no JVM on PATH")
    assert command[1] == "-jar"
    assert command[2].endswith("epubcheck.jar")


class TestEpubcheckDiscovery:
    """The official epubcheck download is a jar and no launcher at all.

    On macOS and Linux a package manager supplies one, so PATH is enough. On
    Windows there is nothing for PATH to find, epubcheck was reported missing,
    and the check that validates our output downgraded itself to SKIPPED — the
    suite staying green while no longer checking the thing it exists to check.
    """

    def test_the_jar_env_var_is_enough_on_its_own(self, monkeypatch, tmp_path) -> None:
        jar = tmp_path / "epubcheck.jar"
        jar.write_bytes(b"")
        monkeypatch.setattr(
            external, "resolve", lambda name: None if name == "epubcheck" else "java"
        )
        monkeypatch.setenv(JAR_ENV, str(jar))

        command = epubcheck_command()
        assert command is not None
        assert command[:2] == ["java", "-jar"]
        assert epubcheck_available()

    def test_a_launcher_on_path_wins_over_the_jar(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(external, "resolve", lambda name: r"C:\tools\epubcheck.CMD")
        monkeypatch.setenv(JAR_ENV, str(tmp_path / "epubcheck.jar"))

        assert epubcheck_command() == [r"C:\tools\epubcheck.CMD"]

    def test_neither_route_available_is_reported_as_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(external, "resolve", lambda name: None)
        monkeypatch.delenv(JAR_ENV, raising=False)

        assert epubcheck_command() is None
        assert not epubcheck_available()


def test_force_utf8_output_leaves_an_already_utf8_stream_alone() -> None:
    """It must be a no-op on Unix rather than a platform branch to keep in sync."""
    before = sys.stdout.encoding
    force_utf8_output()
    assert sys.stdout.encoding.lower() == "utf-8"
    if before.lower() == "utf-8":
        assert sys.stdout.encoding == before


def test_the_cli_prints_utf8_to_a_redirected_stream() -> None:
    """End to end, through the real entry point.

    A console gets UTF-16 through the Windows console API and behaves; a pipe
    falls back to the locale encoding. This runs the CLI with its output captured
    — a pipe — so it fails on Windows without the fix.
    """
    code = (
        "from joven.console import force_utf8_output; force_utf8_output();"
        "print('matr\\u00edz \\u2014 se fu\\u00e9')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=True
    )
    # Decoded strictly on purpose: mojibake must fail this, not pass quietly.
    assert proc.stdout.decode("utf-8").strip() == "matríz — se fué"
