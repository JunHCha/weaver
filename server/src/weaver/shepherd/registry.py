"""Capture-only instances share the existing hashed-token/revocation journal."""

import re
import secrets
import uuid
from pathlib import Path
from typing import Literal

from weaver.mobile.store import Device, MobileStore, hash_token, now_iso
from weaver.private import PrivateFile

Transport = Literal["loopback", "tailnet"]


class Registry:
    def __init__(self, store: MobileStore):
        self.store = store

    def register(
        self,
        instance_id: str,
        token_file: Path,
        *,
        transport: Transport = "loopback",
        node_id: str | None = None,
        login_name: str | None = None,
    ) -> Device:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", instance_id):
            raise ValueError("invalid_instance_id")
        if transport not in ("loopback", "tailnet"):
            raise ValueError("invalid_shepherd_transport")
        if (transport == "tailnet" and (not node_id or not node_id.strip())) or (
            transport == "loopback" and (node_id is not None or login_name is not None)
        ):
            raise ValueError("invalid_shepherd_binding")
        token = "wvr_" + secrets.token_urlsafe(32)
        private = PrivateFile(token_file)
        with private.locked() as parent, self.store._write() as db:
            row = db.execute(
                "SELECT device_id FROM shepherds WHERE instance_id=?", (instance_id,)
            ).fetchone()
            device_id = str(row[0]) if row else "dev_" + uuid.uuid4().hex
            # Publish before DB commit: an interrupted registration fails closed.
            # Rerunning it repairs the credential without changing namespace/IDs.
            private.write(parent, (token + "\n").encode())
            db.execute(
                """INSERT INTO devices VALUES (?,?,?,?,?,'capture',0,?,NULL)
                ON CONFLICT(device_id) DO UPDATE SET node_id=excluded.node_id,
                login_name=excluded.login_name, token_hash=excluded.token_hash,
                scopes='capture', executor=0, revoked_at=NULL""",
                (
                    device_id,
                    instance_id,
                    node_id or "local:" + instance_id,
                    login_name,
                    hash_token(token),
                    now_iso(),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO shepherds VALUES (?,?,?)",
                (instance_id, device_id, transport),
            )
        device = self.store.device(device_id)
        assert device is not None
        return device

    def transport(self, device_id: str) -> str | None:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT transport FROM shepherds WHERE device_id=?", (device_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def revoke(self, instance_id: str) -> bool:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT device_id FROM shepherds WHERE instance_id=?", (instance_id,)
            ).fetchone()
        return bool(row and self.store.revoke(str(row[0])))
