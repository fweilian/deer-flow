"""GaussDB connection string helpers."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote


def _quote_conninfo_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _split_gaussdb_url(raw: str) -> tuple[str, str, str]:
    """Split a GaussDB URL into userinfo, hosts, and database/query parts.

    This parser intentionally does not use ``urlsplit()`` because GaussDB URLs
    in the wild often include:
    - multi-host authorities like ``host1:port1,host2:port2``
    - unescaped ``#`` or ``@`` characters inside passwords
    """
    scheme_sep = "://"
    if scheme_sep not in raw:
        raise ValueError(f"Invalid GaussDB URL: {raw!r}")

    _, remainder = raw.split(scheme_sep, 1)
    authority, sep, path_and_query = remainder.partition("/")
    if not sep:
        authority = remainder
        path_and_query = ""

    userinfo, at, hosts = authority.rpartition("@")
    if not at:
        userinfo = ""
        hosts = authority

    return userinfo, hosts, path_and_query


def _split_userinfo(userinfo: str) -> tuple[str | None, str | None]:
    if not userinfo:
        return None, None
    user, sep, password = userinfo.partition(":")
    if not sep:
        return unquote(user), None
    return unquote(user), unquote(password)


def _split_hosts(hosts: str) -> tuple[str | None, str | None]:
    if not hosts:
        return None, None

    host_values: list[str] = []
    port_values: list[str] = []
    for node in hosts.split(","):
        node = node.strip()
        if not node:
            continue
        host, sep, port = node.rpartition(":")
        if sep:
            host_values.append(host)
            port_values.append(port)
        else:
            host_values.append(node)

    host_value = ",".join(host_values) if host_values else None
    port_value = ",".join(port_values) if port_values else None
    return host_value, port_value


def gaussdb_url_to_conninfo(raw: str) -> str:
    """Convert a GaussDB URL into libpq/psycopg-style conninfo."""
    if not raw:
        return raw

    if "://" not in raw and "=" in raw:
        return raw

    scheme = raw.split("://", 1)[0]
    if scheme not in {"gaussdb", "gaussdb+async_gaussdb"}:
        return raw

    userinfo, hosts, path_and_query = _split_gaussdb_url(raw)
    user, password = _split_userinfo(userinfo)
    host, port = _split_hosts(hosts)
    dbname, _, query = path_and_query.partition("?")

    parts: list[str] = []

    if host:
        parts.append(f"host={_quote_conninfo_value(host)}")
    if port:
        parts.append(f"port={_quote_conninfo_value(port)}")
    if user:
        parts.append(f"user={_quote_conninfo_value(user)}")
    if password is not None:
        parts.append(f"password={_quote_conninfo_value(password)}")

    if dbname:
        parts.append(f"dbname={_quote_conninfo_value(unquote(dbname))}")

    for key, value in parse_qsl(query, keep_blank_values=True):
        parts.append(f"{key}={_quote_conninfo_value(value)}")

    return " ".join(parts)
