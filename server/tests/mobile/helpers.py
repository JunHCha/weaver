"""Synthetic harness for the phone API: fake whois, private stores, no network.

Nothing here touches a real LLM, a vault, ~/Weaver-device-test or a tailnet.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from weaver.action import ActionOutbox
from weaver.action.courier import ActionCourier
from weaver.archive import ArchiveStore
from weaver.config import CourierSettings, ServerConfig, load_config
from weaver.courier import Courier
from weaver.fabric import FabricStore
from weaver.loom.summary_state import SummaryState
from weaver.mobile.api import create_app
from weaver.mobile.applier import MobileApplier
from weaver.mobile.reviews import ReviewService
from weaver.mobile.store import MobileStore
from weaver.mobile.whois import PeerIdentity, WhoisUnavailable
from weaver.schema import Manifest, ReviewPackage, ThreadDraft, Verdict

ROOT = Path(__file__).resolve().parents[3]
SID = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
CAP = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"
CAP2 = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA1"
BODY = "합성 공유 텍스트: 내일 점검 회의".encode()
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
NODE = "nPHONE0001"
LOGIN = "owner@example.com"


class FakeWhois:
    """Names the peer as one fixed node, or nobody (``identity = None``)."""

    def __init__(self) -> None:
        self.identity: PeerIdentity | None = PeerIdentity(NODE, LOGIN)
        self.unavailable = False  # tailscaled could not answer
        self.calls: list[tuple[str, int]] = []

    def lookup(self, host: str, port: int) -> PeerIdentity | None:
        self.calls.append((host, port))
        if self.unavailable:
            raise WhoisUnavailable
        return self.identity


class Decisions:
    """The decision journal behind Courier, backed by the real review table."""

    def __init__(self, summary: SummaryState) -> None:
        self.summary = summary
        self.applied: list[Verdict] = []
        self.fail: Exception | None = None

    def is_applied(self, package_id: str, rev: int) -> bool:
        with self.summary.connect() as db:
            row = db.execute(
                "SELECT stage FROM reviews WHERE id=? AND revision=?",
                (package_id, rev),
            ).fetchone()
        return row is not None and row[0] == "APPLIED"

    def apply(self, verdict: Verdict) -> None:
        if self.fail is not None:
            raise self.fail
        self.applied.append(verdict)
        with self.summary.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",
                (
                    verdict.package_id,
                    verdict.revision,
                    verdict.model_dump_json(),
                    "{}",
                    "APPLIED",
                ),
            )


def write_config(root: Path, mobile: str = "") -> ServerConfig:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.toml"
    path.write_text(
        "\n".join(
            f'{name}_root = "{name}"'
            for name in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[mobile]\nbind = "127.0.0.1"\nport = 8787\n'
        + mobile
    )
    return load_config(path, init_directories=True)


class Harness:
    def __init__(self, tmp_path: Path, mobile: str = "", clock: Any = None) -> None:
        self.config = write_config(tmp_path, mobile)
        self.whois = FakeWhois()
        self.store = MobileStore(self.config.state_root)
        self.archive = ArchiveStore.from_config(self.config)
        self.summary = SummaryState(self.config.state_root)
        self.fabric = FabricStore.from_config(self.config)
        self.outbox = ActionOutbox(self.config.state_root / "action-outbox.sqlite3")
        self.actions = ActionCourier(
            self.config.sync_root,
            self.outbox,
            self.fabric,
            publish_files=lambda: self.config.mobile.transport_for("action") == "files",
        )
        self.reviews = ReviewService(self.summary, self.store)
        self.decisions = Decisions(self.summary)
        self.clock = [1000.0]
        self.courier = Courier(
            self.config.courier,
            self.archive,
            self.reviews,
            self.decisions,
            retry=CourierSettings(
                max_attempts=3, backoff_seconds=1, max_backoff_seconds=4
            ),
            clock=lambda: self.clock[0],
        )
        self.applier = MobileApplier(self.store, self.courier, self.actions)
        self.app = create_app(
            self.config,
            self.whois,
            store=self.store,
            archive=self.archive,
            summary=self.summary,
            actions=self.actions,
            clock=clock,
        )
        self.client = TestClient(
            self.app, base_url="https://127.0.0.1:8787", client=("100.108.10.0", 40000)
        )

    def pair(
        self,
        *scopes: str,
        node: str = NODE,
        executor: bool = False,
        label: str = "fold",
    ) -> dict[str, str]:
        device, token = self.store.create_device(
            label,
            node,
            frozenset(scopes or ("read", "capture", "review", "edit")),
            login_name=LOGIN,
            executor=executor,
        )
        self.device = device
        return {"Authorization": f"Bearer {token}"}

    def add_package(
        self, package_id: str = "pkg_a", revision: int = 1
    ) -> ReviewPackage:
        package = ReviewPackage.model_validate(
            {
                "schema_version": 2,
                "package_id": package_id,
                "source_id": SID,
                "revision": revision,
                "title": "합성 검토",
                "source_text": "합성 본문",
                "source_type": "share_text",
                "draft_body": "초안",
                "questions": [],
                "stage": "검토",
                "stage_times": {"검토": NOW.isoformat()},
            }
        )
        self.summary.save_package(package, "script")
        return package

    def tick(self, seconds: float = 0) -> None:
        self.clock[0] += seconds
        self.applier.run()


def verdict(
    package_id: str = "pkg_a", revision: int = 1, **changes: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 2,
        "package_id": package_id,
        "revision": revision,
        "action": "approve",
        "answers": [],
        "edited_body": None,
        "feedback": None,
    }
    data.update(changes)
    return data


def manifest(capture_id: str = CAP, body: bytes = BODY, **changes: Any) -> Manifest:
    raw = json.loads((ROOT / "schema" / "examples" / "Manifest.json").read_text())
    raw |= {
        "capture_id": capture_id,
        "payload": f"{capture_id}.txt",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }
    raw.update(changes)
    return Manifest.model_validate(raw)


def thread(key: str, **changes: Any) -> ThreadDraft:
    data: dict[str, Any] = dict(
        thread_id=key,
        kind="기록",
        content=key,
        occurred_at=NOW,
        recorded_at=NOW,
        tags=[],
        field=None,
        value=None,
        source_id=SID,
        actor="human",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(changes)
    return ThreadDraft.model_validate(data)


def upload(
    h: Harness, headers: dict[str, str], capture_id: str = CAP, body: bytes = BODY
) -> Any:
    return h.client.put(
        f"/mobile/v1/captures/{capture_id}/payload",
        content=body,
        headers={
            **headers,
            "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
            "Content-Type": "application/octet-stream",
        },
    )


def put_manifest(h: Harness, headers: dict[str, str], m: Manifest | None = None) -> Any:
    m = m or manifest()
    return h.client.put(
        f"/mobile/v1/captures/{m.capture_id}/manifest",
        content=m.model_dump_json(),
        headers=headers,
    )


def schedule(
    key: str, *, tags: tuple[str, ...] = ("홍길동",), **changes: Any
) -> ThreadDraft:
    data: dict[str, Any] = dict(
        kind="일정",
        content="주간 회의",
        tags=list(tags),
        field="schedule",
        value={
            "start": "2026-12-01T10:00:00+09:00",
            "end": None,
            "timezone": "Asia/Seoul",
        },
        actor="ai_approved",
    )
    data.update(changes)
    return thread(key, **data)


def result(
    action_id: str, outcome: str = "completed", **changes: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 2,
        "action_id": action_id,
        "outcome": outcome,
        "external_id": "content://calendar/events/7"
        if outcome == "completed"
        else None,
        "attempts": 1,
        "finished_at": "2026-11-07T15:45:00+09:00",
        "error": None if outcome == "completed" else "provider_error",
    }
    data.update(changes)
    return data
