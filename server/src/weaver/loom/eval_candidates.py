"""Private evaluation candidates from feedback, and human-gated promotion (#27).

Candidates live in `state_root/eval_candidates/` (directory 0700, files 0600)
and contain no raw text: only correction kinds/paths, closed-vocabulary values,
entity slots, expected structure and versions. A person writes a fake sample
from scratch (issue #10 forbids transforming real material). Status moves only
candidate -> authored -> reviewed -> promoted; promotion rejects any sample that
still contains a raw string of the original proposal or feedback record.
The sample must be a valid eval case (`weaver.evals`, schema_version 1);
promotion writes it as `feedback-<hash>.json` in the cases directory with a
derived case_id and `origin`. format_version versions only this candidate file.
"""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weaver.schema.models import TIMESTAMP_PATTERN

from .extract import normalize_name
from .feedback import _EXAMPLE_KINDS, SqliteFeedbackStore, is_raw_text

FORMAT_VERSION = 1
_NEXT = {"authored": "candidate", "reviewed": "authored", "promoted": "reviewed"}


class CandidateError(ValueError):
    """Stable, content-free failure."""


def _write(path: Path, data: dict[str, Any], mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, json.dumps(data, ensure_ascii=False, indent=2).encode() + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _structured(kind: str, value: Any, slots: dict[str, str]) -> Any:
    if kind == "entities.entity_id":
        return None if value is None else slots.setdefault(value, f"E{len(slots) + 1}")
    if kind in _EXAMPLE_KINDS:
        return value
    return {"redacted": type(value).__name__}


def export_candidates(store: SqliteFeedbackStore, root: Path) -> list[str]:
    """Write a candidate per corrected/rejected review once; return new IDs."""
    root = _root(root)
    written = []
    for pid, rev in store.events():
        record = store.event(pid, rev)
        if record is None or (
            not record["corrections"] and record["verdict"] != "reject"
        ):
            continue
        candidate_id = f"{pid}.r{rev}"
        path = root / f"{candidate_id}.json"
        if path.exists():
            continue
        proposal, entity_ids = store.journal.approval(pid, rev)
        slots: dict[str, str] = {}
        identities = [
            entity_ids.get(e.ref) or e.entity_id or e.ref for e in proposal.entities
        ]
        for identity in identities:
            slots.setdefault(identity, f"E{len(slots) + 1}")
        corrections = [
            {
                "kind": c["kind"],
                "path": c["path"],
                "before": _structured(c["kind"], c["before"], slots),
                "after": _structured(c["kind"], c["after"], slots),
            }
            for c in record["corrections"]
        ]
        expected = {
            "verdict": record["verdict"],
            "reject_reason": record["reject_reason"],
            "entities": [
                {"slot": slots[identity], "type": e.type}
                for identity, e in zip(identities, proposal.entities, strict=True)
            ],
            "entry_kinds": [d.kind for d in proposal.entry_drafts],
            "relation_types": [r.type for r in proposal.relations],
            "schedules": len(proposal.schedules),
        }
        _write(
            path,
            {
                "format_version": FORMAT_VERSION,
                "candidate_id": candidate_id,
                "status": "candidate",
                "corrections": corrections,
                "expected": expected,
                "versions": record["versions"],
                "sample": None,
                "history": [{"status": "candidate", "at": _now()}],
            },
            0o600,
        )
        written.append(candidate_id)
    return written


def load(root: Path, candidate_id: str) -> dict[str, Any]:
    path = root / f"{candidate_id}.json"
    if "/" in candidate_id or not path.is_file():
        raise CandidateError("unknown candidate")
    data: dict[str, Any] = json.loads(path.read_text())
    if data.get("format_version") != FORMAT_VERSION:
        raise CandidateError("unsupported candidate format")
    return data


def _transition(root: Path, candidate_id: str, status: str, **extra: Any) -> None:
    data = load(root, candidate_id)
    if data["status"] != _NEXT[status]:
        raise CandidateError(f"candidate must be {_NEXT[status]}")
    data.update(extra, status=status)
    data["history"].append({"status": status, "at": _now()})
    _write(root / f"{candidate_id}.json", data, 0o600)


def _sample(path: Path) -> dict[str, Any]:
    try:
        sample = json.loads(path.read_text())
    except (OSError, ValueError):
        raise CandidateError("sample must be a readable JSON file") from None
    if not isinstance(sample, dict):
        raise CandidateError("sample must be a JSON object")
    return sample


def _valid_case(sample: dict[str, Any]) -> None:
    # Lazy import: weaver.evals imports Loom parsers, which import this package.
    from weaver.evals import DatasetError, validate_case

    try:
        validate_case(sample)
    except DatasetError:
        # The reason may quote sample text; keep the failure content-free.
        raise CandidateError("sample is not a valid eval case") from None


SHINGLE = 8  # Contiguous normalized characters (spaces removed).


def _strings(value: Any) -> list[str]:
    """Every string in a sample, keys included; JSON-encoded strings are decoded."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
        if value.lstrip()[:1] in ("{", "[", '"'):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                decoded = value
            if decoded != value:
                found.extend(_strings(decoded))
    elif isinstance(value, list):
        for item in value:
            found.extend(_strings(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend([*_strings(key), *_strings(item)])
    return found


def _compact(text: str) -> str:
    return "".join(normalize_name(text).split())


def check_residue(sample: dict[str, Any], sensitive: set[str]) -> None:
    """Reject a sample that contains, or partially copies, a raw original string.

    Strings are compared after NFKC/casefold normalization, both with collapsed
    and removed whitespace (so escapes, quotes and spacing changes do not hide a
    copy). Any shared run of SHINGLE characters with a longer original also fails.
    Whole RFC 3339 timestamps are not original text: every eval case carries
    them and they share long digit runs with any record.
    """
    texts = [normalize_name(t) for t in _strings(sample)]
    compact = [_compact(t) for t in texts]
    originals = {
        text
        for value in sensitive
        for text in _strings(value)
        if is_raw_text(text) and not re.fullmatch(TIMESTAMP_PATTERN, text)
    }
    for value in originals:
        normalized, squeezed = normalize_name(value), _compact(value)
        if len(squeezed) < 2:
            continue
        if any(normalized in t for t in texts) or any(squeezed in t for t in compact):
            raise CandidateError("sample contains original text")
        for start in range(len(squeezed) - SHINGLE + 1):
            piece = squeezed[start : start + SHINGLE]
            if any(piece in t for t in compact):
                raise CandidateError("sample contains original text")


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise CandidateError("sample must be a readable JSON file") from None


def mark_authored(
    store: SqliteFeedbackStore, root: Path, candidate_id: str, sample: Path
) -> None:
    """Attach a human-written fake sample; residue is checked early too."""
    pid, rev = _event_id(candidate_id)
    data = _sample(sample)
    check_residue(data, store.sensitive_strings(pid, rev))
    _valid_case(data)
    _transition(
        root,
        candidate_id,
        "authored",
        sample=str(sample.resolve()),
        sample_sha256=_digest(sample),
    )


def _unchanged(data: dict[str, Any]) -> Path:
    path = Path(data["sample"])
    if _digest(path) != data.get("sample_sha256"):
        raise CandidateError("sample changed after authoring")
    return path


def mark_reviewed(root: Path, candidate_id: str) -> None:
    """Review approves the exact authored bytes; any later change is refused."""
    data = load(root, candidate_id)
    if data["status"] == "authored":
        _unchanged(data)
    _transition(root, candidate_id, "reviewed")


def _event_id(candidate_id: str) -> tuple[str, int]:
    pid, _, rev = candidate_id.rpartition(".r")
    if not pid or not rev.isdigit():
        raise CandidateError("unknown candidate")
    return pid, int(rev)


def promote(
    store: SqliteFeedbackStore, root: Path, candidate_id: str, evals_dir: Path
) -> Path:
    """Publish a reviewed fake sample as an eval case without private IDs.

    The case_id becomes `feedback-<hash>` of the authored content and `origin`
    records the feedback versions; the file is `<evals_dir>/<case_id>.json`.
    """
    data = load(root, candidate_id)
    if data["status"] != "reviewed":
        raise CandidateError("candidate must be reviewed")
    pid, rev = _event_id(candidate_id)
    sensitive = store.sensitive_strings(pid, rev)
    if not sensitive:
        raise CandidateError("cannot verify sample without the original record")
    sample = _sample(_unchanged(data))
    check_residue(sample, sensitive)
    content = {k: v for k, v in sample.items() if k not in ("case_id", "origin")}
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    case = {
        **content,
        "case_id": f"feedback-{digest}",
        "origin": {"kind": "feedback", "versions": data["versions"]},
    }
    _valid_case(case)
    check_residue(case, sensitive)
    evals_dir.mkdir(parents=True, exist_ok=True)
    target = evals_dir / f"{case['case_id']}.json"
    _write(target, case, 0o644)
    _transition(root, candidate_id, "promoted", promoted=target.name)
    return target
