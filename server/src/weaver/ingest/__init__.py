"""Domain ingestion shared by transport adapters."""

from .errors import CourierFileError
from .verdict import VerdictIngest

__all__ = ["CourierFileError", "VerdictIngest"]
