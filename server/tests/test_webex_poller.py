"""Synthetic REST assumptions: descending created, replies in list, Link next.

These fixtures are not captured real API traffic; retention, filtering and reply
coverage still require manual pilot acceptance. Never use a real token or API.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from weaver.shepherd.outbox import CapacityError, Outbox, OutboxLimits
from weaver.spinner.webex import WebexBundle
from weaver.webex.poller import CURSOR, Poller, PollError, State
from weaver.webex.settings import WebexSettings

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
BASE = "https://webexapis.com/v1"


class Clock:
    now = NOW

    def __call__(self):
        return self.now


class Tokens:
    def __init__(self):
        self.refreshes = []

    def access_token(self, *, force_refresh=False):
        self.refreshes.append(force_refresh)
        return "fake-refreshed" if force_refresh else "fake-token"


def message(mid="m1", *, days=1, space="S", **extra):
    return {
        "id": mid,
        "roomId": space,
        "created": (NOW - timedelta(days=days)).isoformat(),
        "text": "synthetic",
        **extra,
    }


def page(items, next_url=None):
    return httpx.Response(
        200,
        json={"items": items},
        headers={"Link": f'<{next_url}>; rel="next"'} if next_url else {},
    )


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    settings = WebexSettings(
        request_budget_total=20,
        request_budget_incremental=10,
        request_budget_overlap=5,
        request_budget_deletion=2,
    )
    box = Outbox(
        tmp_path / "private" / "outbox.sqlite3",
        settings.instance_id,
        OutboxLimits(min_free_bytes=1),
    )

    def make(handler, override=None, tokens=None):
        calls = []

        def respond(request):
            assert request.headers["Authorization"].startswith("Bearer fake-")
            assert request.method == "GET"
            assert request.url.host == "webexapis.com"
            calls.append(request)
            return handler(request)

        poller = Poller(
            override or settings,
            box,
            tokens or Tokens(),
            httpx.Client(transport=httpx.MockTransport(respond)),
            clock=clock,
            jitter=lambda: 0.25,
        )
        return poller, calls

    return make, box, clock, settings


def bundles(box):
    return [
        WebexBundle.model_validate_json(box.get(e.capture_id).payload)
        for e in box.entries()
    ]


def test_first_backfill_exclusions_new_spaces_and_metadata_only(setup):
    make, box, clock, settings = setup

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}, {"id": "EXCLUDED"}])
        assert req.url.params["roomId"] == "S"
        assert "after" not in req.url.params
        return page(
            [
                message(files=["https://fixture.invalid/attachment"]),
                message("too-old", days=8),
            ]
        )

    poller, calls = make(respond, replace(settings, exclude_space_ids=("EXCLUDED",)))
    poller.tick()
    assert len(calls) == 3  # list + incremental + separate overlap
    assert [m.message_id for m in bundles(box)[0].messages] == ["m1"]
    assert (
        bundles(box)[0].messages[0].attachments[0].url
        == "https://fixture.invalid/attachment"
    )
    assert poller.state.spaces["S"].high_water == (NOW - timedelta(days=1), "m1")
    assert "EXCLUDED" not in poller.state.spaces
    # A newly discovered excluded Space remains excluded on every discovery.
    clock.now += timedelta(minutes=5)
    poller.tick()
    assert all(r.url.path in ("/v1/rooms", "/v1/messages") for r in calls)
    assert all(r.url.params.get("roomId") != "EXCLUDED" for r in calls)


def test_empty_link_page_resumes_after_budget_and_restart(setup):
    make, box, clock, settings = setup
    next_url = f"{BASE}/messages?roomId=S&beforeMessage=m1"

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        if req.url.params.get("beforeMessage"):
            return page([message("m2", days=2)])
        return page([], next_url)

    settings = replace(settings, request_budget_incremental=2, request_budget_overlap=1)
    poller, calls = make(respond, settings)
    poller.tick()
    state = State.model_validate_json(box.cursor(CURSOR))
    assert state.spaces["S"].high_water is None
    assert state.spaces["S"].incremental.url == next_url
    clock.now += timedelta(minutes=5)
    restarted, calls2 = make(respond, settings)
    restarted.tick()
    assert any(r.url.params.get("beforeMessage") == "m1" for r in calls2)
    assert restarted.state.spaces["S"].high_water == (NOW - timedelta(days=2), "m2")
    assert len(bundles(box)) == 1


def test_discovery_link_empty_pages_and_persistence(setup):
    make, box, clock, settings = setup
    next_url = f"{BASE}/rooms?cursor=next"

    def respond(req):
        if req.url.path == "/v1/rooms":
            return (
                page([{"id": "S"}])
                if "cursor" in req.url.params
                else page([], next_url)
            )
        return page([])

    poller, calls = make(
        respond,
        replace(settings, request_budget_incremental=1, request_budget_overlap=1),
    )
    poller.tick()
    assert poller.state.rooms_url == next_url
    clock.now += timedelta(minutes=5)
    restarted, calls = make(respond, settings)
    restarted.tick()
    assert "S" in restarted.state.spaces
    assert calls[0].url.params["cursor"] == "next"


def test_catchup_after_off_longer_than_overlap_and_same_timestamp_ids(setup):
    make, box, clock, settings = setup
    phase = 0

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        if phase == 0:
            return page([message("a")])
        return page(
            [
                message("new", days=-19),
                message("offline", days=-5),
                message("z"),
                message("a"),
                message("old", days=2),
            ]
        )

    poller, _ = make(respond)
    poller.tick()
    phase = 1
    clock.now += timedelta(days=20)
    restarted, _ = make(respond)
    restarted.tick()
    observed = {m.message_id for b in bundles(box) for m in b.messages}
    assert {"a", "z", "offline", "new"} <= observed
    assert "old" not in observed
    assert restarted.state.spaces["S"].high_water[1] == "new"


def test_multiple_days_atomic_capacity_failure_rolls_back_bodies_and_highwater(setup):
    make, box, clock, settings = setup
    box.limits = OutboxLimits(max_items=1, min_free_bytes=1)

    def respond(req):
        return (
            page([{"id": "S"}])
            if req.url.path == "/v1/rooms"
            else page([message("one", days=1), message("two", days=2)])
        )

    poller, _ = make(respond)
    with pytest.raises(CapacityError):
        poller.tick()
    assert list(box.entries()) == []
    assert poller.state.spaces["S"].high_water is None
    assert poller.state.spaces["S"].incremental.url.endswith("max=100")
    box.limits = OutboxLimits(min_free_bytes=1)
    poller.tick()
    assert len(bundles(box)) == 2
    assert poller.state.spaces["S"].high_water is not None


@pytest.mark.parametrize("status", [429, 500, 503])
def test_backoff_persisted_across_reboot_and_counts_retries(setup, status):
    make, box, clock, settings = setup

    def respond(req):
        return httpx.Response(status, headers={"Retry-After": "120"})

    poller, calls = make(respond)
    poller.tick()
    assert len(calls) == 1
    assert poller.state.retry_at > NOW
    restarted, calls = make(respond)
    restarted.tick()
    assert calls == []
    clock.now = restarted.state.retry_at + timedelta(seconds=1)
    restarted.tick()
    assert len(calls) == 1
    assert sum(restarted.state.used.values()) == 2


def test_retry_after_http_date(setup):
    make, _, _, _ = setup
    poller, calls = make(
        lambda req: httpx.Response(
            429, headers={"Retry-After": "Fri, 20 Nov 2026 12:10:00 GMT"}
        )
    )
    poller.tick()
    assert poller.state.retry_at >= NOW + timedelta(minutes=10)
    assert len(calls) == 1


def test_timeout_does_not_advance_source_cursor(setup):
    make, _, _, _ = setup

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        raise httpx.ReadTimeout("private URL must not escape", request=req)

    poller, _ = make(respond)
    poller.tick()
    assert poller.state.spaces["S"].high_water is None
    assert poller.state.retry_at > NOW


def test_401_refresh_once_and_every_request_uses_budget(setup):
    make, _, _, _ = setup
    tokens = Tokens()
    poller, calls = make(lambda req: httpx.Response(401), tokens=tokens)
    with pytest.raises(PollError, match="reauthentication_required"):
        poller.tick()
    assert len(calls) == 2
    assert tokens.refreshes == [False, True]
    assert poller.state.used["incremental"] == 2


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.invalid/private",
        f"{BASE}/messages?roomId=EXCLUDED",
        "http://webexapis.com/v1/messages?roomId=S",
    ],
)
def test_unsafe_message_links_never_receive_tokens_or_advance(setup, next_url):
    make, box, _, _ = setup

    def respond(req):
        return (
            page([{"id": "S"}])
            if req.url.path == "/v1/rooms"
            else page([message()], next_url)
        )

    poller, _ = make(respond)
    with pytest.raises(PollError, match="unsafe_pagination"):
        poller.tick()
    assert bundles(box) == []
    assert poller.state.spaces["S"].high_water is None


def test_cycle_link_is_rejected(setup):
    make, _, _, _ = setup

    def respond(req):
        return (
            page([{"id": "S"}])
            if req.url.path == "/v1/rooms"
            else page([], str(req.url))
        )

    poller, _ = make(respond)
    with pytest.raises(PollError, match="pagination_cycle"):
        poller.tick()


@pytest.mark.parametrize("status", [403, 404])
def test_space_access_gap_isolated_not_deleted_or_advanced(setup, status):
    make, _, _, _ = setup

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}, {"id": "OTHER"}])
        return (
            httpx.Response(status)
            if req.url.params["roomId"] == "S"
            else page([message(space="OTHER")])
        )

    poller, _ = make(respond)
    poller.tick()
    assert poller.state.spaces["S"].status == "access_gap"
    assert poller.state.spaces["S"].high_water is None
    assert poller.state.spaces["OTHER"].high_water is not None
    assert poller.state.spaces["S"].candidates == {}


def test_round_robin_separate_overlap_and_account_budget(setup):
    make, _, clock, settings = setup

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": sid} for sid in ("A", "B", "C")])
        return page([])

    settings = replace(
        settings,
        request_budget_total=5,
        request_budget_incremental=2,
        request_budget_overlap=2,
        request_budget_deletion=1,
    )
    poller, calls = make(respond, settings)
    poller.tick()
    assert len(calls) == 4
    assert poller.state.used == {"incremental": 2, "overlap": 2}
    assert poller.state.spaces["A"].incremental_at is not None
    assert poller.state.spaces["B"].incremental_at is None
    clock.now += timedelta(minutes=5)
    restarted, _ = make(respond, settings)
    restarted.tick()
    assert restarted.state.spaces["B"].incremental_at is not None
    assert restarted.state.spaces["C"].overlap_at is not None


def test_overlap_changed_message_preserves_original_and_missing_stays_unverified(setup):
    make, box, clock, settings = setup
    phase = 0

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        if req.url.path.startswith("/v1/messages/"):
            return httpx.Response(404)
        if phase == 0:
            return page([message("a"), message("missing", days=2)])
        return page([message("a", text="changed", updated=NOW.isoformat())])

    poller, _ = make(respond)
    poller.tick()
    phase = 1
    clock.now += timedelta(minutes=5)
    restarted, calls = make(respond)
    restarted.tick()
    versions = [m.text for b in bundles(box) for m in b.messages if m.message_id == "a"]
    assert sorted(versions) == ["changed", "synthetic"]
    candidate = restarted.state.spaces["S"].candidates["missing"]
    assert candidate.result == "unverified"
    assert candidate.checked_at == clock.now
    assert any(r.url.path == "/v1/messages/missing" for r in calls)


def test_default_budget_starts_poller(setup):
    make, _, _, _ = setup
    poller, calls = make(lambda req: page([]), WebexSettings())
    poller.tick()
    assert len(calls) == 1
    assert poller.state.used == {"incremental": 1}


def test_small_budget_does_not_starve_incremental_behind_discovery(setup):
    make, _, clock, settings = setup
    settings = replace(settings, request_budget_incremental=1)

    def respond(req):
        return page([{"id": "S"}]) if req.url.path == "/v1/rooms" else page([])

    poller, _ = make(respond, settings)
    poller.tick()
    assert poller.state.spaces["S"].incremental_at is None
    clock.now += timedelta(minutes=5)
    poller, _ = make(respond, settings)
    poller.tick()
    assert poller.state.spaces["S"].incremental_at is not None


def test_refresh_intent_survives_budget_exhaustion(setup):
    make, _, clock, settings = setup
    settings = replace(settings, request_budget_incremental=1)
    tokens = Tokens()
    poller, calls = make(lambda req: httpx.Response(401), settings, tokens)
    poller.tick()
    assert len(calls) == 1
    clock.now += timedelta(minutes=5)
    restarted, calls = make(lambda req: httpx.Response(401), settings, tokens)
    with pytest.raises(PollError, match="reauthentication_required"):
        restarted.tick()
    assert tokens.refreshes == [False, True]


def test_exclude_stops_all_message_work_and_reinclude_restarts_backfill(setup):
    make, _, clock, settings = setup

    def respond(req):
        return page([{"id": "S"}]) if req.url.path == "/v1/rooms" else page([message()])

    poller, _ = make(respond)
    poller.tick()
    clock.now += timedelta(minutes=5)
    excluded, calls = make(respond, replace(settings, exclude_space_ids=("S",)))
    excluded.tick()
    assert all(r.url.path == "/v1/rooms" for r in calls)
    clock.now += timedelta(days=20)
    included, _ = make(respond)
    included.tick()
    # Old original is outside the newly enabled seven-day window.
    assert included.state.spaces["S"].high_water is None


def test_invalid_page_does_not_commit_partial_content(setup):
    make, box, _, _ = setup

    def respond(req):
        return (
            page([{"id": "S"}])
            if req.url.path == "/v1/rooms"
            else page([message(), {"secret": "invalid raw body"}])
        )

    poller, _ = make(respond)
    with pytest.raises(PollError, match="invalid_webex_message"):
        poller.tick()
    assert bundles(box) == []
    assert poller.state.spaces["S"].high_water is None


def test_deletion_verification_deferred_without_same_run_budget(setup):
    make, _, clock, settings = setup
    phase = 0

    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        if req.url.path.startswith("/v1/messages/"):
            pytest.fail("deletion budget is exhausted")
        return (
            page([message("a"), message("missing", days=2)])
            if phase == 0
            else page([message("a")])
        )

    settings = replace(
        settings,
        request_budget_total=4,
        request_budget_incremental=2,
        request_budget_overlap=1,
        request_budget_deletion=1,
    )
    poller, _ = make(respond, settings)
    poller.tick()
    phase = 1
    clock.now += timedelta(minutes=5)
    restarted, _ = make(respond, settings)
    assert restarted._reserve("deletion")
    restarted.tick()
    assert restarted.state.spaces["S"].candidates["missing"].checked_at is None


def test_day_buffers_and_cursor_rollback_on_sqlite_commit_failure(setup, monkeypatch):
    from contextlib import contextmanager

    make, box, _, _ = setup

    def respond(req):
        return page([{"id": "S"}]) if req.url.path == "/v1/rooms" else page([message()])

    poller, _ = make(respond)
    poller.discover()
    original = box._write

    @contextmanager
    def fail_commit():
        with original() as db:
            yield db
            # Fail only a transaction that actually adds source data.
            if db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]:
                raise OSError("simulated fsync failure")

    monkeypatch.setattr(box, "_write", fail_commit)
    with pytest.raises(OSError, match="fsync failure"):
        poller.tick()
    assert list(box.entries()) == []
    assert poller.state.spaces["S"].high_water is None


def test_cli_status_resume_and_once_use_fake_resources(setup, monkeypatch):
    from typer.testing import CliRunner

    from weaver import app
    from weaver.config import load_config
    from weaver.shepherd import cli as shepherd_cli
    from weaver.webex.cli import webex_cli
    from weaver.webex.poller import Space

    from .test_config import write_config

    make, box, _, settings = setup
    poller, _ = make(lambda req: page([]))
    state = State(
        spaces={
            "private-id": Space(
                status="access_gap", high_water=(NOW, "private-message")
            )
        }
    )
    box.buffer_page([], cursor=(CURSOR, state.model_dump_json()))
    config = load_config(write_config(box.path.parent), environ={})
    monkeypatch.setattr(
        app,
        "_load",
        lambda _: replace(config, shepherds=replace(config.shepherds, webex=settings)),
    )
    monkeypatch.setattr(shepherd_cli, "_outbox", lambda *args: (config, box))
    runner = CliRunner()
    status = runner.invoke(webex_cli, ["poll-status"])
    assert status.exit_code == 0
    assert '"access_gaps": 1' in status.stdout
    assert "private-id" not in status.stdout
    resumed = runner.invoke(webex_cli, ["poll-resume"])
    assert resumed.exit_code == 0
    saved = State.model_validate_json(box.cursor(CURSOR))
    assert saved.spaces["private-id"].status == "ready"
    assert saved.spaces["private-id"].high_water == (NOW, "private-message")

    # Exercise the actual --once command with fake OAuth and Messaging HTTP.
    from weaver.webex import oauth

    configured = replace(
        settings,
        enabled=True,
        credentials_file=box.path.parent / "fake-oauth.json",
        courier_token_file=box.path.parent / "fake-courier.token",
    )
    monkeypatch.setattr(
        app,
        "_load",
        lambda _: replace(
            config, shepherds=replace(config.shepherds, webex=configured)
        ),
    )
    monkeypatch.setattr(oauth, "OAuth", lambda _: Tokens())
    real_client = httpx.Client
    calls = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer fake-token"
        calls.append(request)
        return page([])

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = runner.invoke(webex_cli, ["poll", "--once"])
    assert result.exit_code == 0, result.output
    assert "round persisted" in result.stdout
    assert calls
