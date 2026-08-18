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
from homeassistant.helpers import entity_registry as er
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
SERVICE_REMOVE_STALE_DEVICES_SCHEMA = vol.Schema({})

SERVICES = ("test_alarm", "sync_devices", "get_device_names", "remove_stale_devices")

# Prefix of ElroDevice.unique_id, which is both the device registry identifier
# of a sub-device and the leading part of every entity unique ID.
SUBDEVICE_PREFIX = "elro_realtime_"


def _subdevice_id_from_identifiers(device_entry: dr.DeviceEntry) -> int | None:
    """Return the ELRO sub-device ID of a registry device, or None for the hub."""
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN or not identifier.startswith(SUBDEVICE_PREFIX):
            continue
        try:
            return int(identifier[len(SUBDEVICE_PREFIX) :])
        except ValueError:
            return None
    return None


def _subdevice_id_from_unique_id(unique_id: str) -> int | None:
    """Return the ELRO sub-device ID an entity unique ID belongs to."""
    if not unique_id.startswith(SUBDEVICE_PREFIX):
        return None
    head = unique_id[len(SUBDEVICE_PREFIX) :].split("_", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


async def _async_resolve_protocol(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return the protocol to use for this entry, detecting it when needed.

    A detected result is written back to the config entry so the probe only runs
    once per hub; entries created before protocol selection existed land here too.
    """
    # Entries written before the protocol values were lower-cased (for use as
    # translation keys) hold "K1"/"K2", so normalise before comparing.
    configured = str(entry.data.get(CONF_PROTOCOL, PROTOCOL_AUTO)).lower()
    if configured in (PROTOCOL_K1, PROTOCOL_K2):
        if configured != entry.data.get(CONF_PROTOCOL):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_PROTOCOL: configured}
            )
        return configured

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
        model=f"Connects Real-time Hub ({protocol.upper()})",
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

    async def async_remove_stale_devices(call: ServiceCall) -> None:
        """Delete registry devices and entities the hub no longer reports.

        Pairing a K1 hub and then a K2 hub against the same config entry leaves
        the old sub-devices behind: nothing removes a device registry entry when
        it simply stops being reported. This prunes every device whose ID the
        hub does not currently know, plus entities left over from the K1 entity
        layout on devices that do still exist (a K1 "alarm" entity next to the
        K2 per-hazard ones, for instance).
        """
        # Imported here so setting up the integration does not pull in the
        # entity platform modules before Home Assistant asks for them.
        from .binary_sensor import created_binary_sensor_unique_ids
        from .sensor import created_sensor_unique_ids

        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        live_unique_ids = (
            created_binary_sensor_unique_ids() | created_sensor_unique_ids()
        )
        removed_devices = 0
        removed_entities = 0

        for entry_id, entry_data in hass.data[DOMAIN].items():
            hub = entry_data["hub"]
            live_ids = set(hub.devices)

            for device_entry in dr.async_entries_for_config_entry(
                device_registry, entry_id
            ):
                sub_id = _subdevice_id_from_identifiers(device_entry)
                # None is the hub device itself; removing it would orphan the
                # rest, so it is always kept.
                if sub_id is None or sub_id in live_ids:
                    continue
                _LOGGER.info(
                    "Removing stale ELRO device %s (sub-device %d)",
                    device_entry.name_by_user or device_entry.name,
                    sub_id,
                )
                # Unlinks the config entry and deletes the device once it is the
                # last entry referencing it, so a device shared with a second
                # hub entry survives.
                device_registry.async_update_device(
                    device_entry.id, remove_config_entry_id=entry_id
                )
                removed_devices += 1

            for entity_entry in er.async_entries_for_config_entry(
                entity_registry, entry_id
            ):
                if entity_entry.unique_id in live_unique_ids:
                    continue
                sub_id = _subdevice_id_from_unique_id(entity_entry.unique_id)
                # Entities of a removed device went with it above.
                if sub_id is None or sub_id not in live_ids:
                    continue
                # Only prune when this device does have current entities;
                # otherwise a device that was missing from the last sync would
                # lose all of them.
                prefix = f"{SUBDEVICE_PREFIX}{sub_id}_"
                if not any(uid.startswith(prefix) for uid in live_unique_ids):
                    continue
                _LOGGER.info("Removing stale ELRO entity %s", entity_entry.entity_id)
                entity_registry.async_remove(entity_entry.entity_id)
                removed_entities += 1

        _LOGGER.info(
            "Cleanup removed %d stale device(s) and %d stale entity(ies)",
            removed_devices,
            removed_entities,
        )

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

    if not hass.services.has_service(DOMAIN, "remove_stale_devices"):
        hass.services.async_register(
            DOMAIN,
            "remove_stale_devices",
            async_remove_stale_devices,
            schema=SERVICE_REMOVE_STALE_DEVICES_SCHEMA,
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
            for service in SERVICES:
                hass.services.async_remove(DOMAIN, service)
            _LOGGER.info("Removed ELRO Connects services")
        except Exception as ex:
            _LOGGER.error("Error removing services: %s", ex)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a device from its Home Assistant device page.

    Defining this is what makes the "Delete" button appear at all. A device the
    hub still reports would come straight back on the next update, so only
    leftovers are removable; the hub device itself never is.
    """
    sub_id = _subdevice_id_from_identifiers(device_entry)
    if sub_id is None:
        return False

    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if entry_data is None:
        # Entry not loaded, so nothing can claim the device.
        return True

    hub: ElroHub = entry_data["hub"]
    return sub_id not in hub.devices


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
