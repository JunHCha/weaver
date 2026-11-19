"""Fixed error shape: ``{code, retryable, operation_id?}``; never text or paths."""


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        *,
        retryable: bool = False,
        operation_id: str | None = None,
        retry_after: int | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.operation_id = operation_id
        self.retry_after = retry_after
        self.extra = extra or {}

    def body(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "retryable": self.retryable}
        if self.operation_id is not None:
            result["operation_id"] = self.operation_id
        return result | self.extra
