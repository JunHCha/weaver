from datetime import UTC, datetime

from weaver.courier import CourierV2
from weaver.schema.v2 import ReviewPackage, Verdict


def test_v2_publication_and_verdict(paths, captures, tmp_path):
    class Store:
        applied = False
        verdict = None

        def latest_rev(self, package_id):
            return 1 if package_id == "pkg_example" else None

        def is_applied(self, package_id, rev):
            return self.applied

        def apply(self, verdict):
            self.verdict = verdict
            self.applied = True

    store = Store()
    courier = CourierV2(paths, captures, store, store)
    package = ReviewPackage(
        schema_version=2,
        package_id="pkg_example",
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        revision=1,
        draft_body="요약",
        questions=[],
        stage="검토",
        stage_times={"검토": datetime.now(UTC)},
    )
    target = courier.publish_package(package)
    assert ReviewPackage.model_validate_json(target.read_bytes()) == package
    verdict = Verdict(
        schema_version=2,
        package_id=package.package_id,
        revision=1,
        action="approve",
        answers=[],
        edited_body=None,
        feedback=None,
    )
    decision = paths.decisions / target.name
    decision.write_text(verdict.model_dump_json())
    report = courier.scan_once()
    assert report.applied == [(package.package_id, 1)]
    assert store.verdict == verdict
    assert not decision.exists() and not target.exists()


def test_v2_rejects_v1_decision(paths, captures):
    class Store:
        def latest_rev(self, package_id):
            return 1

        def is_applied(self, package_id, rev):
            return False

        def apply(self, verdict):
            raise AssertionError("must not apply")

    store = Store()
    courier = CourierV2(paths, captures, store, store)
    path = paths.decisions / "pkg_example.r1.json"
    path.write_text('{"schema_version":1}')
    assert courier.scan_once().problems["decisions/" + path.name] == "unknown_version"
    assert path.exists()
