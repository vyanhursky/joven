"""``joven ui`` — the whole workflow in one local page.

Choose a book, detect, review, render, verify, download: the same functions the
CLI calls, behind a handful of JSON routes on the same ``http.server`` that has
served the review page since the beginning. No framework, no build step, nothing
leaves the machine.

Security posture, for a server that now writes files and starts jobs:

* bound to ``127.0.0.1`` only;
* every mutating request carries a per-session token the page was handed on
  load, so a page open in another tab cannot post into this one;
* downloads are served by name from the book's own ``out/`` directory and
  nowhere else;
* the working directory is never served as static files.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import ConfigError, Settings
from ..config import load as load_config
from ..epub.archive import EpubError
from ..model import Sidecar
from ..review import ReviewState, _payload, build_context
from ..trace import Decision, load_trace
from ..translate import installed_models, ollama_available, openai_available, openai_models
from .jobs import Busy, Job, JobRunner, detect_job, render_job
from .workspace import Book, Workspace

TOKEN_HEADER = "X-Joven-Token"
MAX_UPLOAD = 200 * 1024 * 1024


class UIState:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.runner = JobRunner()
        self.runner.on_finish = self._job_finished
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self._reviews: dict[str, ReviewState] = {}
        self._traces: dict[str, tuple[float, list[Decision]]] = {}

    def settings(self) -> Settings:
        return load_config().settings

    def review(self, book: Book) -> ReviewState:
        """The review state for a book, built once and dropped when detect rewrites it."""
        with self.lock:
            state = self._reviews.get(book.sha)
            if state is None:
                sidecar = Sidecar.load(book.sidecar_path)
                state = ReviewState(
                    sidecar=sidecar,
                    path=book.sidecar_path,
                    context=build_context(book.epub, sidecar),
                    lock=threading.Lock(),
                )
                self._reviews[book.sha] = state
            return state

    def trace(self, book: Book) -> list[Decision]:
        """The parsed trace, cached by file modification time."""
        if not book.trace_path.is_file():
            return []
        stamp = book.trace_path.stat().st_mtime
        with self.lock:
            cached = self._traces.get(book.sha)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        decisions = load_trace(book.trace_path)
        with self.lock:
            self._traces[book.sha] = (stamp, decisions)
        return decisions

    def _job_finished(self, job: Job) -> None:
        with self.lock:
            self._reviews.pop(job.book, None)


def _trace_payload(decisions: list[Decision], query: dict[str, list[str]]) -> dict:
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.outcome] = counts.get(d.outcome, 0) + 1
    outcome = (query.get("outcome") or [""])[0]
    find = (query.get("find") or [""])[0].casefold()
    limit = int((query.get("limit") or ["100"])[0])
    matching = [
        d
        for d in decisions
        if (not outcome or d.outcome == outcome)
        and (not find or find in d.text.casefold() or find in d.tier2_translation.casefold())
    ]
    return {
        "counts": counts,
        "total": len(decisions),
        "matching": len(matching),
        "decisions": [
            {
                "href": d.href,
                "para_index": d.para_index,
                "segment_index": d.segment_index,
                "text": d.text,
                "outcome": d.outcome,
                "tier1_language": d.tier1_language,
                "tier1_confidence": d.tier1_confidence,
                "tier1_verdict": d.tier1_verdict,
                "tier1_reason": d.tier1_reason,
                "tier1_stripped": d.tier1_stripped,
                "tier2_used": d.tier2_used,
                "tier2_model": d.tier2_model,
                "tier2_is_spanish": d.tier2_is_spanish,
                "tier2_spanish_text": d.tier2_spanish_text,
                "tier2_translation": d.tier2_translation,
                "tier2_latency_s": d.tier2_latency_s,
                "tier2_raw": d.tier2_raw,
                "tier2_error": d.tier2_error,
            }
            for d in matching[:limit]
        ],
    }


_BOOK = r"(?P<sha>[0-9a-f]{64})"
ROUTES_GET = [
    (re.compile(r"^/$"), "page"),
    (re.compile(r"^/api/status$"), "status"),
    (re.compile(r"^/api/config$"), "config"),
    (re.compile(r"^/api/books$"), "books"),
    (re.compile(rf"^/api/books/{_BOOK}$"), "book"),
    (re.compile(rf"^/api/books/{_BOOK}/review$"), "review"),
    (re.compile(rf"^/api/books/{_BOOK}/trace$"), "trace"),
    (re.compile(rf"^/api/books/{_BOOK}/files/(?P<name>[^/]+)$"), "file"),
    (re.compile(r"^/api/jobs$"), "jobs"),
]
ROUTES_POST = [
    (re.compile(r"^/api/books$"), "add_book"),
    (re.compile(rf"^/api/books/{_BOOK}/detect$"), "detect"),
    (re.compile(rf"^/api/books/{_BOOK}/render$"), "render"),
    (re.compile(rf"^/api/books/{_BOOK}/annotations/(?P<annotation>[0-9a-f]{{12}})$"), "annotate"),
    (re.compile(r"^/api/jobs/cancel$"), "cancel"),
]


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, state: UIState, **kwargs) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:  # noqa: D102 - silence per-request logging
        pass

    # ------------------------------------------------------------- plumbing

    def _send(self, code: int, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: object) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _book(self, sha: str) -> Book | None:
        book = self.state.workspace.get(sha)
        if book is None:
            self._error(404, "no such book")
        return book

    def _body(self) -> bytes:
        # Drained before any early return: unread bytes make Windows reset the
        # connection instead of delivering the status code (see review.py).
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_UPLOAD:
            self.rfile.read(min(length, MAX_UPLOAD))
            raise ValueError("body too large")
        return self.rfile.read(length) if length > 0 else b""

    # -------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        for pattern, name in ROUTES_GET:
            match = pattern.match(url.path)
            if match:
                getattr(self, f"get_{name}")(parse_qs(url.query), **match.groupdict())
                return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        try:
            raw = self._body()
        except ValueError as exc:
            self._error(413, str(exc))
            return
        if self.headers.get(TOKEN_HEADER) != self.state.token:
            self._error(403, "missing or stale session token — reload the page")
            return
        url = urlparse(self.path)
        for pattern, name in ROUTES_POST:
            match = pattern.match(url.path)
            if match:
                try:
                    getattr(self, f"post_{name}")(raw, **match.groupdict())
                except Busy as exc:
                    self._error(409, str(exc))
                except (EpubError, ConfigError, ValueError, KeyError) as exc:
                    self._error(400, str(exc))
                return
        self._error(404, "not found")

    # ------------------------------------------------------------------ GET

    def get_page(self, query: dict) -> None:
        page = resources.files(__package__).joinpath("page.html").read_text(encoding="utf-8")
        page = page.replace("{{TOKEN}}", self.state.token)
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def get_status(self, query: dict) -> None:
        settings = self.state.settings()
        ollama = ollama_available(settings.ollama_url)
        openai = openai_available(settings.base_url, settings.api_key)
        self._json(
            200,
            {
                "ollama": {
                    "url": settings.ollama_url,
                    "up": ollama,
                    "models": installed_models(settings.ollama_url) if ollama else [],
                },
                "openai": {
                    "url": settings.base_url,
                    "up": openai,
                    "models": openai_models(settings.base_url, settings.api_key) if openai else [],
                },
            },
        )

    def get_config(self, query: dict) -> None:
        try:
            resolved = load_config()
        except ConfigError as exc:
            self._error(400, str(exc))
            return
        values = {
            name: ("••••" if name == "api_key" and getattr(resolved.settings, name) else
                   getattr(resolved.settings, name))
            for name in resolved.sources
        }
        self._json(200, {"settings": values, "sources": resolved.sources,
                         "files": [str(p) for p in resolved.files]})

    def get_books(self, query: dict) -> None:
        self._json(
            200,
            {
                "home": str(self.state.workspace.root),
                "books": [b.summary() for b in self.state.workspace.books()],
            },
        )

    def get_book(self, query: dict, sha: str) -> None:
        if (book := self._book(sha)) is None:
            return
        try:
            inspect = book.inspect()
        except EpubError as exc:
            self._error(400, str(exc))
            return
        self._json(200, {**book.summary(), "inspect": inspect})

    def get_review(self, query: dict, sha: str) -> None:
        if (book := self._book(sha)) is None:
            return
        if not book.sidecar_path.is_file():
            self._json(200, {"annotations": [], "counts": {}, "total": 0, "reviewed": 0,
                             "suspect": 0})
            return
        self._json(200, _payload(self.state.review(book)))

    def get_trace(self, query: dict, sha: str) -> None:
        if (book := self._book(sha)) is None:
            return
        self._json(200, _trace_payload(self.state.trace(book), query))

    def get_file(self, query: dict, sha: str, name: str) -> None:
        if (book := self._book(sha)) is None:
            return
        name = unquote(name)
        # By name, from one directory: no separators, no traversal, only outputs.
        candidates = {p.name: p for p in book.outputs()}
        path = candidates.get(name)
        if path is None:
            self._error(404, "no such output")
            return
        self._send(
            200,
            path.read_bytes(),
            "application/epub+zip",
            Content_Disposition=f'attachment; filename="{name}"',
        )

    def get_jobs(self, query: dict) -> None:
        self._json(200, self.state.runner.snapshot())

    # ----------------------------------------------------------------- POST

    def post_add_book(self, raw: bytes) -> None:
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type == "application/json":
            body = json.loads(raw or b"{}")
            path = Path(str(body.get("path", ""))).expanduser()
            book = self.state.workspace.add_path(path)
        else:
            if not raw:
                raise ValueError("empty upload")
            filename = unquote(self.headers.get("X-Filename", "book.epub"))
            book = self.state.workspace.add_bytes(raw, filename)
        self._json(200, book.summary())

    def post_detect(self, raw: bytes, sha: str) -> None:
        if (book := self._book(sha)) is None:
            return
        settings = self.state.settings()
        body = json.loads(raw or b"{}")
        limit = body.get("limit")
        job = self.state.runner.start(
            "detect",
            book.sha,
            detect_job(
                book,
                settings,
                backend=str(body.get("backend") or settings.backend),
                model=str(body.get("model") or settings.model),
                workers=max(1, int(body.get("workers") or settings.workers)),
                limit=int(limit) if limit else None,
                resume=bool(body.get("resume", True)),
            ),
        )
        self._json(202, job.snapshot())

    def post_render(self, raw: bytes, sha: str) -> None:
        if (book := self._book(sha)) is None:
            return
        job = self.state.runner.start("render", book.sha, render_job(book))
        self._json(202, job.snapshot())

    def post_annotate(self, raw: bytes, sha: str, annotation: str) -> None:
        if (book := self._book(sha)) is None:
            return
        if not book.sidecar_path.is_file():
            self._error(404, "no annotations yet")
            return
        body = json.loads(raw or b"{}")
        state = self.state.review(book)
        try:
            updated = state.update(
                annotation,
                body.get("status", "approved"),
                body.get("translation"),
                spans=body.get("spans"),
            )
        except KeyError:
            self._error(404, "unknown annotation")
            return
        self._json(
            200,
            {
                "id": updated.id,
                "status": str(updated.status),
                "translation": updated.translation,
                "spans": [list(s) for s in updated.spans],
                "counts": state.sidecar.counts(),
            },
        )

    def post_cancel(self, raw: bytes) -> None:
        self._json(200, {"cancelled": self.state.runner.cancel()})


def serve(
    *,
    home: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8770,
    open_browser: bool = True,
) -> None:
    """Serve the UI until interrupted."""
    state = UIState(Workspace(home))
    server = ThreadingHTTPServer((host, port), partial(_Handler, state=state))
    url = f"http://{host}:{port}/"
    print(f"joven ui — {url}")
    print(f"books live under {state.workspace.root}; Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
