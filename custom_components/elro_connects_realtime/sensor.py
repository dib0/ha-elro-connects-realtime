"""Sensor platform for ELRO Connects Real-time"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_DEVICE_ID, ATTR_DEVICE_TYPE, ATTR_LAST_SEEN, DOMAIN
from .device import ElroDevice
from .hub import ElroConnectsHub

_LOGGER = logging.getLogger(__name__)

# Global registry to track created entities
_CREATED_SENSOR_ENTITIES: set[str] = set()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ELRO Connects sensor platform."""
    hub: ElroConnectsHub = hass.data[DOMAIN][config_entry.entry_id]["hub"]

    entities = []

    # Create sensors for existing devices
    for device in hub.devices.values():
        new_entities = _create_sensors_for_device(device, hub)
        for entity in new_entities:
            if entity.unique_id not in _CREATED_SENSOR_ENTITIES:
                entities.append(entity)
                _CREATED_SENSOR_ENTITIES.add(entity.unique_id)
            else:
                _LOGGER.debug("Skipping duplicate sensor entity: %s", entity.unique_id)

    if entities:
        async_add_entities(entities, True)
        _LOGGER.info("Created %d sensor entities", len(entities))

    # Set up callback for new devices
    def _async_device_updated(device: ElroDevice) -> None:
        """Handle device updates."""
        # Only create entities for devices that have received status updates
        if device.battery_level < 0:
            return

        new_entities = _create_sensors_for_device(device, hub)
        entities_to_add = []

        for entity in new_entities:
            if entity.unique_id not in _CREATED_SENSOR_ENTITIES:
                entities_to_add.append(entity)
                _CREATED_SENSOR_ENTITIES.add(entity.unique_id)

        if entities_to_add:
            async_add_entities(entities_to_add, True)
            _LOGGER.info(
                "Added %d new sensor entities for device %d",
                len(entities_to_add),
                device.id,
            )

    hub.add_device_update_callback(_async_device_updated)


def _create_sensors_for_device(
    device: ElroDevice, hub: ElroConnectsHub
) -> list[ElroConnectsSensor]:
    """Create sensors for a device."""
    entities = []

    # All devices get a battery sensor if they have battery info
    if device.battery_level >= 0:
        entities.append(ElroConnectsBatterySensor(device, hub))

    return entities


class ElroConnectsSensor(SensorEntity):
    """Base class for ELRO Connects sensors."""

    def __init__(self, device: ElroDevice, hub: ElroConnectsHub) -> None:
        """Initialize the sensor."""
        self._device = device
        self._hub = hub
        self._device_id = device.id
        self._attr_unique_id = f"{device.unique_id}_{self._sensor_type}"
        self._attr_device_info = device.device_info

    @property
    def _sensor_type(self) -> str:
        """Return the sensor type identifier."""
        return "sensor"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        device_name = self._device.name or f"Device {self._device.id}"
        return f"{device_name} {self._sensor_name}"

    @property
    def _sensor_name(self) -> str:
        """Return the sensor name suffix."""
        return "Sensor"

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._device.is_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attrs = {
            ATTR_DEVICE_ID: self._device.id,
            ATTR_DEVICE_TYPE: self._device.device_type,
        }

        if self._device.last_seen:
            attrs[ATTR_LAST_SEEN] = self._device.last_seen.isoformat()

        return attrs

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        # Always register the callback when entity is added
        self._hub.add_device_update_callback(self._async_device_updated)

        # If device already exists, trigger an immediate update
        if self._device.id in self._hub.devices:
            self._device = self._hub.devices[self._device.id]
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed from hass."""
        self._hub.remove_device_update_callback(self._async_device_updated)
        # Remove from tracking
        if self.unique_id in _CREATED_SENSOR_ENTITIES:
            _CREATED_SENSOR_ENTITIES.remove(self.unique_id)

    def _async_device_updated(self, device: ElroDevice) -> None:
        """Handle device updates."""
        if device.id == self._device.id:
            self._device = device
            self.async_write_ha_state()


class ElroConnectsBatterySensor(ElroConnectsSensor):
    """Battery sensor for ELRO Connects devices."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def _sensor_type(self) -> str:
        """Return the sensor type identifier."""
        return "battery"

    @property
    def _sensor_name(self) -> str:
        """Return the sensor name suffix."""
        return "Battery"

    @property
    def native_value(self) -> int | None:
        """Return the battery level."""
        if self._device.battery_level >= 0:
            return self._device.battery_level
        return None

    @property
    def icon(self) -> str:
        """Return the icon for the sensor."""
        battery_level = self._device.battery_level
        if battery_level < 0:
            return "mdi:battery-unknown"
        elif battery_level <= 10:
            return "mdi:battery-10"
        elif battery_level <= 20:
            return "mdi:battery-20"
        elif battery_level <= 30:
            return "mdi:battery-30"
        elif battery_level <= 40:
            return "mdi:battery-40"
        elif battery_level <= 50:
            return "mdi:battery-50"
        elif battery_level <= 60:
            return "mdi:battery-60"
        elif battery_level <= 70:
            return "mdi:battery-70"
        elif battery_level <= 80:
            return "mdi:battery-80"
        elif battery_level <= 90:
            return "mdi:battery-90"
        else:
            return "mdi:battery"
