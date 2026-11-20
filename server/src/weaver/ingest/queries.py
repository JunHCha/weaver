"""Store-query guard shared by the file and HTTP verdict adapters."""

import logging
import re
from collections.abc import Callable

from .errors import CourierFileError

logger = logging.getLogger("weaver.courier")
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


def ask[**P, T](query: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run an injected store query; its failure is a per-input, retryable code."""
    try:
        return query(*args, **kwargs)
    except Exception as exc:
        # Only validated opaque IDs/revisions are supplied by these query ports.
        safe_args = tuple(
            a
            for a in args
            if isinstance(a, int) or (isinstance(a, str) and SAFE_ID.fullmatch(a))
        )
        logger.warning("courier query failed: %s %s", type(exc).__name__, safe_args)
        raise CourierFileError("store_failed") from None
