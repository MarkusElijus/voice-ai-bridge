r"""Live HubSpot availability test — run from project root.

Usage (Git Bash):
    .venv/Scripts/python.exe scripts/check_hubspot_availability.py

Usage (PowerShell):
    .\.venv\Scripts\python.exe scripts\check_hubspot_availability.py

Hits api.hubapi.com using HUBSPOT_PRIVATE_APP_TOKEN + HUBSPOT_MEETING_LINK_PATH
from .env. Prints the same {options:[A,B]} payload the xAI bridge will feed
back to the model — this is the same code path the live tool uses.

DEV-ONLY DNS WORKAROUND: this script pre-resolves api.hubapi.com via Cloudflare
(1.1.1.1) and patches socket.getaddrinfo to return that IP, because some
content-blocking DNS resolvers (e.g. Mullvad VPN with content blocking) return
NXDOMAIN for HubSpot's API hostname. TLS SNI still uses the original hostname,
so the cert verifies normally. This patch lives in the script only — production
on Fly.io has unfiltered DNS and uses tools_pkg/hubspot_get_availability.py
unmodified.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import time
from pathlib import Path

# Allow `from tools_pkg...` when invoked as a script rather than a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# -----------------------------------------------------------------------------
# Stdlib UDP DNS query to 1.1.1.1 — bypasses any local content-blocking resolver.
# -----------------------------------------------------------------------------

_PUBLIC_DNS = ("1.1.1.1", 53)
_HUBSPOT_HOST = "api.hubapi.com"


def _build_dns_query(name: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD=1, 1 question
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\x00"
    qtail = struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + qname + qtail


def _parse_first_a_record(resp: bytes) -> str | None:
    # Skip 12-byte header + question section (variable-length name then 4 bytes).
    i = 12
    while resp[i] != 0:
        i += resp[i] + 1
    i += 1 + 4
    ancount = struct.unpack(">H", resp[6:8])[0]
    for _ in range(ancount):
        # Name in answer is usually compressed (2-byte pointer starting with 0xC0..0xFF).
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


def _patch_resolver_for_hubspot() -> None:
    """Substitute api.hubapi.com -> Cloudflare IP at every resolver layer.

    httpx (via httpcore -> anyio -> asyncio) does not always go through
    socket.getaddrinfo on Windows — anyio's asyncio backend calls
    asyncio.BaseEventLoop.getaddrinfo, which references socket.getaddrinfo
    by attribute lookup but is itself the layer most async DNS happens at.
    Patching all three layers is belt-and-suspenders and avoids surprises.
    """
    import asyncio

    ip = _resolve_via_cloudflare(_HUBSPOT_HOST)
    print(f"[dns-override] {_HUBSPOT_HOST} -> {ip} (via 1.1.1.1)", file=sys.stderr)

    # httpcore passes the host as bytes; asyncio's loop accepts both. Match on a
    # normalized str so we catch every caller regardless of input type.
    def _is_target(host: object) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="ignore")
        return host == _HUBSPOT_HOST

    # 1. socket.getaddrinfo — sync paths.
    _orig_socket = socket.getaddrinfo

    def _patched_socket(host, *args, **kwargs):
        if _is_target(host):
            return _orig_socket(ip, *args, **kwargs)
        return _orig_socket(host, *args, **kwargs)

    socket.getaddrinfo = _patched_socket  # type: ignore[assignment]

    # 2. asyncio.BaseEventLoop.getaddrinfo — what httpx/httpcore/anyio actually call.
    _orig_loop = asyncio.base_events.BaseEventLoop.getaddrinfo

    async def _patched_loop(self, host, port, **kwargs):
        if _is_target(host):
            return await _orig_loop(self, ip, port, **kwargs)
        return await _orig_loop(self, host, port, **kwargs)

    asyncio.base_events.BaseEventLoop.getaddrinfo = _patched_loop  # type: ignore[assignment]


# Apply the patch BEFORE the tool import so httpx/httpcore see the override.
_patch_resolver_for_hubspot()

from tools_pkg.hubspot_get_availability import handle  # noqa: E402


async def main() -> None:
    result = await handle(call_sid="dev-test", call_id="dev-test")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
