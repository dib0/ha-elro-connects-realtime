"""Hub protocol detection (K1 vs K2).

The two hub generations answer the same UDP port (1025) but frame their replies
differently:

* K1 replies in plain UTF-8 (``{ST_answer_OK}`` or a JSON object), so the first
  byte of the datagram is printable.
* K2 replies XOR-framed: byte 0 is a random seed and every following byte is
  XORed with ``seed ^ 0x23``.  The reply only arrives when the request itself
  was XOR-framed and sent from local port 1025.

Detection therefore sends the K2 handshake and classifies the answer.  Anything
that is not a decodable K2 frame is treated as K1, which is also the safe
fallback when the hub does not answer at all: the K1 code path is the one that
worked before this module existed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from elro_connects_k2_protocol.protocol import (
    UDP_PORT,
    build_activation,
    decrypt_message,
    encrypt_message,
)

from .const import PROTOCOL_K1, PROTOCOL_K2

_LOGGER = logging.getLogger(__name__)

DETECT_TIMEOUT = 3.0

# Actions a K2 hub sends: the discovery/activation answer plus its unsolicited
# status frames. "IOT_KEY?" is a request, so seeing it means our own loopback.
_HUB_ACTIONS = frozenset({"NODE_ACK", "NODE_SEND", "APP_SEND"})


class _DetectProtocol(asyncio.DatagramProtocol):
    """Resolve a future as soon as a decodable K2 frame arrives."""

    def __init__(self, host: str, result: asyncio.Future[bool]) -> None:
        self._host = host
        self._result = result

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] != self._host or self._result.done():
            return
        if not data or data[0] == 0x7B:  # '{' — plain JSON, so this is a K1 hub
            _LOGGER.debug("Detect: plain-text reply from %s", addr[0])
            self._result.set_result(False)
            return
        _text, obj = decrypt_message(data)
        if not isinstance(obj, dict):
            return
        # Only hub-originated frames count. Our own request loops back when the
        # hub address is this host, and it must not be read as an answer.
        if obj.get("action") in _HUB_ACTIONS:
            _LOGGER.debug("Detect: K2 frame from %s: %s", addr[0], obj.get("action"))
            self._result.set_result(True)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Detect: UDP error: %s", exc)


async def async_detect_protocol(
    host: str, device_id: str, timeout: float = DETECT_TIMEOUT
) -> str:
    """Return PROTOCOL_K2 when the hub answers the K2 handshake, else PROTOCOL_K1."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[bool] = loop.create_future()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    try:
        # A K2 only answers requests coming from port 1025.
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as ex:
        sock.close()
        _LOGGER.warning(
            "Cannot bind UDP port %d for protocol detection (%s); assuming K1",
            UDP_PORT,
            ex,
        )
        return PROTOCOL_K1

    transport: Any
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _DetectProtocol(host, result), sock=sock
    )
    try:
        transport.sendto(encrypt_message(build_activation(device_id)), (host, UDP_PORT))
        is_k2 = await asyncio.wait_for(result, timeout=timeout)
    except TimeoutError:
        _LOGGER.debug("Detect: no reply from %s within %.1fs", host, timeout)
        is_k2 = False
    finally:
        transport.close()

    protocol = PROTOCOL_K2 if is_k2 else PROTOCOL_K1
    _LOGGER.info("Detected %s protocol for hub %s (%s)", protocol, device_id, host)
    return protocol
