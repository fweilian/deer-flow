"""Small MySQL driver-error classifiers used by persistence adapters."""

from __future__ import annotations

import re
from typing import Any

MYSQL_DUPLICATE_KEY = 1062
MYSQL_DEADLOCK = 1213
_KEY_NAME_RE = re.compile(r"for key ['`](?P<key>[^'`]+)['`]", re.IGNORECASE)


def mysql_error_code(error: Any) -> int | None:
    """Return a MySQL error number from asyncmy/PyMySQL-style errors."""
    for candidate in (error, getattr(error, "__cause__", None)):
        if candidate is None:
            continue
        errno = getattr(candidate, "errno", None)
        if isinstance(errno, int):
            return errno
        args = getattr(candidate, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
    return None


def mysql_duplicate_key_name(error: Any) -> str | None:
    """Return the key name emitted by MySQL error 1062, if present."""
    if mysql_error_code(error) != MYSQL_DUPLICATE_KEY:
        return None
    message = " ".join(str(part) for part in getattr(error, "args", ()) if isinstance(part, str)) or str(error)
    match = _KEY_NAME_RE.search(message)
    return match.group("key") if match else None


def is_mysql_deadlock(error: Any) -> bool:
    """Whether *error* is MySQL's transaction-deadlock error (1213)."""
    return mysql_error_code(error) == MYSQL_DEADLOCK
