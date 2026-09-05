"""One job at a time, on a thread, with a snapshot the page can poll.

Ollama is the bottleneck, so there is no queue: a second job while one runs is
refused with :class:`Busy` and the page says so. A job is a function that takes
the :class:`Job` (to report progress and notice a cancel) and returns a result
dict; the runner records how it ended.

The two jobs the UI runs are :func:`detect_job` and :func:`render_job`. Both call
exactly what the CLI calls, so the page cannot drift from the command line.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from ..config import Settings
from ..detect.pipeline import Progress
from ..detect.pipeline import detect as run_detect
from ..detect.triage import Triager
from ..epub.archive import EpubArchive
from ..kepub import kepubify_available
from ..model import Sidecar
from ..render import render_epub
from ..render.strip import count_markers
from ..trace import Tracer, load_trace, reusable_answers
from ..translate import (
    Translator,
    get_translator,
    installed_models,
    ollama_available,
    openai_available,
)
from ..verify import verify as run_verify
from .workspace import Book


class Busy(Exception):
    """A job is already running."""


@dataclass
class Job:
    id: int
    kind: str
    book: str
    state: str = "running"  # running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    progress: dict | None = None
    result: dict = field(default_factory=dict)
    error: str = ""
    stop: bool = False
    """Set by cancel(); the job checks it between paragraphs."""

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "book": self.book,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "elapsed": (self.finished or time.time()) - self.started,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 1
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=20)
        self.on_finish: Callable[[Job], None] | None = None

    def start(self, kind: str, book: str, work: Callable[[Job], dict]) -> Job:
        with self._lock:
            if self.current is not None and self.current.state == "running":
                raise Busy(f"a {self.current.kind} job is already running")
            job = Job(id=self._next, kind=kind, book=book)
            self._next += 1
            self.current = job
        thread = threading.Thread(target=self._run, args=(job, work), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, work: Callable[[Job], dict]) -> None:
        try:
            job.result = work(job)
            job.state = "cancelled" if job.stop else "done"
        except Exception as exc:  # noqa: BLE001 - the page shows it; the server keeps serving
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = {"traceback": traceback.format_exc()}
            job.state = "failed"
        finally:
            job.finished = time.time()
            with self._lock:
                self.history.appendleft(job)
            if self.on_finish is not None:
                self.on_finish(job)

    def cancel(self) -> bool:
        with self._lock:
            job = self.current
        if job is None or job.state != "running":
            return False
        job.stop = True
        return True

    def snapshot(self) -> dict:
        with self._lock:
            current = self.current.snapshot() if self.current is not None else None
            history = [j.snapshot() for j in self.history]
        return {"current": current, "history": history}


# ------------------------------------------------------------------- the jobs


def make_translator(
    backend: str, model: str, settings: Settings
) -> Translator | None:
    """The CLI's pre-flight checks, raising a message the page can show verbatim."""
    if backend in {"none", ""}:
        return None
    if backend == "ollama":
        if not ollama_available(settings.ollama_url):
            raise RuntimeError(f"ollama is not running at {settings.ollama_url}")
        available = installed_models(settings.ollama_url)
        if model not in available:
            raise RuntimeError(
                f"model {model!r} is not installed at {settings.ollama_url}; "
                f"available: {available or 'none'} — try: ollama pull {model}"
            )
        return get_translator("ollama", model, base_url=settings.ollama_url)
    if backend == "openai":
        if not openai_available(settings.base_url, settings.api_key):
            raise RuntimeError(f"nothing answered GET {settings.base_url}/models")
        return get_translator(
            "openai", model, base_url=settings.base_url, api_key=settings.api_key
        )
    if backend == "stub":
        return get_translator("stub")
    raise RuntimeError(f"unknown backend {backend!r}")


def detect_job(
    book: Book,
    settings: Settings,
    *,
    backend: str,
    model: str,
    workers: int,
    limit: int | None,
    resume: bool,
) -> Callable[[Job], dict]:
    def run(job: Job) -> dict:
        translator = make_translator(backend, model, settings)

        recorded = {}
        if resume and book.trace_path.is_file():
            recorded = reusable_answers(load_trace(book.trace_path))

        def on_progress(progress: Progress) -> None:
            job.progress = asdict(progress)

        triager = Triager(
            accept_spanish=settings.accept_spanish,
            reject_english=settings.reject_english,
            accept_spanish_stripped=settings.accept_spanish_stripped,
        )
        with Tracer(path=book.trace_path) as tracer:
            sidecar, result = run_detect(
                book.epub,
                triager=triager,
                translator=translator,
                tracer=tracer,
                limit=limit,
                resume=recorded,
                workers=workers,
                context_chars=settings.context_chars,
                similarity_veto=settings.similarity_veto,
                on_progress=on_progress,
                should_stop=lambda: job.stop,
            )
            report = tracer.format_report()
            counts = tracer.summary()
            errors = [
                {"text": d.text[:80], "error": d.tier2_error[:120]} for d in tracer.errors()[:5]
            ]

        # Merge, as the CLI does by default: human decisions survive, auto entries
        # are replaced, and a rejected id is never re-added.
        merged = None
        if book.sidecar_path.is_file():
            existing = Sidecar.load(book.sidecar_path)
            merged = existing.merge(result.annotations)
            existing.source_sha256 = sidecar.source_sha256
            existing.title = sidecar.title
            existing.save(book.sidecar_path)
            total = len(existing.annotations)
        else:
            sidecar.save(book.sidecar_path)
            total = len(sidecar.annotations)

        return {
            "report": report,
            "counts": counts,
            "found": len(result.annotations),
            "sidecar_total": total,
            "merged": merged,
            "paragraphs": result.paragraphs_scanned,
            "paragraphs_total": result.paragraphs_total,
            "recalled": result.llm_recalled,
            "errors": errors,
            "backend": translator.name if translator is not None else "none",
            "stopped": job.stop,
        }

    return run


def render_job(book: Book) -> Callable[[Job], dict]:
    def run(job: Job) -> dict:
        if not book.sidecar_path.is_file():
            raise RuntimeError("no annotations yet — run detect first")
        if markers := count_markers(EpubArchive.read(book.epub)):
            raise RuntimeError(
                f"this file already carries {markers} Joven footnote markers — it is a "
                "Joven output, and rendering onto it would double every marker. Add the "
                "original instead, or recover one with `joven strip`."
            )
        sidecar = Sidecar.load(book.sidecar_path)
        result = render_epub(
            book.epub, sidecar, book.out_dir, make_kepub=kepubify_available()
        )
        findings = run_verify(result.epub_path, book.epub, result.kepub_path)
        return {
            "epub": result.epub_path.name,
            "kepub": result.kepub_path.name if result.kepub_path else None,
            "kepubify": kepubify_available(),
            "applied": result.annotations_applied,
            "documents": result.documents_touched,
            "upgraded": result.upgraded_to_epub3,
            "skipped": result.skipped,
            "renderable": len(sidecar.renderable()),
            "findings": [
                {"ok": f.ok, "check": f.check, "detail": f.detail} for f in findings
            ],
            "passed": all(f.ok for f in findings),
        }

    return run
