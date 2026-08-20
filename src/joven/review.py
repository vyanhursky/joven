"""M5: the review pass — triage annotations before they reach the book.

Detection is good, not perfect. A full-book run produces ~600 annotations and
roughly 5-10% need a human eye (``Ay.`` translated as "He went away." being the
clearest example so far). Finding those *while reading a novel* is miserable, so
this front-loads it: one sitting, worst-first, before the book ever reaches the
device.

Design decisions worth stating:

**Suspect-first, then book order.** This was originally confidence-ascending, on
the reasoning that the detector's own uncertainty is the best available predictor
of error. The first full-book run showed it is not a predictor at all — quality
was uniformly good across every confidence band, because confidence measures how
hard the *language call* was, not whether the translation is right. What does
predict errors is :mod:`joven.suspicion`, so those sort to the top and carry a badge
saying why; everything else follows in narrative order, where a translation that
is wrong *for the scene* is easiest to spot. Stop whenever you like — the flagged
ones are done first.

**Writes straight to the sidecar.** Every decision is saved immediately, so
closing the tab never loses work, and re-rendering picks the edits up with no
export step. Human statuses are sticky across re-detection
(:meth:`joven.model.Sidecar.merge`), so reviewing is never wasted.

**Local, dependency-free, offline.** ``http.server`` and one self-contained page.
The book text never leaves the machine.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .epub.archive import EpubArchive
from .epub.document import iter_text_units
from .epub.package import read_package
from .model import Annotation, Sidecar, Status
from .suspicion import suspicions

CONTEXT_PARAGRAPHS = 2


@dataclass
class ReviewState:
    """The sidecar plus optional book context, guarded by a lock."""

    sidecar: Sidecar
    path: Path
    context: dict[str, str]  # annotation id -> preceding prose
    lock: threading.Lock

    def ordered(self) -> list[Annotation]:
        """Suspect annotations first, then book order; anything reviewed sinks.

        This used to be least-confident-first, on the assumption that is where the
        errors are. The first full-book run disproved it: detector confidence
        measures how hard the *language call* was, not whether the translation is
        right, and sampling every band showed uniformly good quality. See
        :mod:`joven.suspicion` for what does predict errors.

        Book order for the remainder is not just a fallback — reading in narrative
        sequence is how you notice a translation that is fine alone but wrong for
        the scene.
        """
        return sorted(
            self.sidecar.annotations,
            key=lambda a: (a.status.is_human, not self.suspicions(a), a.href, a.para_index),
        )

    def suspicions(self, annotation: Annotation) -> list[str]:
        """Why this annotation deserves a closer look; empty if nothing detected."""
        return suspicions(annotation.spanish_text, annotation.translation)

    def update(self, annotation_id: str, status: str, translation: str | None) -> Annotation:
        with self.lock:
            found = next(
                (a for a in self.sidecar.annotations if a.id == annotation_id), None
            )
            if found is None:
                raise KeyError(annotation_id)
            if translation is not None and translation.strip() != found.translation:
                found.translation = translation.strip()
                found.status = Status.EDITED
            else:
                found.status = Status(status)
            self.sidecar.save(self.path)
            return found


def build_context(epub: Path | None, sidecar: Sidecar) -> dict[str, str]:
    """Preceding prose for each annotation, so fragments can be judged in context.

    ``Bastante.`` is unjudgeable alone; with the paragraph before it, it is
    obvious. Optional — the review works without the book, just harder.
    """
    if epub is None:
        return {}
    archive = EpubArchive.read(epub)
    package = read_package(archive)

    per_doc: dict[str, list] = {}
    for href in package.spine_hrefs:
        if href in archive:
            per_doc[href] = iter_text_units(archive.get(href), href)

    context: dict[str, str] = {}
    for annotation in sidecar.annotations:
        units = per_doc.get(annotation.href)
        if not units:
            continue
        position = next(
            (i for i, u in enumerate(units) if u.index == annotation.para_index), None
        )
        if position is None:
            continue
        before = units[max(0, position - CONTEXT_PARAGRAPHS) : position]
        context[annotation.id] = " ".join(u.text.strip() for u in before)[-400:]
    return context


def _payload(state: ReviewState) -> dict:
    counts = state.sidecar.counts()
    ordered = state.ordered()
    return {
        "counts": counts,
        "total": len(state.sidecar.annotations),
        "reviewed": sum(v for k, v in counts.items() if k != "auto"),
        "suspect": sum(1 for a in ordered if state.suspicions(a) and not a.status.is_human),
        "annotations": [
            {
                **a.to_dict(),
                "spanish_text": a.spanish_text,
                "context": state.context.get(a.id, ""),
                # pre-split so the page can highlight without re-deriving offsets
                "segments": _segments(a),
                "suspicions": state.suspicions(a),
            }
            for a in ordered
        ],
    }


def _segments(a: Annotation) -> list[dict]:
    """Split the paragraph into Spanish / non-Spanish runs for highlighting."""
    out: list[dict] = []
    cursor = 0
    for start, end in sorted(a.spans):
        start, end = max(0, start), min(len(a.source_text), end)
        if start > cursor:
            out.append({"spanish": False, "text": a.source_text[cursor:start]})
        out.append({"spanish": True, "text": a.source_text[start:end]})
        cursor = end
    if cursor < len(a.source_text):
        out.append({"spanish": False, "text": a.source_text[cursor:]})
    return out


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, state: ReviewState, **kwargs) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:  # noqa: D102 - silence per-request logging
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(
                200, json.dumps(_payload(self.state)).encode(), "application/json"
            )
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self.path.startswith("/api/annotation/"):
            self._send(404, b"not found", "text/plain")
            return
        annotation_id = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            updated = self.state.update(
                annotation_id, body.get("status", "approved"), body.get("translation")
            )
        except KeyError:
            self._send(404, b'{"error":"unknown annotation"}', "application/json")
            return
        except Exception as exc:  # noqa: BLE001 - report to the page, keep serving
            self._send(
                400, json.dumps({"error": str(exc)}).encode(), "application/json"
            )
            return
        self._send(
            200,
            json.dumps(
                {
                    "id": updated.id,
                    "status": str(updated.status),
                    "translation": updated.translation,
                    "counts": self.state.sidecar.counts(),
                }
            ).encode(),
            "application/json",
        )


def serve(
    sidecar_path: Path,
    epub: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Serve the review UI until interrupted."""
    sidecar = Sidecar.load(sidecar_path)
    state = ReviewState(
        sidecar=sidecar,
        path=sidecar_path,
        context=build_context(epub, sidecar),
        lock=threading.Lock(),
    )
    server = ThreadingHTTPServer((host, port), partial(_Handler, state=state))
    url = f"http://{host}:{port}/"
    print(f"reviewing {len(sidecar.annotations)} annotations — {url}")
    print("decisions save to the sidecar immediately; Ctrl-C when done")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>joven review</title>
<style>
  :root {
    --bg: #faf9f7; --fg: #1c1a17; --muted: #6b6560; --line: #e0dcd6;
    --card: #fff; --es: #fdf3d7; --accent: #2f6f4f; --warn: #9a3b28;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#191817; --fg:#e9e5df; --muted:#9a938c; --line:#332f2b;
            --card:#211f1d; --es:#3a3320; --accent:#79b894; --warn:#e08a72; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  header { position:sticky; top:0; z-index:5; background:var(--bg);
           border-bottom:1px solid var(--line); padding:.7rem 1rem;
           display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }
  h1 { font-size:1rem; margin:0; font-weight:600; }
  .bar { flex:1; min-width:140px; height:6px; background:var(--line); border-radius:3px; }
  .bar > i { display:block; height:100%; background:var(--accent); border-radius:3px; }
  .counts { color:var(--muted); font-size:.82rem; font-variant-numeric:tabular-nums; }
  main { max-width:820px; margin:0 auto; padding:1rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:.9rem 1rem; margin-bottom:.8rem; }
  .card.done { opacity:.5; }
  .card.rejected { border-left:3px solid var(--warn); }
  .card.approved, .card.edited { border-left:3px solid var(--accent); }
  .meta { display:flex; gap:.7rem; align-items:center; flex-wrap:wrap;
          color:var(--muted); font-size:.75rem; margin-bottom:.5rem; }
  .conf { font-variant-numeric:tabular-nums; font-weight:600; }
  .conf.low { color:var(--warn); }
  .card.suspect { border-left:3px solid var(--warn); }
  /* The reason, not just a flag — a false positive should be dismissable at a
     glance rather than needing the whole paragraph re-read. */
  .sus { background:color-mix(in srgb, var(--warn) 18%, transparent);
         color:var(--warn); border-radius:3px; padding:.05rem .3rem;
         font-size:.72rem; font-weight:600; }
  .ctx { color:var(--muted); font-size:.85rem; font-style:italic; margin-bottom:.35rem; }
  .para { margin:0 0 .55rem; }
  .para mark { background:var(--es); color:inherit; padding:.05em .15em; border-radius:3px; }
  textarea { width:100%; font:inherit; color:inherit; background:transparent;
             border:1px solid var(--line); border-radius:6px; padding:.5rem;
             resize:vertical; min-height:2.6rem; }
  .row { display:flex; gap:.4rem; margin-top:.5rem; flex-wrap:wrap; }
  button { font:inherit; padding:.32rem .7rem; border-radius:6px; cursor:pointer;
           border:1px solid var(--line); background:transparent; color:inherit; }
  button.primary { border-color:var(--accent); color:var(--accent); }
  button.danger { border-color:var(--warn); color:var(--warn); }
  button:disabled { opacity:.4; cursor:default; }
  .pill { font-size:.7rem; padding:.1rem .45rem; border:1px solid var(--line);
          border-radius:99px; }
  .empty { color:var(--muted); text-align:center; padding:3rem 1rem; }
  kbd { font:inherit; font-size:.72rem; border:1px solid var(--line);
        border-radius:4px; padding:0 .3rem; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>joven review</h1>
  <div class="bar"><i id="bar" style="width:0%"></i></div>
  <div class="counts" id="counts">loading…</div>
  <label class="counts"><input type="checkbox" id="hide"/> hide reviewed</label>
  <span class="counts"><kbd>a</kbd> approve <kbd>r</kbd> reject <kbd>e</kbd> edit</span>
</header>
<main id="list"><div class="empty">loading…</div></main>
<script>
let data = {annotations: [], counts: {}, total: 0, reviewed: 0};
let hideReviewed = false;

const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function paragraph(a) {
  return a.segments.map(s =>
    s.spanish ? `<mark>${esc(s.text)}</mark>` : esc(s.text)).join('');
}

function render() {
  const bar = document.getElementById('bar');
  const pct = data.total ? Math.round(100 * data.reviewed / data.total) : 0;
  bar.style.width = pct + '%';
  document.getElementById('counts').textContent =
    `${data.reviewed}/${data.total} reviewed · ` +
    (data.suspect ? `${data.suspect} flagged · ` : '') +
    Object.entries(data.counts).filter(([,v]) => v).map(([k,v]) => `${v} ${k}`).join(', ');

  const list = document.getElementById('list');
  const shown = data.annotations.filter(a => !(hideReviewed && a.status !== 'auto'));
  if (!shown.length) {
    list.innerHTML = '<div class="empty">nothing left to review 🎉</div>';
    return;
  }
  list.innerHTML = shown.map(a => {
    const done = a.status !== 'auto';
    const low = a.detector_confidence < 0.9 ? ' low' : '';
    const sus = a.suspicions || [];
    const cls = [sus.length && !done ? 'suspect' : '', done ? 'done ' + a.status : ''].join(' ');
    return `<article class="card ${cls}" data-id="${a.id}" tabindex="0">
      <div class="meta">
        <span class="conf${low}">${a.detector_confidence.toFixed(2)}</span>
        <span>${esc(a.href.split('/').pop())}#${a.para_index}</span>
        ${a.model ? `<span class="pill">${esc(a.model)}</span>` : ''}
        ${done ? `<span class="pill">${a.status}</span>` : ''}
        ${sus.map(s => `<span class="sus">${esc(s)}</span>`).join('')}
      </div>
      ${a.context ? `<div class="ctx">…${esc(a.context)}</div>` : ''}
      <p class="para">${paragraph(a)}</p>
      <textarea rows="2">${esc(a.translation)}</textarea>
      <div class="row">
        <button class="primary" data-act="approved">Approve</button>
        <button data-act="save">Save edit</button>
        <button class="danger" data-act="rejected">Reject — not Spanish</button>
      </div>
    </article>`;
  }).join('');
}

async function decide(card, status) {
  const id = card.dataset.id;
  const translation = card.querySelector('textarea').value;
  const res = await fetch('/api/annotation/' + id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status, translation})
  });
  if (!res.ok) { alert('save failed: ' + (await res.text())); return; }
  const out = await res.json();
  const a = data.annotations.find(x => x.id === id);
  a.status = out.status; a.translation = out.translation;
  data.counts = out.counts;
  data.reviewed = Object.entries(out.counts)
    .filter(([k]) => k !== 'auto').reduce((n, [,v]) => n + v, 0);
  render();
}

document.addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const card = btn.closest('.card');
  const act = btn.dataset.act;
  decide(card, act === 'save' ? 'edited' : act);
});

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'TEXTAREA' || e.metaKey || e.ctrlKey) return;
  const card = document.activeElement?.closest?.('.card') || document.querySelector('.card');
  if (!card) return;
  if (e.key === 'a') { decide(card, 'approved'); }
  else if (e.key === 'r') { decide(card, 'rejected'); }
  else if (e.key === 'e') { card.querySelector('textarea').focus(); e.preventDefault(); }
});

document.getElementById('hide').addEventListener('change', e => {
  hideReviewed = e.target.checked; render();
});

fetch('/api/state').then(r => r.json()).then(d => { data = d; render(); });
</script>
</body>
</html>
"""
