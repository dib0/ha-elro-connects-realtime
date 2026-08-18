"""Binary sensor platform for ELRO Connects Real-time."""

from __future__ import annotations

import logging
from typing import Any

from elro_connects_k2_protocol.models import DeviceCapability
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_BATTERY_LEVEL,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_TYPE,
    ATTR_LAST_SEEN,
    DATA_CREATED_UNIQUE_IDS,
    DEVICE_STATE_ALARM,
    DEVICE_STATE_OPEN,
    DOMAIN,
    PROTOCOL_K2,
    ElroDeviceTypes,
)
from .device import ElroDevice
from .k2_hub import TRIGGERED_ALARM_STATES
from .models import ElroHub

_LOGGER = logging.getLogger(__name__)

# Maps the device classes used by the protocol library's device profiles onto
# Home Assistant binary sensor device classes.
_CAPABILITY_DEVICE_CLASSES: dict[str, BinarySensorDeviceClass] = {
    "smoke": BinarySensorDeviceClass.SMOKE,
    "carbon_monoxide": BinarySensorDeviceClass.CO,
    "gas": BinarySensorDeviceClass.GAS,
    "heat": BinarySensorDeviceClass.HEAT,
    "moisture": BinarySensorDeviceClass.MOISTURE,
    "motion": BinarySensorDeviceClass.MOTION,
    "door": BinarySensorDeviceClass.DOOR,
    "window": BinarySensorDeviceClass.WINDOW,
    "opening": BinarySensorDeviceClass.OPENING,
    "vibration": BinarySensorDeviceClass.VIBRATION,
    "problem": BinarySensorDeviceClass.PROBLEM,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ELRO Connects binary sensor platform."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    hub: ElroHub = entry_data["hub"]
    # Shared with the sensor platform; see the note there on why this is per
    # config entry rather than module state.
    created: set[str] = entry_data[DATA_CREATED_UNIQUE_IDS]

    entities = []

    # Create binary sensors for existing devices
    for device in hub.devices.values():
        new_entities = _create_binary_sensors_for_device(device, hub, created)
        for entity in new_entities:
            if entity.unique_id not in created:
                entities.append(entity)
                created.add(entity.unique_id)
            else:
                _LOGGER.debug("Skipping duplicate entity: %s", entity.unique_id)

    if entities:
        async_add_entities(entities, True)
        _LOGGER.info("Created %d binary sensor entities", len(entities))

    # Set up callback for new devices
    def _async_device_updated(device: ElroDevice) -> None:
        """Handle device updates."""
        # Only create entities for devices that have a type (meaning they've received status updates)
        if not device.device_type:
            return

        new_entities = _create_binary_sensors_for_device(device, hub, created)
        entities_to_add = []

        for entity in new_entities:
            if entity.unique_id not in created:
                entities_to_add.append(entity)
                created.add(entity.unique_id)

        if entities_to_add:
            async_add_entities(entities_to_add, True)
            _LOGGER.info(
                "Added %d new binary sensor entities for device %d",
                len(entities_to_add),
                device.id,
            )

    hub.add_device_update_callback(_async_device_updated)


def _create_binary_sensors_for_device(
    device: ElroDevice, hub: ElroHub, created: set[str]
) -> list[ElroConnectsBinarySensor]:
    """Create binary sensors for a device based on its type."""
    entities: list[ElroConnectsBinarySensor] = []

    # Only create entities if device has a type
    if not device.device_type:
        return entities

    # K2: the protocol library resolved a device profile, which lists exactly
    # which hazards this device reports. No type-code branching needed.
    if device.protocol == PROTOCOL_K2:
        return [
            ElroConnectsCapabilitySensor(device, hub, created, capability)
            for capability in device.capabilities
            if capability.entity_type == "binary_sensor"
        ]

    if device.device_type == ElroDeviceTypes.DOOR_WINDOW_SENSOR:
        entities.append(ElroConnectsDoorWindowSensor(device, hub, created))
    elif device.device_type in [
        ElroDeviceTypes.CO_ALARM,
        ElroDeviceTypes.WATER_ALARM,
        ElroDeviceTypes.HEAT_ALARM,
        ElroDeviceTypes.FIRE_ALARM,
    ]:
        entities.append(ElroConnectsAlarmSensor(device, hub, created))

    return entities


class ElroConnectsBinarySensor(BinarySensorEntity):
    """Base class for ELRO Connects binary sensors."""

    def __init__(self, device: ElroDevice, hub: ElroHub, created: set[str]) -> None:
        """Initialize the binary sensor."""
        self._device = device
        self._hub = hub
        self._created = created
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
        # Always register the callback when entity is added
        self._hub.add_device_update_callback(self._async_device_updated)

        # If device already exists, trigger an immediate update
        if self._device.id in self._hub.devices:
            self._device = self._hub.devices[self._device.id]
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed from hass."""
        self._hub.remove_device_update_callback(self._async_device_updated)
        # Untrack so the entity can be recreated if the device comes back.
        if self.unique_id is not None:
            self._created.discard(self.unique_id)

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
        # Handle None device_type safely
        device_type = self._device.device_type or ""
        return device_type_map.get(device_type, "Alarm")

    @property
    def is_on(self) -> bool:
        """Return true if alarm is triggered."""
        return self._device.state == DEVICE_STATE_ALARM


class ElroConnectsCapabilitySensor(ElroConnectsBinarySensor):
    """Binary sensor for one capability of a K2 device.

    Which capabilities a device has comes from the device profile registry in
    the elro-connects-k2-protocol library, so a smoke/CO combi detector gets one
    entity per hazard instead of a single generic "alarm" entity.
    """

    def __init__(
        self,
        device: ElroDevice,
        hub: ElroHub,
        created: set[str],
        capability: DeviceCapability,
    ) -> None:
        """Initialize the capability sensor."""
        self._capability = capability
        super().__init__(device, hub, created)
        self._attr_device_class = _CAPABILITY_DEVICE_CLASSES.get(
            capability.device_class
        )

    @property
    def _sensor_type(self) -> str:
        """Return the sensor type identifier."""
        # str() so the annotation holds even where the library is unavailable
        # for type checking (it is a Home Assistant runtime requirement).
        return str(self._capability.key)

    @property
    def _sensor_name(self) -> str:
        """Return the sensor name suffix."""
        return str(self._capability.label)

    @property
    def is_on(self) -> bool | None:
        """Return true if this capability is reporting its active state."""
        # Thermostats repurpose the alarm byte, so the library decodes those
        # bits into dedicated fields instead.
        if self._capability.key == "valve":
            return self._device.valve_open
        if self._capability.key == "window":
            return self._device.window_open
        if self._device.alarm_state is None:
            return None
        return self._device.alarm_state in TRIGGERED_ALARM_STATES

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attrs = super().extra_state_attributes
        attrs["alarm_state"] = self._device.alarm_state
        attrs["raw_status"] = self._device.raw_status
        return attrs
