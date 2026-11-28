"""Captured text becomes knowledge only after its review is approved.

Both file and HTTP submissions retain the original bytes outside the vault,
produce one approved Fabric decision and publish a linked Wiki page. Reopening
the same data directories preserves the result without another paid model call.
"""

from datetime import datetime

from weaver.llm.errors import LlmTimeout

# Independent oracles: changing a provider fixture must not change expectations.
TEXT = "검색 시안을 금요일에 검토하기로 했다."
AT = "2026-11-07T15:32:00+09:00"


def assert_knowledge(world, package):
    [thread] = world.read_threads()
    assert (thread.kind, thread.content, thread.actor, thread.source_id) == (
        "결정",
        TEXT,
        "ai_approved",
        package.source_id,
    )
    assert thread.occurred_at == datetime.fromisoformat(AT)
    assert thread.tags == ["검색 운영"]
    page = world.read_page("pages/검색 운영.md")
    assert TEXT in page
    assert package.source_id in page
    summaries = list((world.config.vault_root / "Weaver/summaries").glob("*.md"))
    assert len(summaries) == 1
    assert TEXT in summaries[0].read_text()
    assert summaries[0].stem in page
    assert "[[pages/검색 운영" in world.read_page("index.md")


def assert_restart_preserves_knowledge(world, package):
    threads = world.read_threads()
    page = world.read_page("pages/검색 운영.md")
    calls = list(world.llm.calls)
    world.restart()
    world.step()
    assert world.read_threads() == threads
    assert world.read_page("pages/검색 운영.md") == page
    assert world.llm.calls == calls
    assert_knowledge(world, package)


def test_file_capture_approval_publishes_knowledge_once(world_factory):
    world = world_factory()
    capture = world.capture()
    world.step()
    receipt = world.receipt(capture)
    assert receipt.sha256 == capture.sha256
    archived = world.archive.locate(receipt.source_id).payload_path
    assert archived.read_bytes() == TEXT.encode()
    assert not archived.is_relative_to(world.config.vault_root)
    package = world.read_review()
    assert package.source_id == receipt.source_id
    assert package.draft_body == TEXT
    assert world.read_threads() == []
    assert not (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    world.restart()  # Pending review survives an ordinary server restart.
    world.approve(package)
    world.run_until(
        lambda: (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    )
    assert_knowledge(world, package)
    assert_restart_preserves_knowledge(world, package)


def test_http_capture_approval_survives_unavailable_receipt_folder(
    world_factory, monkeypatch
):
    world = world_factory(transport="http")
    with world.receipt_io_failure(monkeypatch):
        capture = world.capture()
        receipt = world.receipt(capture)
    assert receipt.sha256 == capture.sha256
    archived = world.archive.locate(receipt.source_id).payload_path
    assert archived.read_bytes() == TEXT.encode()
    assert not archived.is_relative_to(world.config.vault_root)
    world.step()
    package = world.read_review()
    assert package.source_id == receipt.source_id
    assert world.read_threads() == []
    assert not (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    operation = world.approve(package)
    assert world.operation(operation)["state"] == "queued"
    world.restart()  # The accepted HTTP inbox is durable before application.
    world.run_until(
        lambda: (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    )
    assert world.operation(operation)["state"] == "applied"
    assert_knowledge(world, package)
    assert_restart_preserves_knowledge(world, package)


def test_failed_provider_cannot_turn_an_approval_into_facts(world_factory):
    world = world_factory()
    world.capture()
    world.step()
    package = world.read_review()
    world.approve(package)
    with world.llm_failure("loom.approve", LlmTimeout()):
        world.step()
    assert world.read_threads() == []
    assert not (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    world.restart()
    world.run_until(
        lambda: (world.config.vault_root / "Weaver/pages/검색 운영.md").exists()
    )
    assert_knowledge(world, package)
