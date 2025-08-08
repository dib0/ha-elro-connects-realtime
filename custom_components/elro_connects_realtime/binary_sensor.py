"""Binary sensor platform for ELRO Connects Real-time."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_BATTERY_LEVEL,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_TYPE,
    ATTR_LAST_SEEN,
    DEVICE_STATE_ALARM,
    DEVICE_STATE_OPEN,
    DOMAIN,
    ElroDeviceTypes,
)
from .device import ElroDevice
from .hub import ElroConnectsHub

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ELRO Connects binary sensor platform."""
    hub: ElroConnectsHub = hass.data[DOMAIN][config_entry.entry_id]["hub"]

    entities = []

    # Create binary sensors for existing devices
    for device in hub.devices.values():
        entities.extend(_create_binary_sensors_for_device(device, hub))

    if entities:
        async_add_entities(entities, True)

    # Set up callback for new devices
    @callback
    def _async_device_updated(device: ElroDevice) -> None:
        """Handle device updates."""
        # Check if we need to create new entities for this device
        existing_entities = [
            entity
            for entity in hass.data.get(f"{DOMAIN}_entities", [])
            if getattr(entity, "_device_id", None) == device.id
        ]

        if not existing_entities:
            new_entities = _create_binary_sensors_for_device(device, hub)
            if new_entities:
                async_add_entities(new_entities, True)
                # Store entities for tracking
                if f"{DOMAIN}_entities" not in hass.data:
                    hass.data[f"{DOMAIN}_entities"] = []
                hass.data[f"{DOMAIN}_entities"].extend(new_entities)

    hub.add_device_update_callback(_async_device_updated)


def _create_binary_sensors_for_device(
    device: ElroDevice, hub: ElroConnectsHub
) -> list[ElroConnectsBinarySensor]:
    """Create binary sensors for a device based on its type."""
    entities = []

    if device.device_type == ElroDeviceTypes.DOOR_WINDOW_SENSOR:
        entities.append(ElroConnectsDoorWindowSensor(device, hub))
    elif device.device_type in [
        ElroDeviceTypes.CO_ALARM,
        ElroDeviceTypes.WATER_ALARM,
        ElroDeviceTypes.HEAT_ALARM,
        ElroDeviceTypes.FIRE_ALARM,
    ]:
        entities.append(ElroConnectsAlarmSensor(device, hub))

    return entities


class ElroConnectsBinarySensor(BinarySensorEntity):
    """Base class for ELRO Connects binary sensors."""

    def __init__(self, device: ElroDevice, hub: ElroConnectsHub) -> None:
        """Initialize the binary sensor."""
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

        if self._device.battery_level >= 0:
            attrs[ATTR_BATTERY_LEVEL] = self._device.battery_level

        if self._device.last_seen:
            attrs[ATTR_LAST_SEEN] = self._device.last_seen.isoformat()

        return attrs

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        self._hub.add_device_update_callback(self._async_device_updated)

    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed from hass."""
        self._hub.remove_device_update_callback(self._async_device_updated)

    @callback
    def _async_device_updated(self, device: ElroDevice) -> None:
        """Handle device updates."""
        if device.id == self._device.id:
            self._device = device
            self.async_write_ha_state()


class ElroConnectsDoorWindowSensor(ElroConnectsBinarySensor):
    """Door/Window sensor for ELRO Connects."""

    _attr_device_class = BinarySensorDeviceClass.DOOR

    @property
    def _sensor_type(self) -> str:
        """Return the sensor type identifier."""
        return "door_window"

    @property
    def _sensor_name(self) -> str:
        """Return the sensor name suffix."""
        return "Door/Window"

    @property
    def is_on(self) -> bool:
        """Return true if the door/window is open."""
        return self._device.state == DEVICE_STATE_OPEN


class ElroConnectsAlarmSensor(ElroConnectsBinarySensor):
    """Alarm sensor for ELRO Connects devices."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    @property
    def _sensor_type(self) -> str:
        """Return the sensor type identifier."""
        return "alarm"

    @property
    def _sensor_name(self) -> str:
        """Return the sensor name suffix."""
        device_type_map = {
            ElroDeviceTypes.CO_ALARM: "CO Alarm",
            ElroDeviceTypes.WATER_ALARM: "Water Alarm",
            ElroDeviceTypes.HEAT_ALARM: "Heat Alarm",
            ElroDeviceTypes.FIRE_ALARM: "Fire Alarm",
        }
        return device_type_map.get(self._device.device_type, "Alarm")

    @property
    def is_on(self) -> bool:
        """Return true if alarm is triggered."""
        return self._device.state == DEVICE_STATE_ALARM
