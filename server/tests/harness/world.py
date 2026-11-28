"""Capture and approval use production adapters over temporary durable stores.

Restart reopens runtime and HTTP objects over the same directories. Only the
provider sink and peer lookup are fake; no SQL rows stand in for domain work.
The one-pass loop hook stays here so specifications use observable outcomes.
"""

import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

from weaver.config import load_config
from weaver.fabric import FabricStore, ThreadQuery
from weaver.loom.summary_state import SummaryState
from weaver.mobile.api import create_app
from weaver.mobile.store import MobileStore
from weaver.runtime.assemble import Plugins, build_runtime
from weaver.schema import Manifest, Receipt, ReviewPackage, Verdict

from .llm import AT, TEXT, SyntheticLlm

PREFIX = "/mobile/v1"
NODE = "nSYNTHETIC01"
LOGIN = "synthetic@example.invalid"


class SyntheticWhois:
    def lookup(self, host, port):
        from weaver.mobile.whois import PeerIdentity

        return PeerIdentity(NODE, LOGIN)


class World:
    def __init__(self, root, *, transport="files"):
        self.transport = transport
        self.root = root
        root.mkdir(parents=True)
        path = root / "config.toml"
        path.write_text(
            "\n".join(
                f'{key}_root = "{key}"'
                for key in ("sync", "archive", "vault", "state", "log")
            )
            + '\n[llm]\nbackend = "none"\n'
            + f'\n[mobile]\nbind = "127.0.0.1"\ntransport = "{transport}"\n'
            + "\n[wiki]\nenabled = true\nsettle_seconds = 0\ninterval_seconds = 1\n"
        )
        self.config = load_config(path, environ={}, init_directories=True)
        self.llm = SyntheticLlm()
        self.now = datetime.fromisoformat(AT).timestamp()
        self._capture_number = 0
        self.http = None
        self.restart()
        _, token = self.mobile.create_device(
            "synthetic",
            NODE,
            frozenset({"read", "capture", "review"}),
            login_name=LOGIN,
        )
        self.headers = {"Authorization": f"Bearer {token}"}
        self.human = self.config.vault_root / "Human.md"
        self.human.write_text("합성 사람이 쓴 메모\n")

    def restart(self):
        self.close()
        self.runtime = build_runtime(
            self.config, plugins=Plugins(client=self.llm), environ={}
        )
        self.runtime.clock = lambda: self.now
        self.runtime._wait = lambda delay: True
        self.archive = self.runtime.feed
        self.fabric = FabricStore.from_config(self.config)
        self.mobile = MobileStore(self.config.state_root)
        self.summary = SummaryState(self.config.state_root)
        self.http = TestClient(
            create_app(
                self.config,
                SyntheticWhois(),
                store=self.mobile,
                archive=self.archive,
                summary=self.summary,
            ),
            base_url="https://127.0.0.1:8787",
            client=("100.108.10.0", 40000),
        )

    def close(self):
        if self.http is not None:
            self.http.close()

    def advance(self, seconds=60):
        self.now += seconds

    def step(self):
        self.advance()
        self.runtime.run(threading.Event())
        assert self.runtime.consecutive_failures == 0
        assert self.human.read_text() == "합성 사람이 쓴 메모\n"

    def run_until(self, outcome, *, passes=5):
        for _ in range(passes):
            self.step()
            if outcome():
                return
        raise AssertionError("World did not reach the requested observable outcome")

    def capture(self, text=TEXT):
        self._capture_number += 1
        cap = f"cap_{self._capture_number:026d}"
        body = text.encode()
        manifest = Manifest.model_validate(
            {
                "schema_version": 1,
                "capture_id": cap,
                "kind": "text",
                "channel": "share_text",
                "captured_at": AT,
                "occurred_at": AT,
                "payload": f"{cap}.txt",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
                "hints": {"counterpart": None},
            }
        )
        if self.transport == "files":
            outbox = self.config.courier.outbox
            (outbox / manifest.payload).write_bytes(body)
            self._atomic_wire(outbox / f"{cap}.json", manifest)
        else:
            response = self.http.put(
                f"{PREFIX}/captures/{cap}/payload",
                content=body,
                headers=self.headers | {"X-Content-SHA256": manifest.sha256},
            )
            assert response.status_code == 201, response.text
            response = self.http.put(
                f"{PREFIX}/captures/{cap}/manifest",
                content=manifest.model_dump_json(),
                headers=self.headers,
            )
            assert response.status_code == 201, response.text
        return manifest

    @staticmethod
    def _atomic_wire(path, model):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(model.model_dump_json())
        temporary.replace(path)

    def receipt(self, manifest):
        cap = manifest.capture_id
        if self.transport == "files":
            return Receipt.model_validate_json(
                (self.config.courier.receipts / f"{cap}.json").read_bytes()
            )
        response = self.http.get(
            f"{PREFIX}/captures/{cap}/receipt", headers=self.headers
        )
        assert response.status_code == 200, response.text
        return Receipt.model_validate(response.json())

    def read_review(self):
        if self.transport == "files":
            paths = list(self.config.courier.proposals.glob("*.json"))
            assert len(paths) == 1
            return ReviewPackage.model_validate_json(paths[0].read_bytes())
        response = self.http.get(f"{PREFIX}/reviews", headers=self.headers)
        assert response.status_code == 200, response.text
        [entry] = response.json()["items"]
        response = self.http.get(
            f"{PREFIX}/reviews/{entry['package_id']}/revisions/{entry['revision']}",
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        return ReviewPackage.model_validate(response.json())

    def approve(self, package):
        verdict = Verdict(
            schema_version=2,
            package_id=package.package_id,
            revision=package.revision,
            action="approve",
            answers=[],
            edited_body=None,
            feedback=None,
        )
        if self.transport == "files":
            self._atomic_wire(
                self.config.courier.decisions
                / f"{package.package_id}.r{package.revision}.json",
                verdict,
            )
            return None
        url = f"{PREFIX}/reviews/{package.package_id}/revisions/{package.revision}"
        response = self.http.get(url, headers=self.headers)
        assert response.status_code == 200, response.text
        response = self.http.put(
            url + "/verdict",
            json=verdict.model_dump(mode="json"),
            headers=self.headers | {"If-Match": response.headers["etag"]},
        )
        assert response.status_code == 202, response.text
        return response.json()["operation_id"]

    def operation(self, operation_id):
        response = self.http.get(
            f"{PREFIX}/operations/{operation_id}", headers=self.headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    def read_threads(self):
        return self.fabric.entries(ThreadQuery())

    def read_page(self, path):
        return (self.config.vault_root / "Weaver" / path).read_text()

    @contextmanager
    def llm_failure(self, task, error):
        self.llm.failures[task] = error
        try:
            yield
        finally:
            del self.llm.failures[task]

    @contextmanager
    def receipt_io_failure(self, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("synthetic shared-folder failure")

        with monkeypatch.context() as patch:
            patch.setattr(self.archive.receipt_publisher, "publish", fail)
            yield
