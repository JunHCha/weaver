"""Opt-in policy for backend transport/lifecycle unit tests.

These tests exercise accounting, cancellation and protocol handling. They ask
for ``transport_policy`` explicitly (usually through ``pytestmark``). It is a
real ``LlmPolicy`` over an allow-all resolver, never a Mock, so the shipped
prepare/check path still runs. Fail-closed defaults and real resolvers are
covered in test_llm_policy.py.
"""

import pytest

from weaver.llm import policy


class _AnyOrigin:
    def resolve(self, source_id: str) -> tuple[str, str | None]:
        return ("call", None)


class _TransportPolicy(policy.LlmPolicy):
    """Supplies the caller lineage that these tests deliberately omit."""

    def prepare(self, system: str, user: str) -> policy.PreparedInput:
        with policy.provenance_scope(["fixture"]):
            return super().prepare(system, user)


@pytest.fixture
def transport_policy(monkeypatch):
    stand_in = _TransportPolicy(
        _AnyOrigin(), shared_sources=lambda: (), webex_present=lambda: False
    )
    monkeypatch.setattr(policy, "default_policy", lambda: stand_in)
    # The factory imports the same function for constructing configured clients.
    monkeypatch.setattr("weaver.llm.default_policy", lambda: stand_in)
    return stand_in
