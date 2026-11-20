"""Temporary source-channel barrier until B2's provenance gate is wired."""


class WebexLlmBlocked(ValueError):
    """No Webex content may leave through an LLM adapter before B2."""


def require_webex_gate(channel: str) -> None:
    # B2 merge must replace this guard with the central provenance policy gate.
    if channel == "webex":
        raise WebexLlmBlocked("webex_policy_gate_not_wired")
