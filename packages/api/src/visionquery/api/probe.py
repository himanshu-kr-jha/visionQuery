"""The api's one look at the database: cached for a moment, and logging when its state changes."""

import logging
import threading
import time
from collections.abc import Callable

from visionquery.memory.health import DatabaseHealth

__all__ = ["CACHE_SECONDS", "DatabaseProbe"]

#: How long a check's answer is reused. A flood of requests (or a healthcheck, a web app and a
#: monitor asking at once) then opens one connection a second, not one each.
CACHE_SECONDS = 1.0

_log = logging.getLogger(__name__)


class DatabaseProbe:
    """Callable like the check it wraps; see `CACHE_SECONDS`.

    Logs one WARNING when the database turns unreachable (naming the fixed problem category,
    never any part of the URL) and one INFO when it is reachable again, not one per check.
    """

    def __init__(
        self,
        check: Callable[[], DatabaseHealth],
        *,
        cache_seconds: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._cache_seconds = cache_seconds
        self._clock = clock
        # Held across the check, so concurrent requests wait for its answer instead of each
        # opening a connection.
        self._lock = threading.Lock()
        self._last: DatabaseHealth | None = None
        self._checked_at = 0.0

    def __call__(self) -> DatabaseHealth:
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._checked_at < self._cache_seconds:
                return self._last
            previous = self._last
            current = self._check()
            self._last, self._checked_at = current, self._clock()
            self._log_change(previous, current)
            return current

    @staticmethod
    def _log_change(previous: DatabaseHealth | None, current: DatabaseHealth) -> None:
        if not current.connected:
            if previous is None or previous.connected:
                _log.warning("the database is unreachable: %s", current.problem)
        elif previous is not None and not previous.connected:
            _log.info("the database is reachable again")
