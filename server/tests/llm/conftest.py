"""Synthetic limit messages, with provenance recorded in the fixture."""

import json
from pathlib import Path

import pytest

_LIMITS = json.loads(
    (Path(__file__).parent / "fixtures" / "subscription_limits.json").read_text()
)


@pytest.fixture(params=_LIMITS, ids=lambda case: case["id"])
def cli_usage_message(request):
    return request.param["text"]
