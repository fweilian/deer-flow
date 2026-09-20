"""Small MySQL driver-error classifiers used by persistence adapters."""

from __future__ import annotations

import re
from typing import Any

MYSQL_DUPLICATE_KEY = 1062
MYSQL_DEADLOCK = 1213
_KEY_NAME_RE = re.compile(r"for key ['`](?P<key>[^'`]+)['`]", re.IGNORECASE)


def _error_chain(error: Any):
    """Yield a wrapped driver error at most once, outermost first."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        yield candidate
        for attr in ("orig", "__cause__", "__context__"):
            nested = getattr(candidate, attr, None)
            if nested is not None:
                pending.append(nested)


def mysql_error_code(error: Any) -> int | None:
    """Return a MySQL error number from asyncmy/PyMySQL-style errors."""
    for candidate in _error_chain(error):
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
    for candidate in _error_chain(error):
        message = " ".join(str(part) for part in getattr(candidate, "args", ()) if isinstance(part, str)) or str(candidate)
        match = _KEY_NAME_RE.search(message)
        if match:
            return match.group("key")
    return None


def is_mysql_deadlock(error: Any) -> bool:
    """Whether *error* is MySQL's transaction-deadlock error (1213)."""
    return mysql_error_code(error) == MYSQL_DEADLOCK
