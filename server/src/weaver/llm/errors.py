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


class InsufficientCredit(LlmError):
    """Provider rejected the request because the account has no credit left.

    Retrying cannot help until the account is topped up; never billed.
    """

    code = "insufficient_credit"


class UsageLimited(LlmError):
    """Subscription allowance or the local daily call cap is exhausted.

    Retrying cannot help until the window resets; the runtime waits hourly.
    """

    code = "usage_limited"


class UnbilledRejection(LlmError):
    """4xx the provider returned before processing, so nothing was billed.

    Keeps the generic ``backend_error`` code; only the ledger tells it apart.
    """


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
