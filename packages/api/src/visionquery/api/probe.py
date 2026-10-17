"""The api's one look at the database: run aside, cached for a moment, given up on at a deadline,
and logging when its state changes."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from visionquery.memory.health import HEALTH_BUDGET_SECONDS, ConnectionProblem, DatabaseHealth

# The probe is the api's one window onto the memory (with `serve`, which wires it), so the rest
# of the api takes these types from here and imports nothing from the memory itself.
__all__ = [
    "CACHE_SECONDS",
    "CHECKING",
    "DEADLINE_MARGIN_SECONDS",
    "DEADLINE_SECONDS",
    "Checking",
    "ConnectionProblem",
    "DatabaseHealth",
    "DatabaseProbe",
    "DatabaseState",
]

#: How long a check's answer is reused. A flood of requests (or a healthcheck, a web app and a
#: monitor asking at once) then opens one connection a second, not one each.
CACHE_SECONDS = 1.0

#: How much longer than the check's own budget the probe waits for it.
DEADLINE_MARGIN_SECONDS = 1.0

#: How long a check may run before the probe stops waiting for it and says the database timed
#: out. The check bounds itself (`HEALTH_BUDGET_SECONDS`) except while it resolves a host name
#: or tries several hosts; this bounds those too. Every client of `/health` must wait longer.
DEADLINE_SECONDS = HEALTH_BUDGET_SECONDS + DEADLINE_MARGIN_SECONDS


@dataclass(frozen=True)
class Checking:
    """Nothing is known about the database yet: the first check has not finished."""


#: What a caller gets while the very first check is still running and its deadline has not passed.
CHECKING = Checking()

#: What the probe says about the database: what the last check found, or that none has finished.
type DatabaseState = DatabaseHealth | Checking

_TIMED_OUT = DatabaseHealth("timed out")
#: What a check that raised found; `check_database` never raises, so this would be a bug.
_FAILED = DatabaseHealth("other connection failure")

_log = logging.getLogger(__name__)


@dataclass
class _Run:
    """One check, running in a worker thread of its own."""

    started_at: float
    done: threading.Event = field(default_factory=threading.Event)
    result: DatabaseHealth | None = None
    #: Whether its deadline was seen to pass (and logged) while it ran.
    overdue: bool = False


class DatabaseProbe:
    """Callable like the check it wraps; see `CACHE_SECONDS` and `DEADLINE_SECONDS`.

    One check runs at a time, in a worker thread, so no request is held by it for longer than
    the deadline. The request that starts a check waits for it until then; a request that
    arrives meanwhile does not wait but gets the last answer (`CHECKING` before there is one).
    Once the deadline passes, every request is told the database timed out, at once, until the
    check returns: a thread cannot be stopped, so a stuck check is never joined by another.

    Logs one WARNING when the database turns unreachable (naming the fixed problem category,
    never any part of the URL), one when a check outlives its deadline, and one INFO when the
    database is reachable again, not one per check.
    """

    def __init__(
        self,
        check: Callable[[], DatabaseHealth],
        *,
        cache_seconds: float = CACHE_SECONDS,
        deadline_seconds: float = DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._cache_seconds = cache_seconds
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        # Guards the fields below, and is never held across a check.
        self._lock = threading.Lock()
        self._last: DatabaseHealth | None = None
        self._checked_at = 0.0
        #: The check running now; `None` while none is.
        self._run: _Run | None = None

    def __call__(self) -> DatabaseState:
        with self._lock:
            run = self._run
            if run is None:
                if (
                    self._last is not None
                    and self._clock() - self._checked_at < self._cache_seconds
                ):
                    return self._last
                run = self._run = _Run(self._clock())
                threading.Thread(
                    target=self._work, args=(run,), name="database-check", daemon=True
                ).start()
                started_here = True
            else:
                started_here = False
        if started_here:
            remaining = run.started_at + self._deadline_seconds - self._clock()
            if run.done.wait(max(remaining, 0.0)) and run.result is not None:
                return run.result
            return self._overdue(run)
        return self._meanwhile(run)

    def _meanwhile(self, run: _Run) -> DatabaseState:
        """What a caller gets while another request's check is running."""
        if run.done.is_set() and run.result is not None:
            return run.result
        if self._clock() - run.started_at >= self._deadline_seconds:
            return self._overdue(run)
        with self._lock:
            return self._last if self._last is not None else CHECKING

    def _overdue(self, run: _Run) -> DatabaseHealth:
        """The answer once `run` has outlived its deadline, said in the log the first time."""
        with self._lock:
            if run.done.is_set() and run.result is not None:
                return run.result  # it returned after all, just now
            if not run.overdue:
                run.overdue = True
                # Recorded as the last answer, so the check's return is logged as a change.
                self._last = _TIMED_OUT
                _log.warning(
                    "the database check has not returned within %g s", self._deadline_seconds
                )
        return _TIMED_OUT

    def _work(self, run: _Run) -> None:
        """Run the check and record what it found; the body of `run`'s worker thread."""
        try:
            current = self._check()
        # Only the class: an exception's text may quote the URL.
        except Exception as error:
            _log.error("the database check raised %s", type(error).__name__)
            current = _FAILED
        with self._lock:
            previous, self._last, self._checked_at = self._last, current, self._clock()
            run.result = current
            self._run = None
            run.done.set()
        self._log_change(previous, current)

    @staticmethod
    def _log_change(previous: DatabaseHealth | None, current: DatabaseHealth) -> None:
        if not current.connected:
            if previous is None or previous.connected:
                _log.warning("the database is unreachable: %s", current.problem)
        elif previous is not None and not previous.connected:
            _log.info("the database is reachable again")
