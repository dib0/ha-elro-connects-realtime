"""ELRO Connects K2 hub, backed by the elro-connects-k2-protocol library.

All K2 wire handling (XOR framing, CMD_CODE routing, status decoding, device
profiles) lives in ``elro_connects_k2_protocol``. This module only adapts that
library's ``K2Gateway`` to the interface the rest of this integration expects:
a ``dict[int, ElroDevice]`` plus update callbacks, exactly like the K1
``ElroConnectsHub``.

Library: https://github.com/ldebruijn/elro-connects-k2-protocol
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from elro_connects_k2_protocol.gateway import K2Gateway
from elro_connects_k2_protocol.models import AlarmState, SubDevice, UpdateSource

from .const import (
    DEVICE_STATE_ALARM,
    DEVICE_STATE_CLOSED,
    DEVICE_STATE_NORMAL,
    DEVICE_STATE_OPEN,
    DEVICE_STATE_UNKNOWN,
    K2_KEEPALIVE_SECONDS,
    PROTOCOL_K2,
)
from .device import ElroDevice

_LOGGER = logging.getLogger(__name__)

# Alarm states that mean "this sensor is reporting its hazard/open condition".
# ALERT ("BB") is the test/alert state: the detector really is sounding, so it
# is reported as triggered just like a genuine alarm. OPEN and OPEN_VARIANT are
# the alternate open encodings used by GS320 series contact sensors.
_TRIGGERED_STATES = frozenset(
    {
        AlarmState.ALARM,
        AlarmState.ALERT,
        AlarmState.OPEN,
        AlarmState.OPEN_VARIANT,
    }
)

# Same set as names, for the entity platforms which read ElroDevice.alarm_state.
TRIGGERED_ALARM_STATES = frozenset(state.name for state in _TRIGGERED_STATES)


class ElroK2Hub:
    """Communicate with an ELRO Connects K2 hub through the protocol library."""

    def __init__(self, host: str, device_id: str) -> None:
        """Initialize the hub."""
        self._host = host
        self._device_id = device_id
        self._gateway = K2Gateway(host, device_id)
        self._devices: dict[int, ElroDevice] = {}
        self._device_update_callbacks: list[Callable[[ElroDevice], None]] = []
        self._running = False
        self._reloading = False
        self._keepalive_task: asyncio.Task[None] | None = None
        # The library keeps one set of collect buffers per gateway, so two
        # overlapping syncs (scheduled poll + service call) would interleave.
        self._sync_lock = asyncio.Lock()

    @property
    def devices(self) -> dict[int, ElroDevice]:
        """Return all devices."""
        return self._devices

    @property
    def protocol(self) -> str:
        """Return current protocol."""
        return PROTOCOL_K2

    def add_device_update_callback(
        self, callback: Callable[[ElroDevice], None]
    ) -> None:
        """Add a callback for device updates."""
        if callback not in self._device_update_callbacks:
            self._device_update_callbacks.append(callback)
            _LOGGER.debug(
                "Added device update callback, total: %d",
                len(self._device_update_callbacks),
            )

    def remove_device_update_callback(
        self, callback: Callable[[ElroDevice], None]
    ) -> None:
        """Remove a callback for device updates."""
        if callback in self._device_update_callbacks:
            self._device_update_callbacks.remove(callback)
            _LOGGER.debug(
                "Removed device update callback, total: %d",
                len(self._device_update_callbacks),
            )

    async def async_start(self) -> None:
        """Start the hub connection and fetch the initial device state."""
        if self._running:
            return

        self._running = True
        self._reloading = False

        try:
            self._gateway.add_update_callback(self._handle_gateway_update)
            await self._gateway.connect()

            # CMD_CODE 54 -> 55/56, followed by the nickname sync (24 -> 17).
            await self.async_sync_devices()

            self._keepalive_task = asyncio.create_task(self._async_keepalive())

            _LOGGER.info(
                "ELRO Connects hub started successfully (Protocol: %s, %d devices)",
                self.protocol,
                len(self._devices),
            )
        except Exception as ex:
            _LOGGER.error("Failed to start ELRO Connects hub: %s", ex)
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        """Stop the hub connection."""
        _LOGGER.info("Stopping ELRO Connects hub (reloading: %s)", self._reloading)
        self._running = False

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        self._keepalive_task = None

        self._gateway.remove_update_callback(self._handle_gateway_update)
        await self._gateway.disconnect()

        if not self._reloading:
            self._device_update_callbacks.clear()
            _LOGGER.info("ELRO Connects hub stopped and callbacks cleared")
        else:
            _LOGGER.info("ELRO Connects hub stopped (callbacks preserved for reload)")

    async def async_reload_safe(self) -> None:
        """Reconnect without losing device state or entity callbacks."""
        _LOGGER.info("Safely reloading ELRO Connects hub connection")
        self._reloading = True

        await self._gateway.disconnect()
        await self._gateway.connect()
        await self.async_sync_devices()

        self._reloading = False
        _LOGGER.info("Safe reload completed")

    async def async_sync_devices(self) -> None:
        """Refresh every device (CMD_CODE 54) and their names (CMD_CODE 24)."""
        async with self._sync_lock:
            # The hub ignores APP_SEND until the session has been re-activated.
            await self._gateway.activate()
            devices = await self._gateway.sync_devices()

        if not devices:
            # sync_devices() returns whatever it collected before its timeout,
            # so a hub that never answered is an empty dict rather than an
            # error. Left silent, every device just quietly goes stale.
            _LOGGER.warning(
                "K2 sync returned no devices; the hub at %s did not answer "
                "CMD_CODE 54 (devices will go unavailable if this persists)",
                self._host,
            )
        else:
            _LOGGER.debug("K2 sync returned %d device(s)", len(devices))

        for sub_id, sub_device in devices.items():
            self._update_device(sub_id, sub_device)

    async def async_sync_device_status(self) -> None:
        """Refresh device status.

        The K2 answers status and names through the same CMD_CODE 54 sync, so
        this is the same call as ``async_sync_devices``.
        """
        await self.async_sync_devices()

    async def async_get_device_names(self) -> None:
        """Fetch the sub-device nicknames stored in the hub (CMD_CODE 24 -> 17)."""
        async with self._sync_lock:
            await self._gateway.activate()
            names = await self._gateway.sync_device_names()

        for sub_id, name in names.items():
            device = self._devices.get(sub_id)
            if device is None or not name or device.name == name:
                continue
            device.name = name
            device.last_seen = datetime.now()
            _LOGGER.info("K2: Device %d name: %s", sub_id, name)
            self._notify_device_update(device)

    async def async_test_device_alarm(self, device_id: int) -> None:
        """Trigger the test alarm on a device (CMD_CODE 1)."""
        sub_device = self._gateway.devices.get(device_id)
        if sub_device is None:
            _LOGGER.error("Cannot test device %d: unknown device", device_id)
            return

        action = sub_device.profile.test_action
        if action is None:
            _LOGGER.warning(
                "Device %d (%s) does not support an alarm test",
                device_id,
                sub_device.profile.name,
            )
            return

        await self._gateway.activate()
        self._gateway.send_device_action(device_id, action)
        _LOGGER.info("Sent alarm test to device %d (payload %s)", device_id, action)

    async def _async_keepalive(self) -> None:
        """Re-activate the session so the hub keeps accepting commands."""
        # async_stop cancels this task, so the sleep is the only exit point.
        while self._running:
            try:
                await asyncio.sleep(K2_KEEPALIVE_SECONDS)
                await self._gateway.activate()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                _LOGGER.error("Error in K2 keepalive: %s", ex)

    def _handle_gateway_update(
        self, sub_id: int, sub_device: SubDevice, source: UpdateSource
    ) -> None:
        """Handle a device update coming from the protocol library.

        Called straight from the library's UDP receive callback for every push
        (CMD_CODE 19), poll response (CMD_CODE 55/56) and pairing event.
        """
        device = self._update_device(sub_id, sub_device)
        _LOGGER.debug(
            "K2 update (%s): device %d state=%s battery=%d%%",
            source.name,
            sub_id,
            device.state,
            device.battery_level,
        )

    def _update_device(self, sub_id: int, sub_device: SubDevice) -> ElroDevice:
        """Copy a library ``SubDevice`` onto our ``ElroDevice`` and notify."""
        device = self._get_or_create_device(sub_id)
        profile = sub_device.profile

        device.protocol = PROTOCOL_K2
        device.device_type = sub_device.raw_type
        device.model_name = ", ".join(profile.model_hints) or profile.name
        device.capabilities = profile.capabilities
        device.mains_powered = profile.mains_powered
        device.battery_level = sub_device.battery_pct
        device.signal_bars = sub_device.signal_bars
        device.alarm_state = sub_device.alarm_state.name
        device.raw_status = sub_device.raw_status
        device.state = _map_state(sub_device)
        device.co2_ppm = sub_device.co2_ppm
        device.temperature_c = sub_device.temperature_c
        device.humidity_pct = sub_device.humidity_pct
        device.temperature_setpoint = sub_device.temperature_setpoint
        device.valve_open = sub_device.valve_open
        device.window_open = sub_device.window_open
        device.thermostat_mode = (
            sub_device.thermostat_mode.name.lower()
            if sub_device.thermostat_mode is not None
            else None
        )
        if sub_device.nickname:
            device.name = sub_device.nickname
        elif not device.name:
            device.name = f"{profile.name} {sub_id}"
        device.last_seen = datetime.now()

        self._notify_device_update(device)
        return device

    def _get_or_create_device(self, device_id: int) -> ElroDevice:
        """Get existing device or create new one."""
        if device_id not in self._devices:
            self._devices[device_id] = ElroDevice(device_id, self._device_id)
            _LOGGER.info("Created new device: %d", device_id)
        return self._devices[device_id]

    def _notify_device_update(self, device: ElroDevice) -> None:
        """Notify callbacks of device update."""
        for callback in self._device_update_callbacks:
            try:
                callback(device)
            except Exception as ex:
                _LOGGER.error("Error in device update callback: %s", ex)


def _map_state(sub_device: SubDevice) -> str:
    """Map a library ``AlarmState`` onto this integration's state strings."""
    capability_keys = {c.key for c in sub_device.profile.capabilities}
    triggered = sub_device.alarm_state in _TRIGGERED_STATES

    if "door" in capability_keys:
        return DEVICE_STATE_OPEN if triggered else DEVICE_STATE_CLOSED
    if "valve" in capability_keys:
        # Thermostats repurpose the alarm byte, so read the decoded valve field.
        return DEVICE_STATE_OPEN if sub_device.valve_open else DEVICE_STATE_CLOSED

    if triggered:
        return DEVICE_STATE_ALARM
    if sub_device.alarm_state in (AlarmState.CLEAR, AlarmState.SILENCED):
        return DEVICE_STATE_NORMAL
    # FAULT and anything the library could not decode.
    return DEVICE_STATE_UNKNOWN
