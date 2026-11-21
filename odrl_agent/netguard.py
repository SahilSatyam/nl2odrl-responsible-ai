"""SSRF guard for user-supplied model endpoints."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

ALLOWED_HOSTS_ENV = "NL2ODRL_ALLOWED_LLM_HOSTS"
_BLOCKED_NAMES = {"metadata.google.internal", "metadata", "instance-data"}


class UnsafeBaseUrlError(ValueError):
    """The endpoint targets a link-local, metadata or non-allowlisted host."""


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_link_local or addr.is_unspecified or addr.is_multicast or addr.is_reserved


def validate_base_url(url: str) -> str:
    """Return ``url`` (trailing slash stripped) if safe, else raise UnsafeBaseUrlError.

    Rejects non-http(s) schemes, credentials in the URL, cloud metadata names,
    and any host that is or resolves to a link-local, unspecified, multicast or
    reserved address (the cloud metadata range). If ``NL2ODRL_ALLOWED_LLM_HOSTS``
    is set (comma-separated), the host must also be listed. Loopback stays allowed
    for local model servers. Unresolvable names are not rejected here.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeBaseUrlError("base_url must start with http:// or https://")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeBaseUrlError("base_url has no host")
    if parts.username or parts.password:
        raise UnsafeBaseUrlError("base_url must not contain credentials")
    if host in _BLOCKED_NAMES or host.endswith(".internal"):
        raise UnsafeBaseUrlError(f"host {host!r} is not allowed")
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    allow = [h.strip().lower() for h in raw.split(",") if h.strip()]
    if allow and host not in allow:
        raise UnsafeBaseUrlError(f"host {host!r} is not in {ALLOWED_HOSTS_ENV}")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            infos = []
        addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    for addr in addrs:
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if _blocked(addr):
            raise UnsafeBaseUrlError(f"host {host!r} resolves to blocked address {addr}")
    return url.strip().rstrip("/")
