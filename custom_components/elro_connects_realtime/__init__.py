"""__init__.py with proper hub device creation and timing."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PROTOCOL,
    DOMAIN,
    PROTOCOL_AUTO,
    PROTOCOL_K1,
    PROTOCOL_K2,
)
from .detect import async_detect_protocol
from .device import ElroDevice
from .hub import ElroConnectsHub
from .k2_hub import ElroK2Hub
from .models import ElroHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# The K2 pushes state changes, so polling only refreshes battery/signal values
# and catches anything missed; the K1 needs the tighter loop it always had.
UPDATE_INTERVALS = {
    PROTOCOL_K1: timedelta(seconds=30),
    PROTOCOL_K2: timedelta(seconds=60),
}

# Service schemas
SERVICE_TEST_ALARM_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.positive_int,
    }
)

SERVICE_SYNC_DEVICES_SCHEMA = vol.Schema({})
SERVICE_GET_DEVICE_NAMES_SCHEMA = vol.Schema({})


async def _async_resolve_protocol(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return the protocol to use for this entry, detecting it when needed.

    A detected result is written back to the config entry so the probe only runs
    once per hub; entries created before protocol selection existed land here too.
    """
    configured = entry.data.get(CONF_PROTOCOL, PROTOCOL_AUTO)
    if configured in (PROTOCOL_K1, PROTOCOL_K2):
        return str(configured)

    protocol = await async_detect_protocol(
        entry.data[CONF_HOST], entry.data[CONF_DEVICE_ID]
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_PROTOCOL: protocol}
    )
    return protocol


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ELRO Connects from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    protocol = await _async_resolve_protocol(hass, entry)

    # Create hub device in device registry first - BEFORE creating hub instance
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={
            ("elro_connects_realtime", entry.data[CONF_DEVICE_ID])
        },  # Changed: use device_id
        name=f"ELRO Connects Hub ({entry.data[CONF_DEVICE_ID]})",  # Changed: unique name
        manufacturer="ELRO",
        model=f"Connects Real-time Hub ({protocol})",
        sw_version="1.0.0",
    )
    _LOGGER.info("Created hub device in device registry")

    # Create hub instance
    hub: ElroHub
    if protocol == PROTOCOL_K2:
        hub = ElroK2Hub(
            host=entry.data[CONF_HOST], device_id=entry.data[CONF_DEVICE_ID]
        )
    else:
        hub = ElroConnectsHub(
            host=entry.data[CONF_HOST], device_id=entry.data[CONF_DEVICE_ID], hass=hass
        )

    # Create coordinator for device updates
    coordinator = ElroConnectsCoordinator(hass, hub)

    # Store hub and coordinator
    hass.data[DOMAIN][entry.entry_id] = {
        "hub": hub,
        "coordinator": coordinator,
    }

    # Start the hub connection. A hub that is offline — or, for the K2, a UDP
    # port 1025 still held by something else — is transient, so let HA retry
    # instead of failing the entry outright.
    try:
        await hub.async_start()
    except Exception as ex:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(
            f"Could not connect to the {protocol} hub at {entry.data[CONF_HOST]}: {ex}"
        ) from ex

    # The K1 hub answers asynchronously, so give device discovery time to land.
    # The K2 hub already collected its devices during async_start().
    if protocol != PROTOCOL_K2:
        await asyncio.sleep(5)

    # Refresh initial data
    await coordinator.async_config_entry_first_refresh()

    # Forward the setup to the platforms (this creates entities)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await _async_register_services(hass)

    return True


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register services for ELRO Connects."""

    async def async_test_alarm(call: ServiceCall) -> None:
        """Handle test alarm service call."""
        device_id = call.data.get("device_id")

        # If device_id not provided, try to get it from entity_id
        if not device_id and "entity_id" in call.data:
            entity_id = call.data["entity_id"]
            # Extract device_id from entity state
            state = hass.states.get(entity_id)
            if state and "device_id" in state.attributes:
                device_id = state.attributes["device_id"]

        if not device_id:
            _LOGGER.error("No device_id provided for test_alarm service")
            return

        # Find the hub that contains this device
        for entry_data in hass.data[DOMAIN].values():
            hub = entry_data["hub"]
            if device_id in hub.devices:
                await hub.async_test_device_alarm(device_id)
                return

        _LOGGER.error("Device %s not found in any hub", device_id)

    async def async_sync_devices(call: ServiceCall) -> None:
        """Handle sync devices service call."""
        for entry_data in hass.data[DOMAIN].values():
            hub = entry_data["hub"]
            await hub.async_sync_devices()

    async def async_get_device_names(call: ServiceCall) -> None:
        """Handle get device names service call."""
        for entry_data in hass.data[DOMAIN].values():
            hub = entry_data["hub"]
            await hub.async_get_device_names()

    # Register services only if not already registered
    if not hass.services.has_service(DOMAIN, "test_alarm"):
        hass.services.async_register(
            DOMAIN, "test_alarm", async_test_alarm, schema=SERVICE_TEST_ALARM_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, "sync_devices"):
        hass.services.async_register(
            DOMAIN,
            "sync_devices",
            async_sync_devices,
            schema=SERVICE_SYNC_DEVICES_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, "get_device_names"):
        hass.services.async_register(
            DOMAIN,
            "get_device_names",
            async_get_device_names,
            schema=SERVICE_GET_DEVICE_NAMES_SCHEMA,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading ELRO Connects integration")

    # Get hub instance
    hub_data = hass.data[DOMAIN].get(entry.entry_id)
    if hub_data:
        hub = hub_data["hub"]
        try:
            await hub.async_stop()
        except Exception as ex:
            _LOGGER.error("Error stopping hub during unload: %s", ex)

    # Unload platforms
    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Clean up stored data
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.info("Successfully unloaded ELRO Connects entry")
    else:
        _LOGGER.error("Failed to unload ELRO Connects platforms")

    # Remove services if this is the last entry
    if not hass.data[DOMAIN]:
        try:
            hass.services.async_remove(DOMAIN, "test_alarm")
            hass.services.async_remove(DOMAIN, "sync_devices")
            hass.services.async_remove(DOMAIN, "get_device_names")
            _LOGGER.info("Removed ELRO Connects services")
        except Exception as ex:
            _LOGGER.error("Error removing services: %s", ex)

    return unload_ok


class ElroConnectsCoordinator(DataUpdateCoordinator[dict[int, ElroDevice]]):
    """Class to manage fetching data from ELRO Connects hub."""

    def __init__(self, hass: HomeAssistant, hub: ElroHub) -> None:
        """Initialize."""
        self.hub = hub
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVALS.get(hub.protocol, timedelta(seconds=30)),
        )

    async def _async_update_data(self) -> dict[int, ElroDevice]:
        """Update data via library."""
        try:
            # Request device status update
            await self.hub.async_sync_devices()
            return self.hub.devices
        except Exception as exception:
            _LOGGER.error("Error updating data: %s", exception)
            raise UpdateFailed(exception) from exception
