"""Safe application error codes, also used by the legacy file adapter."""


class CourierFileError(Exception):
    """An input cannot be processed now; only ``code`` is safe to log.

    The historical class name is retained for file/action adapter compatibility."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
