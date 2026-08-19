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
    CONF_DEBUG_LOGGING,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PROTOCOL,
    DATA_CREATED_UNIQUE_IDS,
    DEFAULT_DEBUG_LOGGING,
    DOMAIN,
    K2_PROTOCOL_LOGGER,
    PROTOCOL_AUTO,
    PROTOCOL_K1,
    PROTOCOL_K2,
    SUBDEVICE_PREFIX,
)
from .detect import async_detect_protocol
from .device import ElroDevice
from .hub import ElroConnectsHub
from .k2_hub import ElroK2Hub
from .models import ElroHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Loggers the debug_logging option raises to DEBUG: this integration's own
# package and the K2 protocol library, which is where the wire-level detail
# lives. __package__ is spelled out as a fallback so the tuple stays correct if
# the module is ever imported in a way that leaves it unset.
DEBUG_LOGGERS: tuple[str, ...] = (
    __package__ or "custom_components.elro_connects_realtime",
    K2_PROTOCOL_LOGGER,
)

# Levels those loggers had before the option was first applied. Turning the
# option off restores them instead of blanking them to NOTSET, so a level set in
# configuration.yaml (or by the logger integration before we ran) survives.
_ORIGINAL_LOG_LEVELS: dict[str, int] = {}

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


def _subdevice_id_from_identifiers(device_entry: dr.DeviceEntry) -> int | None:
    """Return the ELRO sub-device ID of a registry device, or None for the hub.

    Handles both the current f"{SUBDEVICE_PREFIX}{hub_id}_{sub_id}" identifiers
    and the pre-migration f"{SUBDEVICE_PREFIX}{sub_id}" ones, since the sub-device
    ID is the last segment either way.
    """
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN or not identifier.startswith(SUBDEVICE_PREFIX):
            continue
        try:
            return int(identifier.rsplit("_", 1)[-1])
        except ValueError:
            return None
    return None


def _rescope_id(old_id: str, hub_id: str) -> str | None:
    """Return the hub-scoped form of an unscoped identifier, or None.

    None means "leave this one alone": it is not ours, or it is already scoped.
    """
    if not old_id.startswith(SUBDEVICE_PREFIX):
        return None
    if old_id.startswith(f"{SUBDEVICE_PREFIX}{hub_id}_"):
        return None
    remainder = old_id[len(SUBDEVICE_PREFIX) :]
    # An unscoped ID has the numeric sub-device ID first; a scoped one has the
    # hub ID there ("ST_abcf234adbfd").
    if not remainder.split("_", 1)[0].isdigit():
        return None
    return f"{SUBDEVICE_PREFIX}{hub_id}_{remainder}"


def _async_migrate_registry_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Add the hub ID to registry identifiers written before they were scoped.

    Sub-device IDs restart at 1 on every hub, so the original
    f"{SUBDEVICE_PREFIX}{sub_id}" scheme collided when two hubs were configured:
    the second hub's entities landed on the first hub's devices, and one of the
    two lost its entities entirely. Renaming in place keeps entity IDs, history
    and customisations; this is idempotent, so it can run on every setup.
    """
    hub_id = entry.data[CONF_DEVICE_ID]
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    migrated_entities = 0
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        new_unique_id = _rescope_id(entity_entry.unique_id, hub_id)
        if new_unique_id is None:
            continue
        if entity_registry.async_get_entity_id(
            entity_entry.domain, DOMAIN, new_unique_id
        ):
            # The scoped entity already exists — this entry is a collided
            # leftover, which remove_stale_devices can clear out.
            _LOGGER.debug(
                "Not migrating %s: %s is already taken",
                entity_entry.entity_id,
                new_unique_id,
            )
            continue
        entity_registry.async_update_entity(
            entity_entry.entity_id, new_unique_id=new_unique_id
        )
        migrated_entities += 1

    migrated_devices = 0
    for device_entry in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        # A device shared with another entry is one of the collisions this fix
        # exists for. Renaming it would take it away from the other hub, so it
        # is left behind and a correctly scoped device is created instead.
        if len(device_entry.config_entries) > 1:
            continue
        new_identifiers = set()
        changed = False
        for domain, identifier in device_entry.identifiers:
            rescoped = _rescope_id(identifier, hub_id) if domain == DOMAIN else None
            new_identifiers.add((domain, rescoped or identifier))
            changed = changed or rescoped is not None
        if not changed:
            continue
        device_registry.async_update_device(
            device_entry.id, new_identifiers=new_identifiers
        )
        migrated_devices += 1

    if migrated_entities or migrated_devices:
        _LOGGER.info(
            "Scoped %d device(s) and %d entity(ies) to hub %s",
            migrated_devices,
            migrated_entities,
            hub_id,
        )


def _apply_debug_logging(hass: HomeAssistant) -> None:
    """Set the integration and protocol library logger levels from the option.

    Loggers are global while the option is per config entry, so debug logging is
    on as soon as *any* configured hub asks for it: with a K1 and a K2 entry side
    by side, ticking the box on the misbehaving one is enough. Called on setup,
    on unload and whenever the options change.
    """
    enabled = any(
        entry.options.get(CONF_DEBUG_LOGGING, DEFAULT_DEBUG_LOGGING)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    for name in DEBUG_LOGGERS:
        logger = logging.getLogger(name)
        original = _ORIGINAL_LOG_LEVELS.setdefault(name, logger.level)
        logger.setLevel(logging.DEBUG if enabled else original)

    if enabled:
        # At INFO so it is visible in a log that was captured before the option
        # was switched on, which is where the question "was debug even on?"
        # usually gets asked.
        _LOGGER.info(
            "Debug logging enabled for %s; this logs every UDP frame exchanged "
            "with the hub. Turn it off in the integration options when done",
            ", ".join(DEBUG_LOGGERS),
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options.

    Only the logger levels are involved, and those take effect immediately, so
    the entry does not need reloading - the hub connection and every entity stay
    up while debug logging is switched on or off.
    """
    _apply_debug_logging(hass)


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

    # First thing in setup, so protocol detection and the hub handshake below are
    # already covered when the user turns the option on and reloads.
    _apply_debug_logging(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.debug(
        "Setting up entry %s: host=%s device_id=%s configured_protocol=%s options=%s",
        entry.entry_id,
        entry.data.get(CONF_HOST),
        entry.data.get(CONF_DEVICE_ID),
        entry.data.get(CONF_PROTOCOL),
        dict(entry.options),
    )

    protocol = await _async_resolve_protocol(hass, entry)

    # Must run before any entity registers, so entities come up on the identity
    # they will keep.
    _async_migrate_registry_ids(hass, entry)

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
    _LOGGER.debug("Creating %s hub instance for %s", protocol, entry.data[CONF_HOST])
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

    # DataUpdateCoordinator only schedules its next refresh while it has at
    # least one listener, and these entities are not CoordinatorEntity
    # subclasses: they update from the hub's own callbacks. Without a listener
    # the coordinator polls exactly once, during the first refresh below, and
    # never again. The K1 hub survives that because it polls itself in
    # _async_heartbeat, but the K2 hub relies on this poll — its keepalive only
    # re-activates the session — so every K2 device would stop being refreshed
    # and go unavailable once ElroDevice.is_available's 5-minute window expired.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))

    # Store hub and coordinator
    hass.data[DOMAIN][entry.entry_id] = {
        "hub": hub,
        "coordinator": coordinator,
        # Filled in by the entity platforms; see the note in sensor.py on why
        # this lives here and not in module state.
        DATA_CREATED_UNIQUE_IDS: set(),
    }

    # Start the hub connection. A hub that is offline — or, for the K2, a UDP
    # port 1025 still held by something else — is transient, so let HA retry
    # instead of failing the entry outright.
    try:
        await hub.async_start()
    except Exception as ex:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # The traceback names which step of the handshake failed, which is the
        # whole question in the "no devices" reports. At debug level: Home
        # Assistant retries a not-ready entry indefinitely, and an offline hub
        # should not fill the log with tracebacks.
        _LOGGER.debug("Hub start failed for %s", entry.data[CONF_HOST], exc_info=True)
        raise ConfigEntryNotReady(
            f"Could not connect to the {protocol} hub at {entry.data[CONF_HOST]}: {ex}"
        ) from ex

    # The K1 hub answers asynchronously, so give device discovery time to land.
    # The K2 hub already collected its devices during async_start().
    if protocol != PROTOCOL_K2:
        await asyncio.sleep(5)

    # Refresh initial data
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug(
        "First refresh done for %s: %d device(s) known (%s)",
        entry.data[CONF_HOST],
        len(hub.devices),
        ", ".join(f"{sub_id}={device.name}" for sub_id, device in hub.devices.items())
        or "none",
    )

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
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        removed_devices = 0
        removed_entities = 0

        for entry_id, entry_data in hass.data[DOMAIN].items():
            hub = entry_data["hub"]
            live_ids = set(hub.devices)
            live_unique_ids: set[str] = entry_data[DATA_CREATED_UNIQUE_IDS]

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
                # Resolved through the device link rather than by parsing the
                # unique ID, which cannot be split reliably: both the hub ID and
                # the entity suffix contain underscores.
                device_entry = (
                    device_registry.async_get(entity_entry.device_id)
                    if entity_entry.device_id
                    else None
                )
                sub_id = (
                    _subdevice_id_from_identifiers(device_entry)
                    if device_entry
                    else None
                )
                # Entities of a removed device went with it above.
                if sub_id is None or sub_id not in live_ids:
                    continue
                # Only prune when this device does have current entities;
                # otherwise a device that was missing from the last sync would
                # lose all of them.
                prefix = f"{hub.devices[sub_id].unique_id}_"
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

    # Recheck the option now this entry is on its way out: any other hub still
    # asking for debug logging keeps it on, otherwise the loggers go back to the
    # level they had. A reload still counts the entry that is coming back, so no
    # debug output is lost across one.
    _apply_debug_logging(hass)

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
