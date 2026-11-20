"""The transport suite uses an explicit policy double, never a live provider."""

import pytest


@pytest.fixture(autouse=True)
def isolated_transport_policy(transport_policy):
    return transport_policy
