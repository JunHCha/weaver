"""Server-only boundary. Never export this as a Courier file or phone schema.

ArchivedSource keeps the local filesystem path outside the Source JSON.
"""

from pathlib import Path

from .models import Contract, Source


class ArchivedSource(Contract):
    source: Source
    payload_path: Path
