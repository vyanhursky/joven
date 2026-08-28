"""``joven`` command line interface."""

from __future__ import annotations

from pathlib import Path

import typer

from .detect.pipeline import detect as run_detect
from .detect.triage import Triager
from .epub.archive import EpubArchive, EpubError
from .epub.document import iter_text_units
from .epub.package import read_package
from .kepub import KepubError
from .model import Annotation, Sidecar, Status, normalize, occurrence_indices
from .render import RenderError, render_epub
from .review import serve as serve_review
from .trace import Outcome, Tracer, load_trace
from .translate import get_translator, installed_models, ollama_available
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


def _fail(exc: Exception) -> None:
    """Report an unusable book as an error, not a traceback.

    ``DocumentError`` is an ``EpubError``, so this covers both the archive being
    wrong and an individual XHTML document being unparseable — which is what a
    reader gets when a book uses a named entity we cannot resolve.
    """
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(2) from exc


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
        typer.secho(
            f"warning: sidecar references documents not in this book: {result.skipped}",
            fg=typer.colors.YELLOW,
            err=True,
        )
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
        typer.secho(
            "warning: kepubify not on PATH — skipped KEPUB (brew install kepubify)",
            fg=typer.colors.YELLOW,
            err=True,
        )


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
    backend: str = typer.Option(
        "ollama", "--backend", help="ollama | stub | none (tier 1 only)"
    ),
    model: str = typer.Option("qwen3:8b", "--model", help="Ollama model tag"),
    limit: int | None = typer.Option(None, "--limit", help="Only scan the first N paragraphs"),
    merge: bool = typer.Option(
        True, "--merge/--overwrite", help="Merge into an existing sidecar, keeping human edits"
    ),
) -> None:
    """Detect foreign-language passages and write an annotations sidecar.

    Tier 1 (statistical) runs on every sentence; only the abstention band reaches
    the LLM. Use --trace to see exactly what happened to every segment.
    """
    _load(epub)

    translator = None
    if backend not in {"none", ""}:
        if backend == "ollama" and not ollama_available():
            typer.secho(
                "error: ollama is not running — start it with: ollama serve",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        if backend == "ollama" and model not in installed_models():
            available = installed_models()
            typer.secho(
                f"error: model {model!r} not installed. Available: {available or 'none'}\n"
                f"       install with: ollama pull {model}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        translator = get_translator(backend, model)

    if translator is None:
        typer.secho(
            "tier 1 only — the abstention band will NOT be adjudicated "
            "(pass --backend ollama for full detection)",
            fg=typer.colors.YELLOW,
        )

    with Tracer(path=trace) as tracer:
        try:
            sidecar, result = run_detect(
                epub,
                triager=Triager(),
                translator=translator,
                tracer=tracer,
                limit=limit,
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


if __name__ == "__main__":
    app()
