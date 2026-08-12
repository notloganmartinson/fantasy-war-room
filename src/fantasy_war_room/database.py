from __future__ import annotations

import time
from collections.abc import Callable

import duckdb

from fantasy_war_room.errors import DatabaseBusyError

LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def is_database_lock_error(error: BaseException) -> bool:
    if not isinstance(error, (duckdb.IOException, duckdb.OperationalError)):
        return False
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "could not set lock",
            "conflicting lock",
            "database is locked",
            "file is already open",
        )
    )


def with_database_lock_retry[T](
    operation: Callable[[], T],
    *,
    sleep: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = LOCK_RETRY_DELAYS,
) -> T:
    for attempt, delay in enumerate((*delays, None), start=1):
        try:
            return operation()
        except Exception as error:
            if not is_database_lock_error(error):
                raise
            if delay is None:
                raise DatabaseBusyError({"attempt_count": attempt}) from error
            sleep(delay)
    raise AssertionError("database retry loop terminated unexpectedly")
