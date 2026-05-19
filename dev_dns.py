"""Dev-only DNS bypass for hosts blocked by local content-filtering resolvers.

Some content-blocking DNS resolvers (notably Mullvad VPN with content blocking
enabled — `2a07:b944::2:1`) return NXDOMAIN for `api.hubapi.com`. That breaks the
bridge whenever it tries to fetch HubSpot availability, even though the API is
perfectly legitimate and reachable.

This module is **self-detecting**: it probes whether the configured hosts
resolve through the system's resolver. If they do, the patch is a no-op
(production on Fly.io has unfiltered DNS — nothing changes). If a host fails to
resolve, it queries Cloudflare (`1.1.1.1`) directly via UDP and patches both
`socket.getaddrinfo` and `asyncio.BaseEventLoop.getaddrinfo` to substitute the
returned IP for that host. TLS SNI uses the original hostname, so the cert
verifies normally.

Call `apply_if_needed()` once at app startup, before any httpx client is built.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time

from logging_config import log


_PUBLIC_DNS = ("1.1.1.1", 53)
# Hosts to probe + bypass. Add more here if other domains start getting blocked
# in the dev environment (e.g. xAI, Make.com).
_TARGET_HOSTS: tuple[str, ...] = (
    "api.hubapi.com",
)

# State: maps blocked-host -> Cloudflare-resolved IP. Empty when no patch is
# active (production / unfiltered DNS).
_overrides: dict[str, str] = {}
_patched: bool = False


def _build_dns_query(name: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\x00"
    qtail = struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + qname + qtail


def _parse_first_a_record(resp: bytes) -> str | None:
    i = 12
    while resp[i] != 0:
        i += resp[i] + 1
    i += 1 + 4
    ancount = struct.unpack(">H", resp[6:8])[0]
    for _ in range(ancount):
        if resp[i] & 0xC0:
            i += 2
        else:
            while resp[i] != 0:
                i += resp[i] + 1
            i += 1
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", resp[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            return ".".join(str(b) for b in resp[i:i + 4])
        i += rdlen
    return None


def _resolve_via_cloudflare(name: str, timeout: float = 5.0) -> str:
    qid = int(time.time() * 1000) & 0xFFFF
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(_build_dns_query(name, qid), _PUBLIC_DNS)
        resp, _ = sock.recvfrom(512)
    finally:
        sock.close()
    if struct.unpack(">H", resp[:2])[0] != qid:
        raise RuntimeError("DNS transaction ID mismatch")
    ip = _parse_first_a_record(resp)
    if not ip:
        raise RuntimeError(f"No A record returned for {name} via 1.1.1.1")
    return ip


async def _system_resolves(host: str) -> bool:
    """Check if the host resolves through both the sync AND the async paths.

    httpx → anyio → asyncio uses the running loop's getaddrinfo, which on
    Windows can fail even when the sync `socket.getaddrinfo` succeeds (e.g.
    when a content-blocking resolver answers AAAA differently than A, or the
    async path uses a different system call). We must check both paths;
    otherwise the probe will say "fine" while httpx requests still fail.
    """
    try:
        socket.getaddrinfo(host, 443, family=socket.AF_INET)
    except OSError:
        return False
    try:
        loop = asyncio.get_running_loop()
        await loop.getaddrinfo(host, 443, family=socket.AF_INET)
    except OSError:
        return False
    return True


def _normalize(host: object) -> str:
    if isinstance(host, bytes):
        return host.decode("ascii", errors="ignore")
    return host if isinstance(host, str) else ""


def _install_patches() -> None:
    """Install both sync (socket) and async (event-loop) DNS overrides.

    Idempotent — safe to call once per process; guarded by `_patched`.
    """
    global _patched
    if _patched:
        return

    _orig_socket = socket.getaddrinfo

    def _patched_socket(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        ip = _overrides.get(_normalize(host))
        return _orig_socket(ip or host, *args, **kwargs)

    socket.getaddrinfo = _patched_socket  # type: ignore[assignment]

    _orig_loop = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def _patched_loop(self, host, port, **kwargs):  # type: ignore[no-untyped-def]
        ip = _overrides.get(_normalize(host))
        return await _orig_loop(self, ip or host, port, **kwargs)

    asyncio.base_events.BaseEventLoop.getaddrinfo = _patched_loop  # type: ignore[assignment]
    _patched = True


async def apply_if_needed() -> dict[str, str]:
    """Probe each target host (sync + async paths); if any fail, install the bypass.

    Returns the dict of {host: cloudflare_ip} overrides that were installed
    (empty dict on production / unfiltered DNS).
    """
    for host in _TARGET_HOSTS:
        if await _system_resolves(host):
            continue
        try:
            ip = _resolve_via_cloudflare(host)
        except Exception:  # noqa: BLE001
            log.exception("dev_dns.cloudflare_resolve_failed", host=host)
            continue
        _overrides[host] = ip
        log.warning("dev_dns.override_installed", host=host, ip=ip)

    if _overrides:
        _install_patches()
    return dict(_overrides)
