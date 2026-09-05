"""The browser UI's server: books, jobs, review, downloads, and the token.

The real handler on an ephemeral port, the stub backend, a temporary home. Jobs
run on a thread, so the tests poll them the way the page does.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from joven.model import Sidecar, Status
from joven.ui.server import TOKEN_HEADER, UIState, _Handler
from joven.ui.workspace import Workspace, safe_filename


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A server over an empty workspace, torn down after the test."""
    monkeypatch.setenv("JOVEN_CONFIG", str(tmp_path / "joven.toml"))
    (tmp_path / "joven.toml").write_text('backend = "stub"\n', encoding="utf-8")
    state = UIState(Workspace(tmp_path / "home"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, state=state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Client:
    def __init__(self, httpd, token: str | None) -> None:
        self.address = httpd.server_address
        self.token = token

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        conn = http.client.HTTPConnection(*self.address, timeout=10)
        sent = dict(headers or {})
        if method != "GET" and self.token and TOKEN_HEADER not in sent:
            sent[TOKEN_HEADER] = self.token
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, response.getheaders(), payload

    def get(self, path: str):
        status, _, payload = self.request("GET", path)
        return status, json.loads(payload) if payload.startswith(b"{") else payload

    def post(self, path: str, body: dict | None = None, **kwargs):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, _, payload = self.request("POST", path, raw, {**headers, **kwargs})
        return status, json.loads(payload) if payload else {}

    def upload(self, data: bytes, filename: str):
        status, _, payload = self.request(
            "POST",
            "/api/books",
            data,
            {"Content-Type": "application/epub+zip", "X-Filename": filename},
        )
        return status, json.loads(payload)

    def wait(self, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, jobs = self.get("/api/jobs")
            current = jobs["current"]
            if current and current["state"] != "running":
                return current
            time.sleep(0.05)
        raise AssertionError("job did not finish")


def _client(ui) -> Client:
    httpd, state = ui
    return Client(httpd, state.token)


def _with_book(ui, sample_epub: Path) -> tuple[Client, str]:
    client = _client(ui)
    status, book = client.upload(sample_epub.read_bytes(), "sample.epub")
    assert status == 200, book
    return client, book["sha"]


# ------------------------------------------------------------------- the page


def test_page_carries_the_session_token(ui) -> None:
    client = _client(ui)
    status, page = client.get("/")
    assert status == 200
    assert ui[1].token.encode() in page
    assert b"{{TOKEN}}" not in page


def test_mutating_requests_need_the_token(ui, sample_epub: Path) -> None:
    """A page in another tab does not have it, so it cannot write here."""
    httpd, state = ui
    anonymous = Client(httpd, token=None)
    status, body = anonymous.upload(sample_epub.read_bytes(), "sample.epub")
    assert status == 403
    assert "token" in body["error"]
    assert state.workspace.books() == []

    wrong = Client(httpd, token="nope")
    status, _ = wrong.post("/api/jobs/cancel")
    assert status == 403


# -------------------------------------------------------------------- books


def test_upload_stores_the_book_by_hash_and_lists_it(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    assert len(sha) == 64
    status, listing = client.get("/api/books")
    assert status == 200
    assert [b["sha"] for b in listing["books"]] == [sha]
    assert listing["books"][0]["title"] == "Test Book"
    assert listing["books"][0]["annotations"] is None

    status, detail = client.get(f"/api/books/{sha}")
    assert status == 200
    assert detail["inspect"]["words"] > 0
    assert detail["inspect"]["version"] == "2.0"
    assert [s["href"] for s in detail["inspect"]["spine"]] == [
        "OEBPS/part1.xhtml",
        "OEBPS/part2.xhtml",
    ]


def test_uploading_the_same_bytes_twice_is_one_book(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    _, again = client.upload(sample_epub.read_bytes(), "renamed.epub")
    assert again["sha"] == sha
    assert len(client.get("/api/books")[1]["books"]) == 1


def test_a_path_on_this_machine_can_be_added(ui, sample_epub: Path) -> None:
    client = _client(ui)
    status, book = client.post("/api/books", {"path": str(sample_epub)})
    assert status == 200, book
    assert book["filename"] == "sample.epub"
    assert sample_epub.is_file(), "the original is copied, never moved"


def test_garbage_is_refused_before_it_takes_up_residence(ui, tmp_path: Path) -> None:
    client = _client(ui)
    status, body = client.upload(b"not a zip", "junk.epub")
    assert status == 400
    assert client.get("/api/books")[1]["books"] == []
    status, body = client.post("/api/books", {"path": str(tmp_path / "missing.epub")})
    assert status == 400


def test_unknown_book_is_404(ui) -> None:
    client = _client(ui)
    assert client.get("/api/books/" + "0" * 64)[0] == 404
    assert client.get("/api/books/not-a-sha")[0] == 404


def test_filenames_from_the_browser_are_tamed() -> None:
    assert safe_filename("../../etc/passwd") == "passwd.epub"
    assert safe_filename("C:\\Users\\me\\book.epub") == "book.epub"
    assert safe_filename("El Niño (1).epub") == "El Ni_o (1).epub"
    assert safe_filename("") == "book.epub"


# --------------------------------------------------------------------- jobs


def test_detect_runs_as_a_job_and_writes_the_sidecar(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    status, job = client.post(f"/api/books/{sha}/detect", {"backend": "stub"})
    assert status == 202, job
    assert job["kind"] == "detect" and job["state"] == "running"

    done = client.wait()
    assert done["state"] == "done", done
    result = done["result"]
    assert result["found"] > 0
    assert result["paragraphs"] == result["paragraphs_total"]
    assert "segments considered" in result["report"]
    assert result["backend"] == "stub"

    book = ui[1].workspace.get(sha)
    assert book.sidecar_path.is_file() and book.trace_path.is_file()
    assert client.get("/api/books")[1]["books"][0]["annotations"] == result["found"]


def test_a_second_job_while_one_runs_is_refused(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    httpd, state = ui

    started = threading.Event()
    release = threading.Event()

    def slow(job):
        started.set()
        release.wait(5)
        return {}

    state.runner.start("detect", sha, slow)
    assert started.wait(2)
    try:
        status, body = client.post(f"/api/books/{sha}/render")
        assert status == 409
        assert "already running" in body["error"]
    finally:
        release.set()
    client.wait()


def test_cancel_stops_a_detect_and_keeps_what_it_answered(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    httpd, state = ui
    # Slow the stub down enough that a cancel lands mid-book.
    from joven.ui import jobs

    real = jobs.make_translator

    class Slow:
        name = "slow"

        def __init__(self, inner):
            self.inner = inner

        def adjudicate(self, text, context=""):
            time.sleep(0.05)
            return self.inner.adjudicate(text, context)

        def translate(self, text, context=""):
            time.sleep(0.05)
            return self.inner.translate(text, context)

    jobs.make_translator = lambda backend, model, settings: Slow(real("stub", model, settings))
    try:
        client.post(f"/api/books/{sha}/detect", {"backend": "stub"})
        time.sleep(0.15)
        status, body = client.post("/api/jobs/cancel")
        assert status == 200 and body["cancelled"] is True
        done = client.wait()
    finally:
        jobs.make_translator = real
    assert done["state"] == "cancelled"
    assert done["result"]["stopped"] is True
    assert done["result"]["paragraphs"] < done["result"]["paragraphs_total"]
    assert ui[1].workspace.get(sha).trace_path.is_file()


def test_a_failing_job_reports_instead_of_dying(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    status, job = client.post(f"/api/books/{sha}/render")  # no sidecar yet
    assert status == 202
    done = client.wait()
    assert done["state"] == "failed"
    assert "run detect first" in done["error"]
    # and the server is still serving
    assert client.get("/api/books")[0] == 200


def test_render_job_produces_files_verifies_them_and_serves_them(
    ui, sample_epub: Path
) -> None:
    client, sha = _with_book(ui, sample_epub)
    client.post(f"/api/books/{sha}/detect", {"backend": "stub"})
    client.wait()
    status, job = client.post(f"/api/books/{sha}/render")
    assert status == 202
    done = client.wait(60)
    assert done["state"] == "done", done
    result = done["result"]
    assert result["epub"].endswith(".annotated.epub")
    assert result["applied"] == result["renderable"] > 0
    assert result["findings"] and all(isinstance(f["ok"], bool) for f in result["findings"])
    assert result["passed"] is True, [f for f in result["findings"] if not f["ok"]]

    status, headers, payload = client.request("GET", f"/api/books/{sha}/files/{result['epub']}")
    assert status == 200
    assert payload[:2] == b"PK"
    assert dict(headers)["Content-Type"] == "application/epub+zip"
    assert result["epub"] in client.get("/api/books")[1]["books"][0]["outputs"]


def test_downloads_are_by_name_from_the_output_directory_only(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    for name in ("..%2Fmeta.json", "meta.json", "sample.epub", "..%2F..%2Fsecret.epub"):
        status, _ = client.get(f"/api/books/{sha}/files/{name}")
        assert status == 404, name


# ------------------------------------------------------------------- review


def test_review_payload_and_decisions_including_a_new_span(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    client.post(f"/api/books/{sha}/detect", {"backend": "stub"})
    client.wait()

    status, review = client.get(f"/api/books/{sha}/review")
    assert status == 200
    assert review["total"] > 0 and review["reviewed"] == 0
    target = next(a for a in review["annotations"] if "old man said" in a["source_text"])
    assert target["context"], "the book is on hand, so context is filled in"

    # approve one
    status, out = client.post(
        f"/api/books/{sha}/annotations/{target['id']}", {"status": "approved"}
    )
    assert status == 200 and out["status"] == "approved"

    # move the span to just the Spanish
    text = target["source_text"]
    end = text.index("?") + 1
    status, out = client.post(
        f"/api/books/{sha}/annotations/{target['id']}",
        {"status": "edited", "translation": target["translation"], "spans": [[0, end]]},
    )
    assert status == 200, out
    assert out["spans"] == [[0, end]] and out["status"] == "edited"
    saved = next(
        a for a in Sidecar.load(ui[1].workspace.get(sha).sidecar_path).annotations
        if a.id == target["id"]
    )
    assert saved.spans == [(0, end)] and saved.marker_offset == end
    assert saved.status is Status.EDITED

    # a span outside the paragraph is refused, and nothing changes
    status, out = client.post(
        f"/api/books/{sha}/annotations/{target['id']}", {"spans": [[0, len(text) + 5]]}
    )
    assert status == 400 and "outside" in out["error"]
    status, out = client.post(f"/api/books/{sha}/annotations/{target['id']}", {"spans": []})
    assert status == 400


def test_review_before_detect_is_empty_not_an_error(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    status, review = client.get(f"/api/books/{sha}/review")
    assert status == 200 and review["annotations"] == []
    status, _ = client.post(f"/api/books/{sha}/annotations/{'0' * 12}", {"status": "approved"})
    assert status == 404


# -------------------------------------------------------------------- trace


def test_trace_is_filterable(ui, sample_epub: Path) -> None:
    client, sha = _with_book(ui, sample_epub)
    assert client.get(f"/api/books/{sha}/trace")[1]["total"] == 0
    client.post(f"/api/books/{sha}/detect", {"backend": "stub"})
    client.wait()

    status, trace = client.get(f"/api/books/{sha}/trace")
    assert status == 200
    assert trace["total"] == sum(trace["counts"].values()) > 0
    assert trace["counts"]["annotated"] > 0

    status, only = client.get(f"/api/books/{sha}/trace?outcome=tier1_english&limit=3")
    assert {d["outcome"] for d in only["decisions"]} == {"tier1_english"}
    assert len(only["decisions"]) <= 3
    assert only["matching"] == trace["counts"]["tier1_english"]

    status, found = client.get(f"/api/books/{sha}/trace?find=cu%C3%A1ntos")
    assert found["matching"] >= 1
    assert all("cuántos" in d["text"].casefold() for d in found["decisions"])


# ------------------------------------------------------------------- status


def test_status_and_config_endpoints(ui) -> None:
    client = _client(ui)
    status, config = client.get("/api/config")
    assert status == 200
    assert config["settings"]["backend"] == "stub"
    assert config["sources"]["backend"].endswith("joven.toml")
    status, servers = client.get("/api/status")
    assert status == 200
    assert set(servers) == {"ollama", "openai"}
    assert isinstance(servers["ollama"]["up"], bool)
