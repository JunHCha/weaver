"""Stable, redacted errors; provider response bodies must never escape here."""


class LlmError(Exception):
    code = "backend_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class InvalidJson(LlmError):
    code = "invalid_json"


class SchemaMismatch(LlmError):
    code = "schema_mismatch"


class MissingOutput(LlmError):
    code = "missing_output"


class Refusal(LlmError):
    code = "refusal"


class AuthenticationError(LlmError):
    code = "authentication_error"


class RateLimited(LlmError):
    code = "rate_limited"


class TransientError(LlmError):
    code = "transient_error"


class LlmTimeout(LlmError):
    code = "timeout"


class UnsupportedBackend(LlmError):
    code = "unsupported_backend"


class UnsupportedStructuredOutput(LlmError):
    code = "unsupported_structured_output"


class BudgetExceeded(LlmError):
    code = "budget_exceeded"
