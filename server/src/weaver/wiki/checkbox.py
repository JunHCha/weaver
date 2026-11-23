"""Checkbox completion from Obsidian and a one-time alert before overwriting edits.

A person may tick a Tasks-plugin checkbox in a Weaver file, including in a
Syncthing ``.sync-conflict-*`` copy. That records a human ``결정`` Thread with
``resolves`` once per request; the file itself is then restored like any other
read-only page. Any other direct edit raises one alert and is overwritten after
the settle window (spec 7.5).
"""

import glob
import hashlib
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from weaver.alert.detector import alert_id_for
from weaver.fabric.store import Fabric, FabricError
from weaver.schema import Alert, Thread, ThreadDraft
from weaver.wiki.layout import block_id, parse_task_marks
from weaver.wiki.store import WikiStore

logger = logging.getLogger(__name__)

CHECK_PREFIX = "wiki-check:"
MESSAGE = "‘{title}’ 직접 수정은 반영하지 않고 되돌렸어요. 고칠 내용은 피드백 탭으로 보내 주세요"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MARK_ONLY = re.compile(
    r"^(\s*- )\[[ xX]\](.*?)(?: ✅ \d{4}-\d{2}-\d{2})?( \^w-[0-9a-f]{10}\s*)$",
    re.MULTILINE,
)


def _source_id(key: str) -> str:
    number = int(hashlib.sha256(key.encode()).hexdigest()[:32], 16)
    return "src_" + "".join(
        _ALPHABET[(number >> (5 * i)) & 31] for i in reversed(range(26))
    )


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _neutral(text: str) -> str:
    return _MARK_ONLY.sub(r"\1[ ]\2\3", text)


class CheckboxWatcher:
    def __init__(
        self,
        store: WikiStore,
        fabric: Fabric,
        state_path: Path,
        zone: ZoneInfo,
        publish_alert: Callable[[Alert], object] | None,
    ) -> None:
        self.store, self.fabric, self.zone = store, fabric, zone
        self.state_path = state_path
        self.publish_alert = publish_alert
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS discard_alerts "
                "(path TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (path, digest))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS rendered_done (block TEXT PRIMARY KEY)"
            )

    def _ticks(self) -> tuple[dict[str, date | None], set[str]]:
        """Blocks a person checked: checked in a file or conflict copy, unchecked in baseline.

        Weaver's own rendered ``[x]`` (already in the baseline) is never a tick.
        """
        ticked: dict[str, date | None] = {}
        in_file: set[str] = set()
        pages = self.store.readonly_pages()
        # Blocks Weaver ever rendered checked: a stale conflict copy of such a
        # page must not complete the request again after an app undo.
        with closing(sqlite3.connect(self.state_path)) as db, db:
            for _, baseline in pages:
                for block, mark in parse_task_marks(_text(baseline)).items():
                    if mark.checked:
                        db.execute(
                            "INSERT OR IGNORE INTO rendered_done VALUES (?)", (block,)
                        )
            rendered = {row[0] for row in db.execute("SELECT block FROM rendered_done")}
        for path, baseline in pages:
            try:
                target = self.store.path_for(path)
            except ValueError:
                continue
            current = {
                block
                for block, mark in parse_task_marks(_text(baseline)).items()
                if mark.checked
            }
            files = [target] if target.is_file() else []
            files += sorted(
                target.parent.glob(f"{glob.escape(target.stem)}.sync-conflict-*.md")
            )
            for file in files:
                try:
                    text = file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for block, mark in parse_task_marks(text).items():
                    # A stale [x] in a conflict copy never counts once Weaver has
                    # rendered it done; a fresh tick in the file itself may.
                    stale = block in current or (file != target and block in rendered)
                    if not mark.checked or stale:
                        continue
                    if file == target:
                        in_file.add(block)
                    prior = ticked.get(block)
                    ticked[block] = (
                        mark.done if prior is None else min(prior, mark.done or prior)
                    )
        # Blocks ticked only in conflict copies, not in the live file.
        return ticked, set(ticked) - in_file

    def collect(self, rows: list[Thread]) -> list[Thread]:
        """Record ticked open requests as completions; return rows including them."""
        ticked, copy_only = self._ticks()
        if not ticked:
            return rows
        retracted = {t.retracts for t in rows if t.retracts}
        resolved = {
            t.resolves
            for t in rows
            if t.resolves and not t.retracts and t.thread_id not in retracted
        }
        # A live wiki-check Thread means it was handled; a retracted one (app
        # undo) does not, but each later tick gets its own id.
        handled = {
            t.resolves
            for t in rows
            if t.thread_id.startswith(CHECK_PREFIX) and t.thread_id not in retracted
        }
        # A conflict copy is a stale replica: once any tick was recorded for the
        # request (even undone in the app), it cannot record another.
        ever = {t.resolves for t in rows if t.thread_id.startswith(CHECK_PREFIX)}
        undone: dict[str, int] = {}
        for t in rows:
            if t.thread_id.startswith(CHECK_PREFIX) and t.thread_id in retracted:
                if t.resolves:
                    undone[t.resolves] = undone.get(t.resolves, 0) + 1
        now = datetime.fromtimestamp(self.store.clock(), UTC)
        drafts: list[ThreadDraft] = []
        for t in rows:
            if (
                t.kind != "요청"
                or t.retracts
                or t.thread_id in retracted
                or t.thread_id in resolved
                or t.thread_id in handled
            ):
                continue
            block = block_id(t.thread_id)
            if block not in ticked or (block in copy_only and t.thread_id in ever):
                continue
            done = ticked[block]
            occurred = (
                datetime.combine(done, time(12), tzinfo=self.zone) if done else now
            )
            check_id = f"{CHECK_PREFIX}{t.thread_id}"
            if undone.get(t.thread_id):
                check_id += f":{undone[t.thread_id]}"
            drafts.append(
                ThreadDraft(
                    thread_id=check_id,
                    kind="결정",
                    actor="human",
                    content="Obsidian에서 완료 표시",
                    occurred_at=occurred,
                    recorded_at=now,
                    source_id=_source_id(check_id),
                    tags=t.tags,
                    counterpart=t.counterpart,
                    direction=None,
                    field=None,
                    value=None,
                    resolves=t.thread_id,
                    retracts=None,
                    correction=None,
                )
            )
        if not drafts:
            return rows
        try:
            return rows + self.fabric.append(drafts)
        except FabricError:
            # A concurrent writer recorded one; the next run sees it.
            logger.warning("checkbox completion append skipped")
            return rows

    def _title(self, path: str) -> str:
        try:
            title = self.store.read(path)[0].get("title")
        except (OSError, ValueError):
            title = None
        return title if isinstance(title, str) and title else Path(path).stem

    def on_change(self, path: str, current: bytes | None, baseline: bytes) -> bool:
        """Whether the file may be restored now; alerts once for non-checkbox edits."""
        if _neutral(_text(current)) == _neutral(_text(baseline)):
            return True
        digest = hashlib.sha256(current or b"").hexdigest()
        with closing(sqlite3.connect(self.state_path)) as db, db:
            seen = db.execute(
                "SELECT 1 FROM discard_alerts WHERE path=? AND digest=?",
                (path, digest),
            ).fetchone()
            if seen is None and self.publish_alert is not None:
                package = "wiki:" + self.store.page_id(path)
                try:
                    created = datetime.fromtimestamp(
                        self.store.path_for(path).stat().st_mtime, UTC
                    )
                except (OSError, ValueError):
                    created = datetime.fromtimestamp(self.store.clock(), UTC)
                try:
                    self.publish_alert(
                        Alert(
                            schema_version=2,
                            alert_id=alert_id_for(package, "wiki-overwrite", digest),
                            package_id=package,
                            thread_id="wiki-overwrite",
                            message=MESSAGE.format(title=self._title(path)),
                            created_at=created,
                        )
                    )
                except Exception:
                    # Keep the edit until the alert can be delivered.
                    logger.exception("wiki overwrite alert failed")
                    return False
            if seen is None:
                db.execute("INSERT INTO discard_alerts VALUES (?, ?)", (path, digest))
        return True
