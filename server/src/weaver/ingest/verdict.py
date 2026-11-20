"""Transport-independent Verdict application, confirmation and bounded retries."""

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from weaver.config import CourierSettings
from weaver.failures import failure_reason
from weaver.llm.policy import PolicyBlocked
from weaver.schema import Verdict

from .errors import CourierFileError
from .ports import DecisionStore, PackageRevisions
from .queries import ask as _ask

# Preserve existing safe diagnostics across both transport adapters.
logger = logging.getLogger("weaver.courier")
EXTERNAL_PREFIX = "external:"


@dataclass
class ApplyFailure:
    verdict_digest: str
    error_digest: str
    attempts: int
    next_attempt: float


class VerdictIngest:
    """Shared Verdict policy over the existing durable decision journal.

    This service owns no transport or additional journal. Retry state is volatile:
    restarting after a code/config fix retries existing decisions, while the
    injected decision store remains the authority for committed applications.
    """

    def __init__(
        self,
        revisions: PackageRevisions,
        decisions: DecisionStore,
        *,
        retry: CourierSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        can_apply: Callable[[], bool] | None = None,
        on_apply_stopped: Callable[[Verdict], None] | None = None,
    ) -> None:
        self._revisions = revisions
        self._decisions = decisions
        self._retry = retry or CourierSettings()
        self._clock = clock
        self._can_apply = can_apply
        self._on_apply_stopped = on_apply_stopped
        self._apply_failures: dict[str, ApplyFailure] = {}

    def _validate(self, verdict: Verdict) -> None:
        """Loom's optional guard against a conflicting resynced decision."""
        validate = getattr(self._decisions, "validate_verdict", None)
        if validate is None:
            return
        try:
            validate(verdict)
        except CourierFileError:
            raise
        except Exception as exc:
            logger.warning(
                "courier recovery failed: %s reason=%s (%s, %s)",
                type(exc).__name__,
                failure_reason(exc),
                verdict.package_id,
                verdict.revision,
            )
            raise CourierFileError("store_failed") from None

    def _apply_checked(self, name: str, verdict: Verdict) -> str:
        """Apply one validated verdict through the shared decision journal.

        Returns ``already`` when this revision was applied before, ``applied``
        after a confirmed application; otherwise raises a ``CourierFileError``
        code (stale_rev, apply_backoff, apply_stopped, ...). File scans and the
        phone HTTP API both come through here, so one revision is applied once.
        """
        pid, rev = verdict.package_id, verdict.revision
        if _ask(self._decisions.is_applied, pid, rev):
            return "already"
        latest = _ask(self._revisions.latest_rev, pid)
        if latest is None:
            raise CourierFileError("unknown_package")
        if rev < latest:
            raise CourierFileError("stale_rev")
        if rev > latest:
            raise CourierFileError("unknown_rev")
        if self._can_apply is not None and not self._can_apply():
            raise CourierFileError("apply_waiting")
        digest = hashlib.sha256(verdict.model_dump_json().encode()).hexdigest()
        failure = self._apply_failures.get(name)
        if failure is not None and failure.verdict_digest != digest:
            self._apply_failures.pop(name)
            failure = None
        if failure is not None:
            if failure.attempts >= self._retry.max_attempts:
                if self._on_apply_stopped is not None:
                    self._on_apply_stopped(verdict)
                raise CourierFileError("apply_stopped")
            if self._clock() < failure.next_attempt:
                raise CourierFileError("apply_backoff")
        try:
            self._decisions.apply(verdict)
            if not _ask(self._decisions.is_applied, pid, rev):
                raise CourierFileError("apply_unconfirmed")
        except PolicyBlocked:
            # A closed gate is a hold, not a failure: it must not consume the
            # retry budget and freeze the verdict as apply_stopped. The decision
            # stays queued and is applied once the gate opens or is resumed.
            logger.warning("courier apply waiting: policy_blocked (%s, %s)", pid, rev)
            raise CourierFileError("apply_waiting") from None
        except Exception as exc:
            logger.warning(
                "courier apply failed: %s reason=%s (%s, %s)",
                type(exc).__name__,
                failure_reason(exc),
                pid,
                rev,
            )
            self._record_apply_failure(name, digest, exc)
            stopped = self._apply_failures[name].attempts >= self._retry.max_attempts
            if stopped and self._on_apply_stopped is not None:
                self._on_apply_stopped(verdict)
            code = (
                "apply_stopped"
                if stopped
                else exc.code
                if isinstance(exc, CourierFileError)
                else "apply_failed"
            )
            raise CourierFileError(code) from None
        self._apply_failures.pop(name, None)
        return "applied"

    def forget_external_failures(self) -> None:
        """Retry stopped external verdicts, as a fresh process would."""
        self._apply_failures = {
            name: failure
            for name, failure in self._apply_failures.items()
            if not name.startswith(EXTERNAL_PREFIX)
        }

    def apply_external(self, key: str, verdict: Verdict) -> str:
        """Apply a verdict that arrived over another transport (phone HTTP API).

        ``key`` identifies the journal operation for retry/backoff bookkeeping.
        """
        return self.apply(EXTERNAL_PREFIX + key, verdict)

    def _record_apply_failure(
        self, name: str, verdict_digest: str, exc: Exception
    ) -> None:
        error_digest = hashlib.sha256(
            f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}".encode()
        ).hexdigest()
        previous = self._apply_failures.get(name)
        attempts = (
            previous.attempts + 1
            if previous is not None and previous.error_digest == error_digest
            else 1
        )
        # Iterative multiplication avoids huge powers for user-supplied limits.
        delay = self._retry.backoff_seconds
        for _ in range(attempts - 1):
            delay = min(delay * 2, self._retry.max_backoff_seconds)
            if delay == self._retry.max_backoff_seconds:
                break
        self._apply_failures[name] = ApplyFailure(
            verdict_digest, error_digest, attempts, self._clock() + delay
        )
        if attempts == self._retry.max_attempts:
            logger.warning(
                "courier apply retries stopped: %s after %s attempts; "
                "restart or submit a new verdict to retry",
                name,
                attempts,
            )

    def apply(self, key: str, verdict: Verdict) -> str:
        """Apply a parsed verdict using its transport-specific retry key."""
        self._validate(verdict)
        return self._apply_checked(key, verdict)

    def forget(self, key: str) -> None:
        """Forget retry state after the transport has cleaned up its input."""
        self._apply_failures.pop(key, None)

    def retain_files(self, present: set[str]) -> None:
        """Discard retries for removed files, preserving HTTP operation retries."""
        self._apply_failures = {
            name: failure
            for name, failure in self._apply_failures.items()
            if name in present or name.startswith(EXTERNAL_PREFIX)
        }
