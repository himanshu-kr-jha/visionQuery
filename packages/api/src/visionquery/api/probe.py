"""The api's one look at the database: cached for a moment, and logging when its state changes."""

import logging
import threading
import time
from collections.abc import Callable

from visionquery.memory.health import ConnectionProblem, DatabaseHealth

# The probe is the api's one window onto the memory (with `serve`, which wires it), so the rest
# of the api takes these types from here and imports nothing from the memory itself.
__all__ = [
    "CACHE_SECONDS",
    "CHECKING",
    "STUCK_SECONDS",
    "ConnectionProblem",
    "DatabaseHealth",
    "DatabaseProbe",
]

#: How long a check's answer is reused. A flood of requests (or a healthcheck, a web app and a
#: monitor asking at once) then opens one connection a second, not one each.
CACHE_SECONDS = 1.0

#: How long a check may run before callers stop trusting the answer from before it: past this,
#: they are told the database timed out. Above the check's own bounds (see `check_database`).
STUCK_SECONDS = 5.0

#: What a caller gets while the very first check is still running.
CHECKING = DatabaseHealth("still checking")

_log = logging.getLogger(__name__)


class DatabaseProbe:
    """Callable like the check it wraps; see `CACHE_SECONDS`.

    One check runs at a time; a request that arrives meanwhile does not wait for it but gets the
    last answer (`CHECKING` before there is one, "timed out" once the check is stuck).

    Logs one WARNING when the database turns unreachable (naming the fixed problem category,
    never any part of the URL) and one INFO when it is reachable again, not one per check.
    """

    def __init__(
        self,
        check: Callable[[], DatabaseHealth],
        *,
        cache_seconds: float = CACHE_SECONDS,
        stuck_seconds: float = STUCK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._cache_seconds = cache_seconds
        self._stuck_seconds = stuck_seconds
        self._clock = clock
        # Held across a check, so one request at a time opens a connection. Others never wait
        # for it: a check with no upper bound would otherwise hold every request's thread.
        self._lock = threading.Lock()
        self._last: DatabaseHealth | None = None
        self._checked_at = 0.0
        #: When the running check began; `None` while none is running.
        self._started_at: float | None = None

    def __call__(self) -> DatabaseHealth:
        if not self._lock.acquire(blocking=False):
            return self._while_checking()
        try:
            now = self._clock()
            if self._last is not None and now - self._checked_at < self._cache_seconds:
                return self._last
            previous = self._last
            self._started_at = now
            try:
                current = self._check()
            finally:
                self._started_at = None
            self._last, self._checked_at = current, self._clock()
            self._log_change(previous, current)
            return current
        finally:
            self._lock.release()

    def _while_checking(self) -> DatabaseHealth:
        """What a caller gets while another request's check is running."""
        started = self._started_at
        if started is not None and self._clock() - started >= self._stuck_seconds:
            # The check has outlasted every bound it has: the old answer is no longer news.
            return DatabaseHealth("timed out")
        return self._last if self._last is not None else CHECKING

    @staticmethod
    def _log_change(previous: DatabaseHealth | None, current: DatabaseHealth) -> None:
        if not current.connected:
            if previous is None or previous.connected:
                _log.warning("the database is unreachable: %s", current.problem)
        elif previous is not None and not previous.connected:
            _log.info("the database is reachable again")
