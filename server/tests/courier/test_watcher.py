import threading
import time

from weaver.courier import SCAN_INTERVAL_SECONDS

from .conftest import BODY, CAP, manifest_for, write_json


def test_default_rescan_interval_is_five_seconds():
    assert SCAN_INTERVAL_SECONDS == 5.0


def test_periodic_rescan_picks_up_late_body(courier, paths, captures):
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    stop = threading.Event()
    worker = threading.Thread(target=courier.run, args=(stop, 0.01))
    worker.start()
    try:
        time.sleep(0.05)
        assert captures.accepted == []
        (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
        deadline = time.monotonic() + 5
        while not captures.accepted and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert [c.manifest.capture_id for c in captures.accepted] == [CAP]


def test_scan_errors_do_not_stop_the_loop(courier, captures, caplog):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret content must not be logged")
        stop.set()

    stop = threading.Event()
    courier.scan_once = flaky
    courier.run(stop, 0)
    assert len(calls) == 2
    assert "secret" not in caplog.text
    assert "RuntimeError" in caplog.text
