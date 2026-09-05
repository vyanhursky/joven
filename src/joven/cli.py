"""``joven`` command line interface."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, TextColumn, TimeRemainingColumn
from rich.progress import Progress as RichProgress

from .config import ConfigError, Resolved
from .config import load as load_config
from .console import force_utf8_output
from .detect.pipeline import Progress
from .detect.pipeline import detect as run_detect
from .detect.triage import Triager
from .epub.archive import EpubArchive, EpubError
from .epub.document import iter_text_units
from .epub.package import read_package
from .kepub import INSTALL_HINT, KepubError
from .model import Annotation, Sidecar, Status, normalize, occurrence_indices
from .render import RenderError, render_epub
from .review import serve as serve_review
from .suspicion import suspicions
from .trace import Outcome, Tracer, load_trace, reusable_answers
from .translate import (
    BACKENDS,
    get_translator,
    installed_models,
    ollama_available,
    openai_available,
    openai_models,
)
from .verify import verify as run_verify

app = typer.Typer(
    add_completion=False,
    help="Insert clickable translation footnotes for foreign-language passages in an EPUB.",
    no_args_is_help=True,
)


def _load(path: Path) -> EpubArchive:
    try:
        return EpubArchive.read(path)
    except EpubError as exc:
        _fail(exc)


def _fail(exc: Exception) -> NoReturn:
    """Report an unusable book as an error, not a traceback.

    ``DocumentError`` is an ``EpubError``, so this covers both the archive being
    wrong and an individual XHTML document being unparseable — which is what a
    reader gets when a book uses a named entity we cannot resolve.
    """
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(2) from exc


def _error(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _warn(message: str) -> None:
    typer.secho(f"warning: {message}", fg=typer.colors.YELLOW, err=True)


def _settings() -> Resolved:
    """The effective configuration, or a clear error about the file that broke it."""
    try:
        return load_config()
    except ConfigError as exc:
        _fail(exc)


def _warn_if_different_file(sidecar: Sidecar, sidecar_path: Path, epub: Path) -> None:
    """Say plainly when a sidecar was detected from some other file.

    A warning, not an error: the same edition re-saved by Calibre hashes
    differently and renders fine. A different *edition* fails per paragraph with
    "source text drifted" deep inside rendering — this line is what makes that
    error make sense when it arrives.
    """
    if sidecar.source_matches(epub) is False:
        _warn(
            f"{sidecar_path.name} was detected from a different file than "
            f"{epub.name} (sha256 differs). The same edition re-saved is fine; a "
            "different edition will fail with 'source text drifted'."
        )


def _locate(sidecar: Sidecar, find: str | None, ident: str | None) -> Annotation:
    """Exactly one annotation, by a substring of its paragraph or by id."""
    if bool(find) == bool(ident):
        _error("give exactly one of --find or --id")
    if ident:
        for annotation in sidecar.annotations:
            if annotation.id == ident:
                return annotation
        _error(f"no annotation with id {ident!r}")
    needle = normalize(find or "")
    matches = [a for a in sidecar.annotations if needle in normalize(a.source_text)]
    if not matches:
        _error(f"no annotation's paragraph contains {find!r}")
    if len(matches) > 1:
        typer.secho(f"{len(matches)} annotations match — be more specific:", fg=typer.colors.YELLOW)
        for a in matches[:8]:
            typer.echo(f"  {a.id}  {a.href}#{a.para_index}  {a.source_text[:72]!r}")
        raise typer.Exit(1)
    return matches[0]


def _parse_range(text: str) -> tuple[int | None, int | None]:
    """``START:STOP`` with either side optional, as a Python slice reads it."""
    start_text, sep, stop_text = text.partition(":")
    if not sep:
        _error(f"--range wants START:STOP, got {text!r}")
    try:
        start = int(start_text) if start_text else None
        stop = int(stop_text) if stop_text else None
    except ValueError:
        _error(f"--range wants integers, got {text!r}")
    if (start is not None and start < 0) or (stop is not None and stop < 0):
        _error("--range does not take negative positions")
    return start, stop


# ----------------------------------------------------------------------- commands


@app.command()
def inspect(
    epub: Path = typer.Argument(..., exists=True, dir_okay=False, help="EPUB to inspect"),
    show_units: int = typer.Option(0, "--show-units", help="Print the first N text units"),
) -> None:
    """Report an EPUB's structure and content statistics."""
    archive = _load(epub)
    package = read_package(archive)

    typer.secho(f"\n{epub.name}", bold=True)
    typer.echo(f"  size          {epub.stat().st_size:,} bytes")
    typer.echo(f"  entries       {len(archive.names())}")
    typer.echo(f"  OPF           {package.opf_path}")
    typer.echo(f"  EPUB version  {package.version}")
    for key, value in package.metadata.items():
        typer.echo(f"  {key:<13} {value}")

    typer.secho("\nspine", bold=True)
    total_units = 0
    total_words = 0
    for href in package.spine_hrefs:
        if href not in archive:
            typer.secho(f"  {href}  (MISSING)", fg=typer.colors.RED)
            continue
        try:
            units = iter_text_units(archive.get(href), href)
        except EpubError as exc:
            _fail(EpubError(f"{href}: {exc}"))
        words = sum(len(u.text.split()) for u in units)
        total_units += len(units)
        total_words += words
        typer.echo(f"  {href:<34} {len(units):>5} units  {words:>7,} words")

    typer.secho("\ntotals", bold=True)
    typer.echo(f"  text units    {total_units:,}")
    typer.echo(f"  words         {total_words:,}")

    if show_units:
        typer.secho(f"\nfirst {show_units} text units", bold=True)
        shown = 0
        for href in package.spine_hrefs:
            if href not in archive:
                continue
            for unit in iter_text_units(archive.get(href), href):
                if shown >= show_units:
                    break
                preview = unit.text.strip().replace("\n", " ")[:100]
                typer.echo(f"  {unit.address:<44} {preview}")
                shown += 1
            if shown >= show_units:
                break
    typer.echo()


@app.command()
def render(
    epub: Path = typer.Argument(..., exists=True, dir_okay=False, help="Source EPUB"),
    annotations: Path | None = typer.Argument(
        None, exists=True, dir_okay=False, help="annotations.json sidecar (omit for passthrough)"
    ),
    out_dir: Path = typer.Option(Path("out"), "-o", "--out", help="Output directory"),
    kepub: bool = typer.Option(True, "--kepub/--no-kepub", help="Also emit .kepub.epub for Kobo"),
    style: str = typer.Option(
        "footnote", "--style", help="footnote (EPUB 3 popups) | inline (bracketed text)"
    ),
) -> None:
    """Render an annotated EPUB and a KEPUB for the Kobo.

    With no sidecar this is a pure passthrough — the lossless round-trip that
    every other guarantee is built on.
    """
    _load(epub)  # fail fast on DRM / corruption before doing any work
    sidecar = Sidecar.load(annotations) if annotations else None
    if sidecar is not None and annotations is not None:
        _warn_if_different_file(sidecar, annotations, epub)

    try:
        result = render_epub(epub, sidecar, out_dir, renderer=style, make_kepub=kepub)
    except (RenderError, KepubError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if sidecar:
        counts = sidecar.counts()
        typer.echo(
            f"sidecar: {len(sidecar.annotations)} annotations "
            f"({', '.join(f'{v} {k}' for k, v in counts.items() if v)})"
        )
    if result.normalized_kobo:
        typer.echo(
            f"source was already Kobo-converted: stripped koboSpans from "
            f"{len(result.normalized_kobo)} document(s) before annotating"
        )
    if result.upgraded_to_epub3:
        typer.echo("upgraded package to EPUB 3.0 (added nav document)")
    if result.stylesheet:
        typer.echo(f"footnote CSS appended to {result.stylesheet}")
    if result.skipped:
        _warn(f"sidecar references documents not in this book: {result.skipped}")
    if result.annotations_applied:
        typer.echo(
            f"applied {result.annotations_applied} annotation(s) "
            f"across {result.documents_touched} document(s) [{style}]"
        )

    size = result.epub_path.stat().st_size
    typer.secho(f"wrote {result.epub_path}  ({size:,} bytes)", fg=typer.colors.GREEN)

    if kepub and result.kepub_path:
        typer.secho(
            f"wrote {result.kepub_path}  ({result.kepub_path.stat().st_size:,} bytes)"
            "  <- copy this to the Kobo",
            fg=typer.colors.GREEN,
        )
    elif kepub:
        _warn(f"kepubify not on PATH — skipped KEPUB ({INSTALL_HINT})")


@app.command()
def verify(
    epub: Path = typer.Argument(..., exists=True, dir_okay=False, help="EPUB to verify"),
    original: Path | None = typer.Option(
        None, "--original", exists=True, dir_okay=False, help="Compare against the source EPUB"
    ),
    kepub: Path | None = typer.Option(
        None,
        "--kepub",
        exists=True,
        dir_okay=False,
        help="Also check the KEPUB sent to the device",
    ),
) -> None:
    """Run the integrity gate against a produced EPUB (and optionally its KEPUB)."""
    if kepub is None:
        # the KEPUB is what reaches the device — check it by default when present
        guess = epub.with_name(epub.name.replace(".epub", ".kepub.epub"))
        if guess != epub and guess.is_file():
            kepub = guess
    findings = run_verify(epub, original, kepub)
    typer.echo()
    for finding in findings:
        colour = typer.colors.GREEN if finding.ok else typer.colors.RED
        typer.secho(str(finding), fg=colour)
    failed = [f for f in findings if not f.ok]
    typer.echo()
    if failed:
        typer.secho(
            f"{len(failed)} of {len(findings)} checks FAILED", fg=typer.colors.RED, bold=True
        )
        raise typer.Exit(1)
    typer.secho(f"all {len(findings)} checks passed", fg=typer.colors.GREEN, bold=True)


@app.command()
def detect(
    epub: Path = typer.Argument(..., exists=True, dir_okay=False, help="Source EPUB"),
    out: Path = typer.Option(Path("annotations.json"), "-o", "--out", help="Sidecar to write"),
    trace: Path | None = typer.Option(
        None, "--trace", help="Write a JSONL decision trace (one record per segment)"
    ),
    backend: str | None = typer.Option(
        None, "--backend", help="ollama | openai | stub | none (tier 1 only) [config: ollama]"
    ),
    model: str | None = typer.Option(None, "--model", help="Model tag [config: qwen3:8b]"),
    ollama_url: str | None = typer.Option(
        None, "--ollama-url", help="Where Ollama is listening [config: http://localhost:11434]"
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="OpenAI-compatible server, ending in /v1 [config: http://localhost:8080/v1]",
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="Bearer token for a server that wants one (prefer JOVEN_API_KEY)"
    ),
    workers: int | None = typer.Option(
        None, "--workers", min=1, help="Paragraphs in flight at once [config: 1]"
    ),
    limit: int | None = typer.Option(None, "--limit", help="Only scan the first N paragraphs"),
    href: list[str] | None = typer.Option(
        None, "--href", help="Only scan this spine document (repeatable)"
    ),
    paragraph_range: str | None = typer.Option(
        None, "--range", help="Only scan paragraphs START:STOP (either side optional)"
    ),
    merge: bool = typer.Option(
        True, "--merge/--overwrite", help="Merge into an existing sidecar, keeping human edits"
    ),
    resume: Path | None = typer.Option(
        None,
        "--resume",
        exists=True,
        dir_okay=False,
        help="Reuse model answers from an earlier trace instead of re-asking",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No progress bar"),
) -> None:
    """Detect foreign-language passages and write an annotations sidecar.

    Tier 1 (statistical) runs on every sentence; only the abstention band reaches
    the LLM. Use --trace to see exactly what happened to every segment.

    A run that was interrupted can be picked up with --resume pointed at its
    trace: every model answer already recorded is reused, and only the segments
    it never reached cost anything.

    Every option marked [config: …] falls back to joven.toml or a JOVEN_* variable;
    `joven config` shows the effective values.
    """
    _load(epub)
    settings = _settings().settings
    backend = backend or settings.backend
    model = model or settings.model
    ollama_url = ollama_url or settings.ollama_url
    base_url = base_url or settings.base_url
    api_key = api_key if api_key is not None else settings.api_key
    workers = workers or settings.workers

    translator = None
    if backend not in {"none", ""}:
        if backend not in BACKENDS:
            _error(f"unknown backend {backend!r} — choose from: {', '.join(BACKENDS)}, none", 2)
        if backend == "ollama":
            if not ollama_available(ollama_url):
                _error(
                    f"ollama is not running at {ollama_url} — start it with: ollama serve\n"
                    "       (or point --ollama-url / JOVEN_OLLAMA_URL at a server that is)",
                    2,
                )
            available = installed_models(ollama_url)
            if model not in available:
                _error(
                    f"model {model!r} not installed at {ollama_url}. "
                    f"Available: {available or 'none'}\n"
                    f"       install with: ollama pull {model}",
                    2,
                )
        elif backend == "openai":
            if not openai_available(base_url, api_key):
                _error(
                    f"nothing answered GET {base_url}/models — is the server running, "
                    "and does --base-url end in /v1?",
                    2,
                )
            listed = openai_models(base_url, api_key)
            if listed and model not in listed:
                _warn(f"{base_url} does not list {model!r}; it lists {listed}")
        translator = get_translator(
            backend,
            model,
            base_url=ollama_url if backend == "ollama" else base_url,
            api_key=api_key,
        )

    if translator is None:
        typer.secho(
            "tier 1 only — the abstention band will NOT be adjudicated "
            "(pass --backend ollama for full detection)",
            fg=typer.colors.YELLOW,
        )

    # Read the resume trace *before* the tracer opens: --trace truncates, and
    # pointing both at one file is the obvious way to run this.
    recorded = {}
    if resume is not None:
        recorded = reusable_answers(load_trace(resume))
        if not recorded:
            _warn(f"{resume} holds no reusable model answers — running as a fresh detection")
        else:
            typer.echo(f"resuming: {len(recorded):,} model answers recalled from {resume}")
            recorded_by = {d.tier2_model for d in recorded.values()} - {""}
            if translator is not None and recorded_by and model not in recorded_by:
                _warn(
                    f"the trace was recorded by {sorted(recorded_by)} and this run uses "
                    f"{model!r}; recorded answers are reused as they stand"
                )
        if trace is None:
            _warn(
                "--resume without --trace, so this run records nothing "
                "and an interruption would lose it again"
            )

    hrefs = set(href) if href else None
    if hrefs:
        spine = set(read_package(_load(epub)).spine_hrefs)
        for missing in sorted(hrefs - spine):
            _warn(f"--href {missing!r} is not in the spine")
    span = _parse_range(paragraph_range) if paragraph_range else None

    triager = Triager(
        accept_spanish=settings.accept_spanish,
        reject_english=settings.reject_english,
        accept_spanish_stripped=settings.accept_spanish_stripped,
    )

    console = Console(stderr=True)
    bar = RichProgress(
        TextColumn("detect"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[status]}"),
        TimeRemainingColumn(),
        console=console,
        disable=quiet or not console.is_terminal,
        transient=True,
    )
    task = bar.add_task("detect", total=None, status="")

    def on_progress(p: Progress) -> None:
        status = f"{p.escalated:,} escalated · {p.annotated:,} footnotes"
        if p.errors:
            status += f" · {p.errors} errors"
        if p.last_latency_s:
            status += f" · {p.last_latency_s:.1f}s/call"
        bar.update(task, total=p.paragraphs_total, completed=p.paragraphs_done, status=status)

    with bar, Tracer(path=trace) as tracer:
        try:
            sidecar, result = run_detect(
                epub,
                triager=triager,
                translator=translator,
                tracer=tracer,
                limit=limit,
                resume=recorded,
                hrefs=hrefs,
                paragraph_range=span,
                workers=workers,
                context_chars=settings.context_chars,
                similarity_veto=settings.similarity_veto,
                on_progress=on_progress,
            )
        except EpubError as exc:
            _fail(exc)
        report = tracer.format_report()
        band = tracer.band_samples(8)
        rejections = tracer.llm_rejections(8)
        errors = tracer.errors()

    typer.echo(report)
    typer.echo(f"\n  paragraphs scanned    {result.paragraphs_scanned:,}")
    typer.echo(f"  annotations           {len(result.annotations):,}")
    if result.llm_recalled:
        typer.echo(f"  recalled from trace   {result.llm_recalled:,}  (no model call)")

    if band:
        typer.secho("\n  least-confident escalations (tune thresholds here):", bold=True)
        for d in band:
            mark = "ES" if d.tier2_is_spanish else "en"
            typer.echo(f"    {d.tier1_confidence:.2f} [{mark}] {d.text[:64]}")
    if rejections:
        typer.secho("\n  LLM said not Spanish:", bold=True)
        for d in rejections:
            typer.echo(f"    {d.tier1_confidence:.2f}  {d.text[:64]}")
    if errors:
        typer.secho(f"\n  {len(errors)} LLM ERROR(S):", fg=typer.colors.RED, bold=True)
        for d in errors[:5]:
            typer.echo(f"    {d.text[:44]}  {d.tier2_error[:70]}")

    if merge and out.is_file():
        existing = Sidecar.load(out)
        stats = existing.merge(result.annotations)
        existing.source_sha256 = sidecar.source_sha256
        existing.title = sidecar.title
        existing.save(out)
        typer.secho(
            f"\nmerged into {out}: {stats['added']} added, {stats['updated']} updated, "
            f"{stats['kept_human']} human edits kept, {stats['suppressed']} suppressed",
            fg=typer.colors.GREEN,
        )
    else:
        sidecar.save(out)
        typer.secho(
            f"\nwrote {out} ({len(sidecar.annotations)} annotations)",
            fg=typer.colors.GREEN,
        )

    if trace:
        typer.secho(f"wrote {trace}", fg=typer.colors.GREEN)


@app.command("explain")
def explain(
    trace: Path = typer.Argument(..., exists=True, dir_okay=False, help="A JSONL trace"),
    find: str | None = typer.Option(None, "--find", help="Only segments containing this text"),
    outcome: str | None = typer.Option(None, "--outcome", help="Filter by outcome"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Explain why passages were or weren't annotated, from a trace file.

    Answers "why is this line not annotated?" directly:

        joven explain trace.jsonl --find "Escuchame"
    """
    decisions = load_trace(trace)
    if find:
        needle = find.casefold()
        decisions = [d for d in decisions if needle in d.text.casefold()]
    if outcome:
        decisions = [d for d in decisions if d.outcome == outcome]

    if not decisions:
        typer.secho("no matching segments in the trace", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    typer.echo(f"\n{len(decisions)} matching segment(s), showing up to {limit}:\n")
    for d in decisions[:limit]:
        colour = typer.colors.GREEN if d.outcome == Outcome.ANNOTATED else typer.colors.YELLOW
        typer.secho(f"  {d.href}#{d.para_index}.{d.segment_index}  [{d.outcome}]", fg=colour)
        typer.echo(f"    text        {d.text[:96]!r}")
        typer.echo(
            f"    tier 1      {d.tier1_language} {d.tier1_confidence:.4f} "
            f"-> {d.tier1_verdict} ({d.tier1_reason})"
        )
        if d.tier1_stripped:
            typer.echo(f"    stripped    {d.tier1_stripped!r}")
        if d.tier2_used:
            typer.echo(
                f"    tier 2      {d.tier2_model} {d.tier2_latency_s:.1f}s "
                f"is_spanish={d.tier2_is_spanish}"
            )
            if d.tier2_spanish_text:
                typer.echo(f"    span        {d.tier2_spanish_text[:80]!r}")
            if d.tier2_translation:
                typer.echo(f"    translation {d.tier2_translation[:80]!r}")
            if d.tier2_error:
                typer.secho(f"    ERROR       {d.tier2_error}", fg=typer.colors.RED)
        typer.echo()


@app.command()
def review(
    annotations: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="annotations.json sidecar"
    ),
    epub: Path | None = typer.Option(
        None, "--epub", exists=True, dir_okay=False, help="Source EPUB, for context"
    ),
    port: int = typer.Option(8765, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Review annotations in a local web UI, worst-confidence first.

    Decisions save to the sidecar immediately and survive re-detection, so a
    review is never wasted work. Pass --epub to see the surrounding prose, which
    is what makes short fragments judgeable.
    """
    if epub is not None:
        _warn_if_different_file(Sidecar.load(annotations), annotations, epub)
    serve_review(annotations, epub, port=port, open_browser=open_browser)


@app.command()
def add(
    annotations: Path = typer.Argument(..., dir_okay=False, help="Sidecar to add to"),
    epub: Path = typer.Option(..., "--epub", exists=True, dir_okay=False, help="Source EPUB"),
    find: str = typer.Option(..., "--find", help="Text of the paragraph to annotate"),
    translation: str = typer.Option(..., "--translation", help="English translation"),
    spanish: str | None = typer.Option(
        None, "--spanish", help="Substring to mark (default: the whole paragraph)"
    ),
) -> None:
    """Manually annotate a passage detection missed.

    The escape hatch for the other direction of error: detection can be tuned to
    find less, but nothing recovers a passage it never proposed. Added entries are
    marked 'edited', so re-detection will not overwrite them.
    """
    archive = _load(epub)
    package = read_package(archive)

    matches: list[tuple[str, int, str, int]] = []
    for href in package.spine_hrefs:
        if href not in archive:
            continue
        try:
            units = iter_text_units(archive.get(href), href)
        except EpubError as exc:
            _fail(EpubError(f"{href}: {exc}"))
        for unit, occurrence in zip(
            units, occurrence_indices(u.text for u in units), strict=True
        ):
            if normalize(find) in normalize(unit.text):
                matches.append((href, unit.index, unit.text, occurrence))

    if not matches:
        typer.secho(f"error: no paragraph contains {find!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.secho(f"{len(matches)} paragraphs match — be more specific:", fg=typer.colors.YELLOW)
        for href, index, text, _ in matches[:8]:
            typer.echo(f"  {href}#{index}  {text[:88]!r}")
        raise typer.Exit(1)

    href, para_index, text, occurrence = matches[0]
    target = spanish or text.strip()
    start = text.find(target)
    if start < 0:
        typer.secho(
            f"error: {target!r} is not in that paragraph: {text[:80]!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    sidecar = Sidecar.load(annotations) if annotations.is_file() else Sidecar()
    entry = Annotation.create(
        href=href,
        para_index=para_index,
        source_text=text,
        spans=[(start, start + len(target))],
        translation=translation,
        occurrence=occurrence,
        detector_confidence=1.0,
        model="manual",
    )
    entry.status = Status.EDITED
    existing = {a.id for a in sidecar.annotations}
    if entry.id in existing:
        sidecar.annotations = [a for a in sidecar.annotations if a.id != entry.id]
        typer.echo("replacing the existing annotation for that paragraph")
    sidecar.annotations.append(entry)
    sidecar.annotations.sort(key=lambda a: (a.href, a.para_index))
    sidecar.save(annotations)

    typer.secho(f"added {href}#{para_index}", fg=typer.colors.GREEN)
    typer.echo(f"  marked: {target[:70]!r}")
    typer.echo(f"  ->      {translation[:70]!r}")
    typer.echo(f"  sidecar now has {len(sidecar.annotations)} annotation(s)")


@app.command()
def reject(
    annotations: Path = typer.Argument(..., exists=True, dir_okay=False, help="Sidecar to edit"),
    find: str | None = typer.Option(None, "--find", help="Text of the annotated paragraph"),
    ident: str | None = typer.Option(None, "--id", help="The annotation's id"),
) -> None:
    """Mark an annotation rejected — not Spanish, never render it — without the review page.

    The counterpart of `add`. A rejected id is never re-added by re-detection,
    however confident the detector is.
    """
    sidecar = Sidecar.load(annotations)
    target = _locate(sidecar, find, ident)
    before = target.status
    target.status = Status.REJECTED
    sidecar.save(annotations)
    typer.secho(f"rejected {target.href}#{target.para_index}  ({before} -> rejected)", fg="green")
    typer.echo(f"  {target.spanish_text[:70]!r}")


@app.command()
def reset(
    annotations: Path = typer.Argument(..., exists=True, dir_okay=False, help="Sidecar to edit"),
    find: str | None = typer.Option(None, "--find", help="Text of the annotated paragraph"),
    ident: str | None = typer.Option(None, "--id", help="The annotation's id"),
) -> None:
    """Return an annotation to `auto`, undoing a review decision.

    The next re-detection may then overwrite it. An edited translation keeps its
    current wording until that happens — there is no earlier wording to go back to.
    """
    sidecar = Sidecar.load(annotations)
    target = _locate(sidecar, find, ident)
    before = target.status
    if before is Status.AUTO:
        typer.echo(f"{target.href}#{target.para_index} is already auto")
        return
    target.status = Status.AUTO
    sidecar.save(annotations)
    typer.secho(f"reset {target.href}#{target.para_index}  ({before} -> auto)", fg="green")
    if before is Status.EDITED:
        typer.echo("  the edited wording stays until the next detect overwrites it")


@app.command()
def status(
    annotations: Path = typer.Argument(..., exists=True, dir_okay=False, help="Sidecar to read"),
    epub: Path | None = typer.Option(
        None, "--epub", exists=True, dir_okay=False, help="Check the sidecar is for this book"
    ),
) -> None:
    """Where a sidecar stands: counts by status, what is flagged, which model, which book."""
    sidecar = Sidecar.load(annotations)
    counts = sidecar.counts()
    flagged = sum(
        1
        for a in sidecar.annotations
        if not a.status.is_human and suspicions(a.spanish_text, a.translation)
    )
    models = sorted({m.strip() for a in sidecar.annotations for m in a.model.split(",") if m})
    reviewed = sum(v for k, v in counts.items() if k != "auto")

    typer.secho(f"\n{annotations.name}", bold=True)
    typer.echo(f"  title         {sidecar.title or '(none recorded)'}")
    typer.echo(f"  annotations   {len(sidecar.annotations):,}")
    for name, value in counts.items():
        typer.echo(f"    {name:<11} {value:,}")
    typer.echo(f"  reviewed      {reviewed:,} of {len(sidecar.annotations):,}")
    typer.echo(f"  flagged       {flagged:,}  (auto annotations the review would show first)")
    typer.echo(f"  renderable    {len(sidecar.renderable()):,}")
    typer.echo(f"  models        {', '.join(models) or '(none)'}")
    digest = sidecar.source_sha256
    typer.echo(f"  source sha256 {digest[:12] + '…' if digest else '(none recorded)'}")
    if epub is not None:
        match = sidecar.source_matches(epub)
        if match is True:
            typer.secho(f"  book          {epub.name}: matches", fg=typer.colors.GREEN)
        elif match is False:
            typer.secho(f"  book          {epub.name}: DIFFERENT FILE", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"  book          {epub.name}: no hash recorded to compare")
    typer.echo()


@app.command()
def diff(
    old: Path = typer.Argument(..., exists=True, dir_okay=False, help="Earlier sidecar"),
    new: Path = typer.Argument(..., exists=True, dir_okay=False, help="Later sidecar"),
    limit: int = typer.Option(20, "--limit", help="Examples to show per kind of change"),
) -> None:
    """What changed between two sidecars, by annotation id.

    The question after re-detecting with a different model or threshold: which
    footnotes appeared, which vanished, and which read differently.
    """
    before = {a.id: a for a in Sidecar.load(old).annotations}
    after = {a.id: a for a in Sidecar.load(new).annotations}
    added = [after[i] for i in after.keys() - before.keys()]
    removed = [before[i] for i in before.keys() - after.keys()]
    reworded = [
        (before[i], after[i])
        for i in before.keys() & after.keys()
        if before[i].translation != after[i].translation
    ]
    restated = [
        (before[i], after[i])
        for i in before.keys() & after.keys()
        if before[i].status != after[i].status
    ]
    key = lambda a: (a.href, a.para_index)  # noqa: E731 - three sorts, one rule

    typer.echo(
        f"\n{old.name} -> {new.name}: {len(added)} added, {len(removed)} removed, "
        f"{len(reworded)} reworded, {len(restated)} changed status"
    )
    if added:
        typer.secho(f"\n  added ({len(added)})", bold=True)
        for a in sorted(added, key=key)[:limit]:
            typer.echo(f"    + {a.href}#{a.para_index}  {a.spanish_text[:48]!r}")
            typer.echo(f"        {a.translation[:70]!r}")
    if removed:
        typer.secho(f"\n  removed ({len(removed)})", bold=True)
        for a in sorted(removed, key=key)[:limit]:
            typer.echo(f"    - {a.href}#{a.para_index}  {a.spanish_text[:48]!r}")
    if reworded:
        typer.secho(f"\n  reworded ({len(reworded)})", bold=True)
        for was, now in sorted(reworded, key=lambda p: key(p[1]))[:limit]:
            typer.echo(f"    ~ {now.href}#{now.para_index}  {now.spanish_text[:48]!r}")
            typer.echo(f"        was  {was.translation[:66]!r}")
            typer.echo(f"        now  {now.translation[:66]!r}")
    if restated:
        typer.secho(f"\n  changed status ({len(restated)})", bold=True)
        for was, now in sorted(restated, key=lambda p: key(p[1]))[:limit]:
            typer.echo(
                f"    ~ {now.href}#{now.para_index}  {was.status} -> {now.status}  "
                f"{now.spanish_text[:40]!r}"
            )
    typer.echo()


@app.command()
def config() -> None:
    """Show the effective configuration and where each value comes from."""
    resolved = _settings()
    typer.secho("\nfiles consulted", bold=True)
    for path in resolved.files:
        state = "read" if path.is_file() else "absent"
        typer.echo(f"  {path}  ({state})")
    typer.secho("\nsettings", bold=True)
    for name, source in resolved.sources.items():
        value = getattr(resolved.settings, name)
        shown = "••••" if name == "api_key" and value else repr(value)
        typer.echo(f"  {name:<24} {shown:<36} {source}")
    typer.echo("\n  precedence: flag > JOVEN_<NAME> > ./joven.toml > user config > default")
    typer.echo()


def main() -> None:
    """Console-script entry point.

    The streams are pinned to UTF-8 before Typer can print a word of Spanish
    through a cp1252 one; see :func:`joven.console.force_utf8_output`.
    """
    force_utf8_output()
    app()


if __name__ == "__main__":
    main()
