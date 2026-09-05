"""Broadcast discovery of K2 hubs on the local network.

A K2 hub answers an XOR-framed ``IOT_KEY?`` addressed to ``devID: "NULL"`` with
a ``NODE_ACK`` that carries its own device ID, so a single UDP broadcast yields
both the address and the ``ST_...`` ID that the config flow would otherwise ask
the user to copy out of the ELRO Connects app.

The protocol library ships ``discover_gateway()``, but it resolves on the first
responder and hands back a gateway object.  A config flow wants the whole list -
a household can have two hubs - and only wants the addresses, so the scan is
repeated here on top of the same protocol helpers.

K1 hubs are not discoverable this way.  They do not speak the XOR framing, and
their plain-text ``IOT_KEY?`` has to name the hub, which is exactly the piece of
information discovery is supposed to find.  Everything returned by this module
is therefore a K2, and the config flow records it as one without re-probing.

A scan must not run while a K2 hub is already talking to Home Assistant.  Two
sockets can share UDP port 1025 (see ``async_discover_hubs``), but an inbound
unicast datagram is then delivered to only one of them, so a scan would swallow
frames belonging to the live session - including an alarm.  The caller is
responsible for that check; see ``async_step_user`` in the config flow.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import Any

from elro_connects_k2_protocol.protocol import (
    UDP_PORT,
    build_discovery,
    decrypt_message,
    encrypt_message,
)
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Length of the listening window, split evenly between the broadcasts below: the
# request and the answer are both single unacknowledged datagrams, so a dropped
# one is not retransmitted by anything except us.
DISCOVERY_TIMEOUT = 4.0
DISCOVERY_BROADCASTS = 2

# Sent unconditionally, in addition to whatever the network integration knows
# about: it is the address the vendor app uses and the one that works on a host
# with a single interface, which is the common case.
GLOBAL_BROADCAST = "255.255.255.255"


@dataclass(frozen=True)
class DiscoveredHub:
    """A hub that answered the discovery broadcast."""

    host: str
    device_id: str


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect every hub that answers, keyed by device ID.

    Keying by device ID rather than address deduplicates the answers a hub sends
    when it is reached over more than one broadcast address, and keeps the two
    hubs of a two-hub household apart even if one of them has just changed IP.
    """

    def __init__(self) -> None:
        self.hubs: dict[str, DiscoveredHub] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text, obj = decrypt_message(data)
        if not isinstance(obj, dict):
            # A K1 hub replying in plain text lands here, as does our own
            # broadcast looping back on a host that is also running a hub
            # emulator. Neither is an error, but both are worth seeing.
            _LOGGER.debug(
                "Discovery: reply from %s did not decode as a K2 frame: %r",
                addr[0],
                text[:200],
            )
            return
        device_id = _hub_device_id(obj)
        if device_id is None:
            _LOGGER.debug(
                "Discovery: ignoring K2 frame from %s: %s", addr[0], text[:200]
            )
            return
        if device_id in self.hubs:
            return
        self.hubs[device_id] = DiscoveredHub(host=addr[0], device_id=device_id)
        _LOGGER.info("Discovered K2 hub %s at %s", device_id, addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Discovery: UDP error: %s", exc)


def _hub_device_id(obj: dict[str, Any]) -> str | None:
    """Return the device ID of a discovery answer, or None if it is not one.

    Discarded here: our own request looping back (``IOT_KEY?`` with devID
    ``NULL``), and the unsolicited status frames a hub already talking to
    another controller sprays over the same port.
    """
    if obj.get("action") != "NODE_ACK":
        return None
    msg = obj.get("msg")
    if not isinstance(msg, dict) or msg.get("CMD_CODE") != 0:
        return None
    device_id = obj.get("devID")
    if not isinstance(device_id, str) or device_id in ("", "NULL"):
        return None
    return device_id


async def _async_broadcast_targets(hass: HomeAssistant) -> list[str]:
    """Return the addresses to send the discovery frame to.

    255.255.255.255 leaves over one interface only - whichever the default route
    picks - so on a multi-homed host (a VPN, a second NIC, a Docker bridge) it
    can go out the one the hub is not on. The network integration knows the
    per-interface broadcast addresses, so those are added to it.
    """
    targets = [GLOBAL_BROADCAST]
    try:
        # manifest.json depends on the network integration, so this is expected
        # to work. Both the import and the call are guarded anyway, and the
        # global broadcast alone is a usable answer: a Home Assistant that never
        # set network up (recovery mode) should still get a config flow it can
        # start rather than one that raises on its first step. The import is
        # local so a failing one is caught by the same handler.
        from homeassistant.components import network

        addresses = await network.async_get_ipv4_broadcast_addresses(hass)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Discovery: no per-interface broadcast addresses (%s)", ex)
        return targets
    targets.extend(
        str(address) for address in addresses if str(address) != GLOBAL_BROADCAST
    )
    return targets


async def async_discover_hubs(
    hass: HomeAssistant, timeout: float = DISCOVERY_TIMEOUT
) -> list[DiscoveredHub]:
    """Broadcast the K2 discovery frame and return every hub that answers.

    Returns an empty list when nothing answers and when the scan could not run
    at all; the caller falls back to manual entry either way.
    """
    loop = asyncio.get_running_loop()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Both options are what K2Gateway.connect() sets, and for the same reasons:
    # SO_REUSEADDR because asyncio has not set it on UDP sockets since Python
    # 3.8, so a port still held by one of our own not-yet-reaped sockets would
    # raise EADDRINUSE; SO_BROADCAST because allow_broadcast= cannot be passed
    # alongside sock= from Python 3.14 on.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        # A K2 only answers requests that came from port 1025, so binding it is
        # not just about receiving: without it there is no answer at all.
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as ex:
        sock.close()
        # Linux shares an addr:port between UDP sockets only when every socket
        # bound to it set SO_REUSEADDR, so getting here means an unrelated
        # process is squatting on 1025 - a second copy of this integration, or
        # elro_test_tool.py. Logged rather than raised: the config flow falls
        # back to the manual form, which is also what a K1 user needs.
        _LOGGER.info(
            "Cannot bind UDP port %d for hub discovery (%s); skipping the scan",
            UDP_PORT,
            ex,
        )
        return []

    transport: Any
    collector: _DiscoveryProtocol
    transport, collector = await loop.create_datagram_endpoint(
        _DiscoveryProtocol, sock=sock
    )
    try:
        targets = await _async_broadcast_targets(hass)
        interval = timeout / DISCOVERY_BROADCASTS
        for _ in range(DISCOVERY_BROADCASTS):
            for target in targets:
                # Re-encrypted per send: encrypt_message picks a fresh XOR seed.
                transport.sendto(encrypt_message(build_discovery()), (target, UDP_PORT))
            _LOGGER.debug("Discovery: broadcast IOT_KEY? to %s", ", ".join(targets))
            await asyncio.sleep(interval)
    finally:
        transport.close()

    hubs = sorted(collector.hubs.values(), key=lambda hub: hub.device_id)
    _LOGGER.debug("Discovery: %d hub(s) found in %.1fs", len(hubs), timeout)
    return hubs
