"""Opt-in policy stand-in for backend transport/lifecycle unit tests.

These tests exercise accounting, cancellation and protocol handling. Real
policy behavior and fail-closed defaults are covered in test_llm_policy.py.
"""

from unittest.mock import Mock

import pytest

from weaver.llm import policy


@pytest.fixture
def transport_policy(monkeypatch):
    stand_in = Mock(spec=policy.LlmPolicy)
    stand_in.backlog = None
    stand_in.prepare.side_effect = lambda system, user: policy.PreparedInput(
        system, user, policy.Provenance(frozenset({"fixture"}))
    )
    monkeypatch.setattr(policy, "default_policy", lambda: stand_in)
    # The factory imports the same function for constructing configured clients.
    monkeypatch.setattr("weaver.llm.default_policy", lambda: stand_in)
    return stand_in
