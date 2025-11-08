"""ELRO Connects Device representation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import DEVICE_STATE_UNKNOWN


class ElroDevice:
    """Representation of an ELRO Connects device."""

    def __init__(self, device_id: int, hub_device_id: str | None = None) -> None:
        """Initialize the device."""
        self.id = device_id
        self._hub_device_id = hub_device_id or "hub"  # Store hub device ID
        self.name: str | None = None
        self.device_type: str | None = None
        self.state = DEVICE_STATE_UNKNOWN
        self.battery_level = -1
        self.last_seen: datetime | None = None

    @property
    def unique_id(self) -> str:
        """Return unique ID for this device."""
        return f"elro_realtime_{self.id}"

    @property
    def is_available(self) -> bool:
        """Return True if device is available."""
        if self.last_seen is None:
            return False

        # Consider device unavailable if not seen for more than 5 minutes
        time_diff = datetime.now() - self.last_seen
        return time_diff.total_seconds() < 300

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information for device registry."""
        # Get hub device_id from somewhere - need to pass it to ElroDevice
        # For now, we need to modify ElroDevice to store hub_device_id
        return {
            "identifiers": {("elro_connects_realtime", self.unique_id)},
            "name": self.name or f"ELRO Device {self.id}",
            "manufacturer": "ELRO",
            "model": self._get_model_name(),
            "via_device": ("elro_connects_realtime", self._hub_device_id),
        }

    def _get_model_name(self) -> str:
        """Get human-readable model name from device type."""
        from .const import ElroDeviceTypes

        type_map = {
            ElroDeviceTypes.CO_ALARM: "CO Alarm",
            ElroDeviceTypes.WATER_ALARM: "Water Alarm",
            ElroDeviceTypes.HEAT_ALARM: "Heat Alarm",
            ElroDeviceTypes.FIRE_ALARM: "Fire Alarm",
            ElroDeviceTypes.DOOR_WINDOW_SENSOR: "Door/Window Sensor",
        }

        return type_map.get(str(self.device_type), f"Unknown ({self.device_type})")

    def to_dict(self) -> dict[str, Any]:
        """Convert device to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "device_type": self.device_type,
            "state": self.state,
            "battery_level": self.battery_level,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }

    def __repr__(self) -> str:
        """Return string representation."""
        return f"ElroDevice(id={self.id},\
            name={self.name},\
             type={self.device_type}, state={self.state})"
