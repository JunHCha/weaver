"""Protected wiki pages and durable human edit delivery."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

HASH_KEY = "weaver_content_sha256"


def normalize_path(path: str) -> str:
    """Canonical spelling for wiki keys, git paths, and links."""
    return unicodedata.normalize("NFC", path)


def _disk_child(parent: Path, name: str) -> Path:
    """Resolve existing decomposed names on normalization-sensitive filesystems."""
    target = parent / name
    if target.exists() or target.is_symlink() or not parent.is_dir():
        return target
    matches = [p for p in parent.iterdir() if normalize_path(p.name) == name]
    if len(matches) > 1:
        raise ValueError("ambiguous normalized wiki path")
    return matches[0] if matches else target


def _without_comment(text: str) -> str:
    """Strip YAML comments while respecting doubled quotes and JSON escapes."""
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and text[i : i + 2] == "''":
                    i += 2
                    continue
                quote = None
        elif char == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].strip()
        elif char in ("'", '"') and (
            i == 0 or text[i - 1].isspace() or text[i - 1] in "[,"
        ):
            quote = char
        i += 1
    if quote is not None:
        raise ValueError("unclosed quote")
    return text.strip()


def _scalar(text: str) -> JsonValue:
    text = _without_comment(text.strip())
    if not text or text in ("null", "Null", "NULL", "~"):
        return None
    if text.startswith("'"):
        if re.fullmatch(r"'(?:[^']|'')*'", text) is None:
            raise ValueError("unsupported quoted scalar")
        return text[1:-1].replace("''", "'")
    if text in ("true", "True", "TRUE", "false", "False", "FALSE"):
        return text.lower() == "true"
    if text.startswith(('"', "[")):
        value: JsonValue = json.loads(text)
        if isinstance(value, list) and any(
            isinstance(item, (dict, list)) for item in value
        ):
            raise ValueError("unsupported nested value")
        return value
    if (
        text.startswith(("{", "|", ">", "&", "*", "!", "- ", "? ", "@", "`", "%"))
        or text
        in (
            "-",
            "?",
            "yes",
            "Yes",
            "YES",
            "no",
            "No",
            "NO",
            "on",
            "On",
            "ON",
            "off",
            "Off",
            "OFF",
        )
        or re.search(r":(?:\s|$)", text)
        or any(char in text for char in "[]{}")
    ):
        raise ValueError("unsupported scalar")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", text):
        return int(text)
    # Other YAML numeric spellings and implicit booleans are outside this
    # dialect. Reject them rather than turning a typed value into human text.
    if re.fullmatch(
        r"[-+]?(?:0[xob][0-9a-f_]+|[0-9][0-9_]*(?:\.[0-9_]*)?"
        r"(?::[0-9_]+(?:\.[0-9_]*)?)*(?:e[-+]?[0-9]+)?|"
        r"\.[0-9]+(?:e[-+]?[0-9]+)?|\.inf|\.nan)",
        text,
        re.IGNORECASE,
    ):
        raise ValueError("unsupported scalar type")
    return text


class HumanEditPending(Exception):
    """A page changed or is too recent to safely overwrite."""

    def __init__(self, path: str, *, ready_at: float | None = None) -> None:
        super().__init__(path)
        self.ready_at = ready_at


@dataclass(frozen=True)
class HumanEdit:
    """A committed change awaiting acknowledgement by its consumer."""

    path: str
    base_rev: str
    new_rev: str
    diff: str
    frontmatter_before: dict[str, JsonValue]
    frontmatter_after: dict[str, JsonValue]


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _properties(data: bytes) -> dict[str, JsonValue]:
    text = data.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    result: dict[str, JsonValue] = {}
    current: str | None = None
    sequence_allowed = False
    sequence_indent: int | None = None
    for line in lines[1:]:
        if line == "---":
            return result
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped == "-" or stripped.startswith("- "):
            if (
                current is None
                or not sequence_allowed
                or (sequence_indent is not None and indent != sequence_indent)
            ):
                raise ValueError("invalid block sequence")
            sequence_indent = indent
            item = _scalar(stripped[1:])
            if isinstance(item, (list, dict)):
                raise ValueError("nested frontmatter")
            value = result[current]
            if value is None:
                value = []
                result[current] = value
            assert isinstance(value, list)
            value.append(item)
            continue
        key, sep, value = line.partition(":")
        if (
            indent
            or not sep
            or not re.fullmatch(r"[A-Za-z_][\w-]*", key)
            or key in result
        ):
            raise ValueError("invalid flat frontmatter")
        current = key
        sequence_allowed = not value.strip() or value.lstrip().startswith("#")
        sequence_indent = None
        if key == "schedule" and value.strip().startswith("{"):
            parsed = json.loads(value)
            if not isinstance(parsed, dict) or any(
                isinstance(v, (dict, list)) for v in parsed.values()
            ):
                raise ValueError("invalid schedule frontmatter")
            result[key] = parsed
        else:
            result[key] = _scalar(value)
    raise ValueError("unclosed frontmatter")


def _render(properties: dict[str, JsonValue], body: str) -> bytes:
    lines = ["---"]
    for key, value in properties.items():
        if key == HASH_KEY:
            continue
        if not re.fullmatch(r"[A-Za-z_][\w-]*", key):
            raise ValueError("invalid frontmatter key")
        if (isinstance(value, dict) and key != "schedule") or (
            isinstance(value, list) and any(isinstance(v, (list, dict)) for v in value)
        ):
            raise ValueError("frontmatter must be flat")
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    unsigned = ("\n".join([*lines, "---", body]).rstrip("\n") + "\n").encode()
    lines.append(f"{HASH_KEY}: {_hash(unsigned)}")
    return ("\n".join([*lines, "---", body]).rstrip("\n") + "\n").encode()


class WikiStore:
    """Manage pages rooted under a vault, with history outside that vault.

    ``clock`` returns Unix seconds; ``settle_seconds`` controls the quiet period.
    Paths are relative to the wiki root. A write returns the resulting file hash.
    """

    def __init__(
        self,
        vault: Path,
        state_root: Path,
        *,
        wiki_root: str = "Weaver",
        settle_seconds: float = 1800,
        clock: Callable[[], float] = time.time,
    ) -> None:
        relative_root = Path(normalize_path(wiki_root))
        if (
            relative_root.is_absolute()
            or not relative_root.parts
            or any(
                part in (".", "..") or part.startswith(".")
                for part in relative_root.parts
            )
        ):
            raise ValueError("invalid wiki root")
        self.root = vault
        for part in relative_root.parts:
            self.root = _disk_child(self.root, part)
            if self.root.is_symlink():
                raise ValueError("symlink in wiki root")
            self.root.mkdir(exist_ok=True, parents=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("invalid wiki root")
        state_root.mkdir(parents=True, exist_ok=True)
        if state_root.resolve().is_relative_to(vault.resolve()):
            raise ValueError("state root must be outside vault")
        self.git_dir = state_root / "wiki.git"
        self.clock = clock
        self.settle_seconds = settle_seconds
        if not self.git_dir.exists():
            subprocess.run(
                ["git", "init", "--bare", str(self.git_dir)],
                check=True,
                capture_output=True,
            )
        if subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
        ).returncode:
            self._git("commit", "--allow-empty", "-m", "initialize wiki")
        self.db_path = state_root / "wiki.sqlite3"
        with self._db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, content BLOB NOT NULL, rev TEXT NOT NULL, readonly INTEGER NOT NULL, candidate TEXT, since REAL, mtime REAL);
                CREATE TABLE IF NOT EXISTS pending (rev TEXT PRIMARY KEY, payload TEXT NOT NULL);""")
            # Keep an existing canonical baseline; legacy aliases must never
            # become new human edits. This transaction is safe to repeat.
            for (path,) in db.execute(
                "SELECT path FROM pages ORDER BY path"
            ).fetchall():
                canonical = normalize_path(path)
                if path != canonical:
                    db.execute(
                        "UPDATE OR IGNORE pages SET path=? WHERE path=?",
                        (canonical, path),
                    )
                    db.execute("DELETE FROM pages WHERE path=?", (path,))
            for rev, payload in db.execute(
                "SELECT rev, payload FROM pending"
            ).fetchall():
                edit = json.loads(payload)
                edit["path"] = normalize_path(edit["path"])
                db.execute(
                    "UPDATE pending SET payload=? WHERE rev=?",
                    (json.dumps(edit, ensure_ascii=False), rev),
                )

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _path(self, path: str) -> Path:
        relative = Path(normalize_path(path))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(
                p in (".", "..")
                or p.startswith(".")
                or p.endswith(("~", ".tmp", ".swp"))
                for p in relative.parts
            )
        ):
            raise ValueError("invalid wiki path")
        current = self.root
        if current.is_symlink():
            raise ValueError("symlink root")
        for part in relative.parts:
            current = _disk_child(current, part)
            if current.is_symlink():
                raise ValueError("symlink in path")
        return current

    def _git(self, *args: str, author: str = "weaver") -> str:
        env = os.environ.copy()
        env.update(
            GIT_AUTHOR_NAME=author,
            GIT_AUTHOR_EMAIL=f"{author}@localhost",
            GIT_COMMITTER_NAME=author,
            GIT_COMMITTER_EMAIL=f"{author}@localhost",
        )
        return subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.git_dir),
                "--work-tree",
                str(self.root),
                *args,
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _commit(self, path: str, data: bytes, author: str, message: str) -> str:
        path = normalize_path(path)
        # Commit the bytes that were verified, never a later version read by git.
        env = os.environ.copy()
        env.update(
            GIT_AUTHOR_NAME=author,
            GIT_AUTHOR_EMAIL=f"{author}@localhost",
            GIT_COMMITTER_NAME=author,
            GIT_COMMITTER_EMAIL=f"{author}@localhost",
        )
        with tempfile.TemporaryDirectory(dir=self.git_dir) as directory:
            env["GIT_INDEX_FILE"] = str(Path(directory) / "index")
            command = [
                "git",
                "--git-dir",
                str(self.git_dir),
                "--work-tree",
                str(self.root),
            ]

            def run(*args: str, input_data: bytes | None = None) -> str:
                return (
                    subprocess.run(
                        [*command, *args],
                        input=input_data,
                        env=env,
                        check=True,
                        capture_output=True,
                    )
                    .stdout.decode()
                    .strip()
                )

            run("read-tree", "HEAD")
            blob = run("hash-object", "-w", "--stdin", input_data=data)
            run("update-index", "--add", "--cacheinfo", "100644", blob, path)
            run("commit", "--allow-empty", "-m", message)
            return run("rev-parse", "HEAD")

    def _before_replace(self) -> None:
        pass

    def _replace(self, path: str, data: bytes, expected: str | None) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".weaver-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._before_replace()
            target = self._path(path)
            actual = _hash(target.read_bytes()) if target.exists() else None
            if actual != expected:
                raise HumanEditPending(path)
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self, path: str) -> tuple[dict[str, JsonValue], str, str]:
        """Return frontmatter, body, and the current full-file hash."""
        data = self._path(path).read_bytes()
        properties = _properties(data)
        text = data.decode("utf-8")
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
        return properties, body, _hash(data)

    def is_tracked_version(self, path: str, digest: str) -> bool:
        """Whether the hash matches the latest recorded version of this page."""
        path = normalize_path(path)
        self._path(path)
        with self._db() as db:
            row = db.execute(
                "SELECT content FROM pages WHERE path=?", (path,)
            ).fetchone()
        return row is not None and _hash(row[0]) == digest

    def check_write(self, path: str, base_hash: str | None) -> None:
        """Check hash and quiet period before expensive generation.

        This is advisory: write checks again because the file may change meanwhile.
        """
        path = normalize_path(path)
        target = self._path(path)
        current = target.read_bytes() if target.exists() else None
        if (None if current is None else _hash(current)) != base_hash:
            raise HumanEditPending(path)
        if (
            current is not None
            and self.clock() - target.stat().st_mtime < self.settle_seconds
        ):
            raise HumanEditPending(
                path, ready_at=target.stat().st_mtime + self.settle_seconds
            )

    def write(
        self,
        path: str,
        frontmatter: dict[str, JsonValue],
        body: str,
        base_hash: str | None,
        *,
        metadata: str = "wiki update",
        readonly: bool = False,
    ) -> str:
        """Write and commit a page if its hash and quiet period permit it."""
        path = normalize_path(path)
        self.check_write(path, base_hash)
        data = _render(frontmatter, body)
        self._replace(path, data, base_hash)
        rev = self._commit(path, data, "weaver", metadata)
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (path, data, rev, int(readonly), None),
            )
        return _hash(data)

    def published_pages(self) -> list[tuple[str, bytes, datetime]]:
        """Committed full-file versions only; pending human edits are not outputs."""
        with self._db() as db:
            rows = db.execute("SELECT path, content FROM pages").fetchall()
        result = []
        for path, baseline in rows:
            try:
                target = self._path(path)
                if target.is_file() and target.read_bytes() == baseline:
                    result.append(
                        (
                            path,
                            baseline,
                            datetime.fromtimestamp(target.stat().st_mtime, UTC),
                        )
                    )
            except (ValueError, OSError):
                continue
        return result

    def scan(self, now: float) -> list[HumanEdit]:
        """Confirm stable human edits and return all unacknowledged edits."""
        with self._db() as db:
            known = {row[0] for row in db.execute("SELECT path FROM pages")}
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                base = Path(directory)
                dirs[:] = [
                    name
                    for name in dirs
                    if not name.startswith(".") and not (base / name).is_symlink()
                ]
                for name in files:
                    path = normalize_path(
                        (base / name).relative_to(self.root).as_posix()
                    )
                    if path in known:
                        continue
                    try:
                        target = self._path(path)
                        if target.is_file():
                            db.execute(
                                "INSERT INTO pages VALUES (?, ?, ?, 0, NULL, NULL, NULL)",
                                (path, b"", self._git("rev-parse", "HEAD")),
                            )
                            known.add(path)
                    except ValueError:
                        continue
            rows = db.execute(
                "SELECT path, content, rev, readonly, candidate, since, mtime FROM pages"
            ).fetchall()
            for path, baseline, base_rev, readonly, candidate, since, mtime in rows:
                if readonly:
                    continue
                try:
                    target = self._path(path)
                    stat = target.stat()
                    if not target.is_file():
                        continue
                    if candidate is None and stat.st_mtime == mtime:
                        continue
                    data = target.read_bytes()
                    digest = _hash(data)
                    if digest == _hash(baseline):
                        db.execute(
                            "UPDATE pages SET candidate=NULL, since=NULL, mtime=? WHERE path=?",
                            (stat.st_mtime, path),
                        )
                    elif digest != candidate:
                        db.execute(
                            "UPDATE pages SET candidate=?, since=?, mtime=? WHERE path=?",
                            (digest, now, stat.st_mtime, path),
                        )
                    elif (
                        since is not None
                        and now - since >= self.settle_seconds
                        and now - stat.st_mtime >= self.settle_seconds
                    ):
                        if _hash(target.read_bytes()) != digest:
                            continue
                        head = self._git("rev-parse", "HEAD")
                        entry = self._git("ls-tree", "-z", head, "--", path)
                        if entry:
                            blob = entry.split("\t", 1)[0].split()[2]
                            committed = subprocess.check_output(
                                [
                                    "git",
                                    "--git-dir",
                                    str(self.git_dir),
                                    "cat-file",
                                    "blob",
                                    blob,
                                ]
                            )
                            if committed == data:
                                db.execute(
                                    "UPDATE pages SET content=?, rev=?, candidate=NULL, since=NULL, mtime=? WHERE path=?",
                                    (data, head, stat.st_mtime, path),
                                )
                                continue
                        before, after = _properties(baseline), _properties(data)
                        new_rev = self._commit(
                            path, data, "human", f"human edit: {path}"
                        )
                        diff = self._git("diff", base_rev, new_rev, "--", path)
                        edit = HumanEdit(path, base_rev, new_rev, diff, before, after)
                        db.execute(
                            "UPDATE pages SET content=?, rev=?, candidate=NULL, since=NULL, mtime=? WHERE path=?",
                            (data, new_rev, stat.st_mtime, path),
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO pending VALUES (?, ?)",
                            (new_rev, json.dumps(asdict(edit), ensure_ascii=False)),
                        )
                except (FileNotFoundError, ValueError, UnicodeDecodeError):
                    continue
            pending = db.execute(
                "SELECT payload FROM pending ORDER BY rowid"
            ).fetchall()
        return [HumanEdit(**json.loads(row[0])) for row in pending]

    def acknowledge(self, edit: HumanEdit) -> None:
        """Stop delivering a previously confirmed edit; repeated calls are safe."""
        with self._db() as db:
            db.execute("DELETE FROM pending WHERE rev=?", (edit.new_rev,))

    def restore_readonly(self) -> None:
        """Restore registered read-only pages without emitting human edits."""
        with self._db() as db:
            for path, content in db.execute(
                "SELECT path, content FROM pages WHERE readonly=1"
            ).fetchall():
                try:
                    target = self._path(path)
                    current = target.read_bytes() if target.exists() else None
                    if current != content:
                        self._replace(
                            path, content, None if current is None else _hash(current)
                        )
                except (ValueError, HumanEditPending):
                    continue
