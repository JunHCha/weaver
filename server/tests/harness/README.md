# Synthetic server worlds

Specifications exercise production runtime assembly, archive, codecs, review
journals, Fabric, Wiki (including private Git), and mobile ASGI routing. Existing
tests remain intact. Import `world_factory` from `server.tests.harness.fixtures`
in a suite's `conftest.py` to opt into isolation; it does not change legacy suites.

```python
world = world_factory(transport="files")  # or "http"
capture = world.capture()
world.step()
receipt = world.receipt(capture)
review = world.read_review()
operation_id = world.approve(review)  # HTTP: durable queued operation ID
world.restart()
world.run_until(lambda: bool(world.read_threads()))
```

`step()` runs one complete resident-loop pass without sleeping. `advance(seconds)`
moves its scheduling clock, and `run_until(predicate, passes=5)` bounds progress by
an explicit observable outcome. The loop wait hook is confined to the harness.
`restart()` reconstructs both runtime and HTTP objects with the same config/data,
provider sink, and device credentials. It simulates ordinary restart, not SIGKILL
or power loss. Capture IDs are deterministic within a world, survive its restart,
and production IDs remain production generated.

Use `read_threads()`, `read_page(relative_wiki_path)`, `receipt(manifest)`, and
`operation(id)` as observation ports. Expected content in specifications is
literal domain data, not generated through production projections. Raw archive
bytes are accessible through the production archive locator.

`llm_failure(task, error)` injects provider failures after the real archive-backed
policy prepares and checks input. `receipt_io_failure(monkeypatch)` makes shared
Receipt publication raise `OSError`; HTTP capture should not call that boundary.
Responses in `SyntheticLlm` are explicit schema-validated fixtures, never repaired
by the older area-completion helper. Unexpected model tasks fail the test.

The factory uses temporary HOME/XDG/config/vault/archive/state roots, removes
provider/Weaver environment settings, blocks socket connections and real provider
construction, and permits only local Git subprocesses inside the test
root. All HTTP requests use `TestClient` and fake peer identity with production
authentication/token storage. Real stores and fsync remain enabled. This isolation
is scoped to suites opting into the fixture, not a sandbox for arbitrary code.
The runtime scheduling clock and source timestamp are controlled; production
approval timestamps, Git metadata, and other component clocks still use real time.
Date-boundary specifications should inject the relevant component clock explicitly.
