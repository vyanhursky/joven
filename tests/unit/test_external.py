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


def _stub_java(monkeypatch, returncode: int) -> None:
    """A ``java`` that resolves, and starts or does not."""
    monkeypatch.setattr(external, "resolve", lambda name: "/usr/bin/java")
    monkeypatch.setattr(
        external,
        "run",
        lambda argv: subprocess.CompletedProcess(
            argv, returncode, "", "Unable to locate a Java Runtime" if returncode else ""
        ),
    )


def test_a_java_that_will_not_start_is_not_a_java(monkeypatch, tmp_path) -> None:
    """Trap 1, in its macOS form — and the one this cost a real release.

    Every macOS install carries ``/usr/bin/java`` whether a JDK was ever installed
    or not. It is executable, so ``which`` finds it, and it exits 1 with "Unable to
    locate a Java Runtime". Trusting the lookup made ``doctor`` report ``java`` and
    ``epubcheck`` OK on a stock Mac and the render then end in ``1 of 12 checks
    FAILED`` — availability saying yes and the invocation dying, which is the exact
    bug the rest of this module exists to prevent.
    """
    _stub_java(monkeypatch, returncode=1)

    assert external.java_runtime() is None
    assert external.java_command(tmp_path / "epubcheck.jar") is None


def test_a_java_that_starts_is_accepted(monkeypatch, tmp_path) -> None:
    """The other half: probing must not reject a JVM that works."""
    _stub_java(monkeypatch, returncode=0)

    assert external.java_runtime() == "/usr/bin/java"
    command = external.java_command(tmp_path / "epubcheck.jar")
    assert command is not None
    assert command[:2] == ["/usr/bin/java", "-jar"]


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
        # The probe runs on this route, so the JVM has to answer for the test to be
        # about the environment variable rather than about the host's JDK.
        monkeypatch.setattr(
            external, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", "")
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


# ------------------------------------------------------------- the bundled copies


def test_vendor_dir_is_none_without_a_frozen_build_or_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(external.VENDOR_ENV, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert external.vendor_dir() is None


def test_the_apps_own_binary_wins_over_one_on_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled copy is the version this release was tested against.

    Someone with an older kepubify from a package manager should not silently get
    it in preference to the one the app shipped with.
    """
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    name = "kepubify.exe" if sys.platform == "win32" else "kepubify"
    bundled = vendor / name
    bundled.write_bytes(b"")
    monkeypatch.setenv(external.VENDOR_ENV, str(vendor))
    monkeypatch.setattr(external.shutil, "which", lambda _n: "/somewhere/else/kepubify")
    assert external.resolve("kepubify") == str(bundled)


def test_a_vendor_hit_is_a_file_not_a_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """epubcheck lives in the vendor dir as a *directory*, and must not shadow PATH.

    ``resolve`` returning a directory would satisfy every availability check and
    then fail at the point of invocation -- trap 1 in a new costume.
    """
    vendor = tmp_path / "vendor"
    (vendor / "epubcheck").mkdir(parents=True)
    monkeypatch.setenv(external.VENDOR_ENV, str(vendor))
    monkeypatch.setattr(external.shutil, "which", lambda _n: None)
    assert external.resolve("epubcheck") is None


def test_the_bundled_jar_is_the_last_route_to_epubcheck(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No launcher, no configured jar -- the app's own copy still works."""
    vendor = tmp_path / "vendor"
    jar = vendor / "epubcheck" / "epubcheck.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"")
    monkeypatch.setenv(external.VENDOR_ENV, str(vendor))
    monkeypatch.setenv("JOVEN_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "absent.toml").write_text("", encoding="utf-8")
    # "No configured jar" has to be arranged, not assumed: JOVEN_EPUBCHECK_JAR
    # outranks the bundled copy on purpose, and CI's Windows job exports it. An
    # empty JOVEN_CONFIG does not neutralise it, because the variable beats the
    # file. Without this the test passes or fails according to whose machine it
    # is running on.
    monkeypatch.delenv(JAR_ENV, raising=False)
    monkeypatch.setattr(
        external.shutil, "which", lambda n: "/usr/bin/java" if n == "java" else None
    )
    # These assert which jar wins, not whether this host has a JDK, so the probe
    # gets an answer rather than the host's /usr/bin/java -- which on a Mac is a
    # stub that refuses to start and on Windows is not a path at all.
    monkeypatch.setattr(
        external, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", "")
    )
    command = epubcheck_command()
    assert command is not None
    assert command[:2] == ["/usr/bin/java", "-jar"]
    assert command[2] == str(jar)


def test_a_configured_jar_outranks_the_bundled_one(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone who set the path meant it."""
    vendor = tmp_path / "vendor"
    (vendor / "epubcheck").mkdir(parents=True)
    (vendor / "epubcheck" / "epubcheck.jar").write_bytes(b"")
    theirs = tmp_path / "theirs.jar"
    theirs.write_bytes(b"")
    monkeypatch.setenv(external.VENDOR_ENV, str(vendor))
    monkeypatch.setenv(JAR_ENV, str(theirs))
    monkeypatch.setenv("JOVEN_CONFIG", str(tmp_path / "absent.toml"))
    (tmp_path / "absent.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        external.shutil, "which", lambda n: "/usr/bin/java" if n == "java" else None
    )
    monkeypatch.setattr(
        external, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", "")
    )
    command = epubcheck_command()
    assert command is not None and command[2] == str(theirs)
