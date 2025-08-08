"""ELRO Connects Hub communication."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_PORT,
    DEVICE_STATE_ALARM,
    DEVICE_STATE_CLOSED,
    DEVICE_STATE_NORMAL,
    DEVICE_STATE_OPEN,
    DEVICE_STATE_UNKNOWN,
    ElroCommands,
    ElroDeviceTypes,
)
from .device import ElroDevice

_LOGGER = logging.getLogger(__name__)


class ElroConnectsHub:
    """Class to communicate with ELRO Connects hub."""

    def __init__(
        self,
        host: str,
        device_id: str,
        hass: HomeAssistant,
        ctrl_key: str = "0",
        app_id: str = "0",
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize the hub."""
        self._host = host
        self._port = port
        self._device_id = device_id
        self._ctrl_key = ctrl_key
        self._app_id = app_id
        self._hass = hass

        self._socket: socket.socket | None = None
        self._msg_id = 0
        self._devices: dict[int, ElroDevice] = {}
        self._running = False
        self._receive_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_data_received = datetime.now()

        self._device_update_callbacks: list[Callable[[ElroDevice], None]] = []

    @property
    def devices(self) -> dict[int, ElroDevice]:
        """Return all devices."""
        return self._devices

    def add_device_update_callback(
        self, callback: Callable[[ElroDevice], None]
    ) -> None:
        """Add a callback for device updates."""
        self._device_update_callbacks.append(callback)

    def remove_device_update_callback(
        self, callback: Callable[[ElroDevice], None]
    ) -> None:
        """Remove a callback for device updates."""
        if callback in self._device_update_callbacks:
            self._device_update_callbacks.remove(callback)

    async def async_start(self) -> None:
        """Start the hub connection."""
        if self._running:
            return

        self._running = True

        # Create UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setblocking(False)

        try:
            # Start connection
            await self._async_send_data(f"IOT_KEY?{self._device_id}")

            # Start receive task
            self._receive_task = asyncio.create_task(self._async_receive_data())

            # Start heartbeat task
            self._heartbeat_task = asyncio.create_task(self._async_heartbeat())

            # Request initial device status
            await self.async_sync_device_status()
            await self.async_get_device_names()

            _LOGGER.info("ELRO Connects hub started successfully")

        except Exception as ex:
            _LOGGER.error("Failed to start ELRO Connects hub: %s", ex)
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        """Stop the hub connection."""
        self._running = False

        # Cancel tasks
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Close socket
        if self._socket:
            self._socket.close()
            self._socket = None

        _LOGGER.info("ELRO Connects hub stopped")

    async def _async_send_data(self, data: str) -> None:
        """Send data to the hub."""
        if not self._socket:
            raise RuntimeError("Socket not initialized")

        try:
            await self._hass.async_add_executor_job(
                self._socket.sendto, data.encode("utf-8"), (self._host, self._port)
            )
            _LOGGER.debug("Sent: %s", data)
        except Exception as ex:
            _LOGGER.error("Error sending data: %s", ex)
            raise

    async def _async_receive_data(self) -> None:
        """Receive data from the hub."""
        while self._running:
            try:
                if not self._socket:
                    _LOGGER.error("Socket is None during receive")
                    break

                data, _ = await self._hass.async_add_executor_job(
                    self._socket.recvfrom, 4096
                )

                reply = data.decode("utf-8").strip()
                _LOGGER.debug("Received: %s", reply)

                self._last_data_received = datetime.now()

                if reply.startswith("{") and reply != "{ST_answer_OK}":
                    try:
                        msg = json.loads(reply)
                        await self._async_handle_message(msg)
                        # Send acknowledgment
                        await self._async_send_data("APP_answer_OK")
                    except json.JSONDecodeError as ex:
                        _LOGGER.error("Failed to parse JSON message: %s", ex)

            except socket.error as ex:
                if self._running:
                    _LOGGER.error("Socket error: %s", ex)
                    break
            except Exception as ex:
                _LOGGER.error("Error receiving data: %s", ex)
                if self._running:
                    break

    async def _async_handle_message(self, msg: dict[str, Any]) -> None:
        """Handle received message."""
        if "params" not in msg or "data" not in msg["params"]:
            return

        data = msg["params"]["data"]
        cmd_id = data.get("cmdId")

        if cmd_id == ElroCommands.DEVICE_STATUS_UPDATE:
            await self._async_handle_device_status_update(data)
        elif cmd_id == ElroCommands.DEVICE_ALARM_TRIGGER:
            await self._async_handle_device_alarm_trigger(data)
        elif cmd_id == ElroCommands.DEVICE_NAME_REPLY:
            await self._async_handle_device_name_reply(data)

    async def _async_handle_device_status_update(self, data: dict[str, Any]) -> None:
        """Handle device status update."""
        if data.get("device_name") == "STATUES":
            return

        device_id = data.get("device_ID")
        if device_id is None:
            return

        device = self._get_or_create_device(device_id)
        device.device_type = data.get("device_name")

        # Parse device status
        device_status = data.get("device_status", "")
        if len(device_status) >= 4:
            # Battery level
            try:
                battery_level = int(device_status[2:4], 16)
                device.battery_level = battery_level
            except ValueError:
                pass

            # Device state
            if len(device_status) >= 6:
                status_code = (
                    device_status[4:-2] if len(device_status) > 6 else device_status[4:]
                )

                if device.device_type == ElroDeviceTypes.DOOR_WINDOW_SENSOR:
                    device.state = (
                        DEVICE_STATE_CLOSED
                        if status_code == "AA"
                        else DEVICE_STATE_OPEN
                    )
                else:
                    if status_code == "BB":
                        device.state = DEVICE_STATE_ALARM
                    elif status_code == "AA":
                        device.state = DEVICE_STATE_NORMAL
                    else:
                        device.state = DEVICE_STATE_UNKNOWN

        device.last_seen = datetime.now()
        await self._async_notify_device_update(device)

    async def _async_handle_device_alarm_trigger(self, data: dict[str, Any]) -> None:
        """Handle device alarm trigger."""
        answer_content = data.get("answer_content", "")
        if len(answer_content) >= 10:
            try:
                device_id = int(answer_content[6:10], 16)
                device = self._get_or_create_device(device_id)
                device.state = DEVICE_STATE_ALARM
                device.last_seen = datetime.now()

                _LOGGER.warning(
                    "ALARM! Device ID %d (%s)", device_id, device.name or "Unknown"
                )
                await self._async_notify_device_update(device)
            except ValueError:
                pass

    async def _async_handle_device_name_reply(self, data: dict[str, Any]) -> None:
        """Handle device name reply."""
        answer_content = data.get("answer_content", "")
        if answer_content == "NAME_OVER" or len(answer_content) < 36:
            return

        try:
            device_id = int(answer_content[0:4], 16)
            name_hex = answer_content[4:36]  # 32 hex chars

            # Convert hex to ASCII name
            name = self._hex_to_string(name_hex)
            if name:
                device = self._get_or_create_device(device_id)
                device.name = name
                device.last_seen = datetime.now()
                await self._async_notify_device_update(device)
        except ValueError:
            pass

    def _hex_to_string(self, hex_input: str) -> str:
        """Convert hex string to ASCII string (from original coder_utils)."""
        try:
            if len(hex_input) != 32:
                return ""

            byte_data = bytes.fromhex(hex_input)
            name = "".join(chr(b) for b in byte_data if b != 0)
            name = name.replace("@", "").replace("$", "")
            return name
        except Exception:
            return ""

    def _get_or_create_device(self, device_id: int) -> ElroDevice:
        """Get existing device or create new one."""
        if device_id not in self._devices:
            self._devices[device_id] = ElroDevice(device_id)
        return self._devices[device_id]

    async def _async_notify_device_update(self, device: ElroDevice) -> None:
        """Notify callbacks of device update."""
        for callback in self._device_update_callbacks:
            try:
                callback(device)
            except Exception as ex:
                _LOGGER.error("Error in device update callback: %s", ex)

    def _construct_message(self, data: str) -> str:
        """Construct message with proper format."""
        self._msg_id += 1
        return json.dumps(
            {
                "msgId": self._msg_id,
                "action": "appSend",
                "params": {
                    "devTid": self._device_id,
                    "ctrlKey": self._ctrl_key,
                    "appTid": self._app_id,
                    "data": json.loads(data),
                },
            }
        )

    async def async_sync_device_status(self) -> None:
        """Sync device status."""
        data = json.dumps(
            {"cmdId": ElroCommands.SYN_DEVICE_STATUS, "device_status": ""}
        )
        msg = self._construct_message(data)
        await self._async_send_data(msg)

    async def async_sync_devices(self) -> None:
        """Get all device status."""
        data = json.dumps(
            {"cmdId": ElroCommands.GET_ALL_EQUIPMENT_STATUS, "device_status": ""}
        )
        msg = self._construct_message(data)
        await self._async_send_data(msg)

    async def async_get_device_names(self) -> None:
        """Get device names."""
        data = json.dumps({"cmdId": ElroCommands.GET_DEVICE_NAME, "device_ID": 0})
        msg = self._construct_message(data)
        await self._async_send_data(msg)

    async def async_test_device_alarm(self, device_id: int) -> None:
        """Test device alarm."""
        device = self._devices.get(device_id)
        if not device:
            return

        payload = "BB000000"
        if device.device_type == ElroDeviceTypes.FIRE_ALARM:
            payload = "17000000"

        data = json.dumps(
            {
                "cmdId": ElroCommands.EQUIPMENT_CONTROL,
                "device_ID": device_id,
                "device_status": payload,
            }
        )
        msg = self._construct_message(data)
        await self._async_send_data(msg)

    async def _async_heartbeat(self) -> None:
        """Heartbeat to maintain connection."""
        while self._running:
            try:
                # Wait for 30 seconds
                await asyncio.sleep(30)

                if not self._running:
                    break

                # Check if we received data recently
                time_since_last_data = datetime.now() - self._last_data_received
                if time_since_last_data > timedelta(minutes=1):
                    _LOGGER.warning(
                        "No data received for %s, reconnecting", time_since_last_data
                    )
                    # Restart connection
                    await self._async_send_data(f"IOT_KEY?{self._device_id}")
                    await self.async_sync_device_status()

                # Sync devices periodically
                await self.async_sync_devices()

            except asyncio.CancelledError:
                break
            except Exception as ex:
                _LOGGER.error("Error in heartbeat: %s", ex)
                if self._running:
                    await asyncio.sleep(5)
