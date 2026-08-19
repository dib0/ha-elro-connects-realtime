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
    """Resolve a future as soon as a decodable K2 frame arrives.

    Every datagram is logged at debug, including the ones that do not settle the
    question: a hub that is answering something unexpected and a hub that is not
    answering at all both come out as K1 here, and the log is the only place that
    difference is visible.
    """

    def __init__(self, host: str, result: asyncio.Future[bool]) -> None:
        self._host = host
        self._result = result
        # Set alongside the future, so the K1/K2 verdict can be logged with the
        # reason behind it.
        self.reason = "no reply"

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        _LOGGER.debug(
            "Detect: %d byte(s) from %s:%d: %s",
            len(data),
            addr[0],
            addr[1],
            data[:64].hex(),
        )
        if addr[0] != self._host:
            _LOGGER.debug(
                "Detect: ignoring datagram from %s, waiting for %s",
                addr[0],
                self._host,
            )
            return
        if self._result.done():
            _LOGGER.debug("Detect: already decided, ignoring further datagrams")
            return
        if not data or data[0] == 0x7B:  # '{' — plain JSON, so this is a K1 hub
            _LOGGER.debug("Detect: plain-text reply from %s: %r", addr[0], data[:200])
            self.reason = "plain-text reply"
            self._result.set_result(False)
            return
        text, obj = decrypt_message(data)
        if not isinstance(obj, dict):
            _LOGGER.debug(
                "Detect: reply from %s did not decode as a K2 frame: %r",
                addr[0],
                text[:200],
            )
            return
        # Only hub-originated frames count. Our own request loops back when the
        # hub address is this host, and it must not be read as an answer.
        action = obj.get("action")
        if action in _HUB_ACTIONS:
            _LOGGER.debug("Detect: K2 frame from %s: %s", addr[0], text[:200])
            self.reason = f"K2 frame ({action})"
            self._result.set_result(True)
        else:
            _LOGGER.debug(
                "Detect: K2 frame from %s carries action %r, which is not a hub "
                "answer (our own request looping back?)",
                addr[0],
                action,
            )

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
        # A K2 hub only answers requests that came from local port 1025, so
        # without that port there is no way to tell the generations apart, and no
        # way to talk to a K2 either. Named explicitly because the K1 fallback
        # otherwise looks like a successful detection.
        _LOGGER.warning(
            "Cannot bind UDP port %d for protocol detection (%s); assuming K1. "
            "A K2 hub cannot work while another process holds this port - see "
            "the K2 troubleshooting section of the integration README",
            UDP_PORT,
            ex,
        )
        return PROTOCOL_K1

    _LOGGER.debug("Detect: UDP port %d bound as %s", UDP_PORT, sock.getsockname())

    transport: Any
    detector: _DetectProtocol
    transport, detector = await loop.create_datagram_endpoint(
        lambda: _DetectProtocol(host, result), sock=sock
    )
    try:
        activation = build_activation(device_id)
        _LOGGER.debug(
            "Detect: sending K2 handshake to %s:%d: %s", host, UDP_PORT, activation
        )
        transport.sendto(encrypt_message(activation), (host, UDP_PORT))
        is_k2 = await asyncio.wait_for(result, timeout=timeout)
    except TimeoutError:
        _LOGGER.debug("Detect: no reply from %s within %.1fs", host, timeout)
        is_k2 = False
    finally:
        transport.close()

    protocol = PROTOCOL_K2 if is_k2 else PROTOCOL_K1
    # The reason matters as much as the answer: "no reply" on a hub that really
    # is a K2 means the handshake never got through, not that it is a K1.
    _LOGGER.info(
        "Detected %s protocol for hub %s (%s): %s",
        protocol,
        device_id,
        host,
        detector.reason,
    )
    return protocol
