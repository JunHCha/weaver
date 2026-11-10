"""Runtime adapters for the durable v2 summary workflow."""

import threading
from pathlib import Path

from weaver.archive import ArchiveStore
from weaver.courier.atomic import write_json_atomic
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.schema import Source
from weaver.schema.v2 import EventSummary, ReviewPackage, Verdict
from weaver.spinner import Spinner
from weaver.wiki.compiler import Summary

from .gate import Interrupted, LlmGate
from .loop import Outcome


class SummaryLookup:
    """Resolve the latest archived summary and its source-copy title."""

    def __init__(self, archive: Path, sources: ArchiveStore) -> None:
        self.archive = archive
        self.sources = sources

    def latest(self, source_id: str) -> EventSummary | None:
        items = []
        for path in self.archive.glob("*/*.json"):
            summary = EventSummary.model_validate_json(path.read_bytes())
            if summary.source_id == source_id:
                items.append(summary)
        if not items:
            return None
        replaced = {item.replaces for item in items}
        heads = [item for item in items if item.summary_id not in replaced]
        return max(heads, key=lambda item: (item.created_at, item.summary_id))

    def title(self, source_id: str) -> str:
        source = self.sources.get(source_id)
        date = (source.occurred_at or source.captured_at).date().isoformat()
        counterpart = source.hints.counterpart or "알 수 없음"
        return f"{date} {counterpart} {source.channel}"

    def __call__(self, source_id: str) -> Summary:
        summary = self.latest(source_id)
        if summary is None:
            raise ValueError("summary not found")
        return Summary(title=self.title(source_id), body=summary.body)


class ReviewAdapter:
    """Expose the summary journal through Courier's v2 ports."""

    def __init__(self, review: SummaryReview, lookup: SummaryLookup) -> None:
        self.review = review
        self.lookup = lookup

    def latest_rev(self, package_id: str) -> int | None:
        with self.review.state.connect() as db:
            row = db.execute(
                "SELECT MAX(revision) FROM packages WHERE id=?", (package_id,)
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def is_applied(self, package_id: str, rev: int) -> bool:
        with self.review.state.connect() as db:
            row = db.execute(
                "SELECT stage FROM reviews WHERE id=? AND revision=?", (package_id, rev)
            ).fetchone()
        return row is not None and row[0] == "APPLIED"

    def validate_verdict(self, verdict: Verdict) -> None:
        with self.review.state.connect() as db:
            row = db.execute(
                "SELECT verdict FROM reviews WHERE id=? AND revision=?",
                (verdict.package_id, verdict.revision),
            ).fetchone()
        if row is not None and Verdict.model_validate_json(row[0]) != verdict:
            raise ValueError("conflicting verdict")

    def apply(self, verdict: Verdict) -> None:
        package, script = self.review.state.load_package(
            verdict.package_id, verdict.revision
        )
        previous = self.lookup.latest(package.source_id)
        self.review.apply(
            package, script, verdict, replaces=previous.summary_id if previous else None
        )


class SummaryProcessor:
    """Produce a single durable review package for each archived source."""

    def __init__(self, spinner: Spinner, drafts: SummaryDrafts, gate: LlmGate) -> None:
        self.spinner, self.drafts, self.gate = spinner, drafts, gate

    def process(self, source: Source, stop: threading.Event) -> Outcome:
        with self.drafts.state.connect() as db:
            rows = db.execute(
                "SELECT package FROM packages WHERE revision=1"
            ).fetchall()
        for (raw,) in rows:
            package = ReviewPackage.model_validate_json(raw)
            if package.source_id == source.source_id:
                with self.drafts.state.connect() as db:
                    applied = db.execute(
                        "SELECT 1 FROM reviews WHERE id=? AND revision=1 AND stage='APPLIED'",
                        (package.package_id,),
                    ).fetchone()
                if not applied:
                    self.drafts.proposals.mkdir(parents=True, exist_ok=True)
                    write_json_atomic(
                        self.drafts.proposals / f"{package.package_id}.r1.json", package
                    )
                return "done"
        if not self.gate.available:
            return "waiting_llm"
        if stop.is_set():
            raise Interrupted()
        script = self.spinner.spin(source)
        if stop.is_set():
            raise Interrupted()
        self.drafts.create(source.source_id, script.text, source.hints.counterpart)
        return "done"
