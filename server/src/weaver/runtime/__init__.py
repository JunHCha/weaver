"""Resident runtime: loop, single-instance lock, logs and LaunchAgent (#16).

Importing this package has no side effects; ``weaver.runtime.assemble`` wires
the real stores and is imported only by ``weaver.app run``.
"""

from .gate import Interrupted, LlmGate, LlmStatus
from .ledger import RuntimeLedger
from .loop import (
    EXIT_ALREADY_RUNNING,
    AlreadyRunning,
    InstanceLock,
    MaintenanceTask,
    Outcome,
    Runtime,
)

__all__ = [
    "EXIT_ALREADY_RUNNING",
    "AlreadyRunning",
    "InstanceLock",
    "Interrupted",
    "LlmGate",
    "LlmStatus",
    "MaintenanceTask",
    "Outcome",
    "Runtime",
    "RuntimeLedger",
]
