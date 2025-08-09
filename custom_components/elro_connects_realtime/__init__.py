from __future__ import annotations

import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol  # type: ignore[import-untyped]
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_DEVICE_ID, CONF_HOST, DOMAIN
from .device import ElroDevice
from .hub import ElroConnectsHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Service schemas
SERVICE_TEST_ALARM_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.positive_int,
    }
)

SERVICE_SYNC_DEVICES_SCHEMA = vol.Schema({})
SERVICE_GET_DEVICE_NAMES_SCHEMA = vol.Schema({})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ELRO Connects from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Create hub instance
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

    # Start the hub connection
    await hub.async_start()

    # Refresh initial data
    await coordinator.async_config_entry_first_refresh()

    # Forward the setup to the platforms
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
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]

    # Safely stop the hub connection
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


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    _LOGGER.info("Reloading ELRO Connects integration")

    # Get the hub before unloading
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]

    # Use safe reload instead of full stop/start
    try:
        await hub.async_reload_safe()
        _LOGGER.info("Successfully reloaded ELRO Connects hub connection")
    except Exception as ex:
        _LOGGER.error("Error during safe reload: %s", ex)
        # If safe reload fails, fall back to full reload
        await async_unload_entry(hass, entry)
        await async_setup_entry(hass, entry)


class ElroConnectsCoordinator(DataUpdateCoordinator[dict[int, ElroDevice]]):
    """Class to manage fetching data from ELRO Connects hub."""

    def __init__(self, hass: HomeAssistant, hub: ElroConnectsHub) -> None:
        """Initialize."""
        self.hub = hub
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
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
