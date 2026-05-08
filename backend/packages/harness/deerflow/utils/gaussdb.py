"""GaussDB connection string helpers for async ORM usage."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote


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


def _normalize_host_port_values(host: str | None, port: str | None) -> tuple[object | None, object | None]:
    if host is None:
        return None, None

    host_items = [item for item in host.split(",") if item]
    port_items = [item for item in (port.split(",") if port else []) if item]

    if len(host_items) <= 1:
        return host_items[0], int(port_items[0]) if port_items else None

    normalized_ports: list[int | None] = []
    for idx in range(len(host_items)):
        if idx < len(port_items):
            normalized_ports.append(int(port_items[idx]))
        else:
            normalized_ports.append(None)

    return host_items, normalized_ports


def gaussdb_url_to_async_connect_kwargs(raw: str) -> dict[str, object]:
    """Convert a GaussDB URL into ``async_gaussdb.connect()`` keyword args."""
    if not raw:
        return {}

    userinfo, hosts, path_and_query = _split_gaussdb_url(raw)
    user, password = _split_userinfo(userinfo)
    host, port = _split_hosts(hosts)
    normalized_host, normalized_port = _normalize_host_port_values(host, port)
    dbname, _, query = path_and_query.partition("?")

    kwargs: dict[str, object] = {}
    if user:
        kwargs["user"] = user
    if password is not None:
        kwargs["password"] = password
    if normalized_host is not None:
        kwargs["host"] = normalized_host
    if normalized_port is not None:
        kwargs["port"] = normalized_port
    if dbname:
        kwargs["database"] = unquote(dbname)

    for key, value in parse_qsl(query, keep_blank_values=True):
        kwargs[key] = value

    return kwargs
