"""ELRO Connects Hub communication with K1/K2 protocol support and enhanced reliability."""

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
from .k2_codec import K2Codec

_LOGGER = logging.getLogger(__name__)


class ElroConnectsHub:
    """Class to communicate with ELRO Connects hub with K1/K2 support."""

    def __init__(
        self,
        host: str,
        device_id: str,
        hass: HomeAssistant,
        ctrl_key: str = "0",
        app_id: str = "0",
        port: int = DEFAULT_PORT,
        force_protocol: str | None = None,
    ) -> None:
        """Initialize the hub."""
        self._host = host
        self._port = port
        self._device_id = device_id
        self._ctrl_key = ctrl_key
        self._app_id = app_id
        self._hass = hass
        self._force_protocol = force_protocol

        self._socket: socket.socket | None = None
        self._msg_id = 0
        self._devices: dict[int, ElroDevice] = {}
        self._running = False
        self._receive_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._periodic_reset_task: asyncio.Task[None] | None = None
        self._last_data_received = datetime.now()
        self._last_connection_reset = datetime.now()
        self._connection_issues = 0

        self._device_update_callbacks: list[Callable[[ElroDevice], None]] = []
        self._reloading = False  # Track if we're in reload mode

        # Protocol detection
        self._detected_protocol: str | None = None
        self._use_k2 = False

        # Auto-detect protocol unless forced
        if force_protocol and force_protocol.upper() in ["K1", "K2"]:
            self._detected_protocol = force_protocol.upper()
            self._use_k2 = force_protocol.upper() == "K2"
            _LOGGER.info("Forced protocol: %s", self._detected_protocol)
        else:
            _LOGGER.info("Protocol will be auto-detected from hub responses")

    @property
    def devices(self) -> dict[int, ElroDevice]:
        """Return all devices."""
        return self._devices

    @property
    def protocol(self) -> str:
        """Return current protocol."""
        if self._force_protocol:
            return self._force_protocol.upper()
        if self._detected_protocol:
            return self._detected_protocol
        return "UNKNOWN"

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
        """Start the hub connection."""
        if self._running:
            return

        self._running = True
        self._reloading = False
        self._last_connection_reset = datetime.now()

        try:
            await self._async_connect()

            # Start receive task
            self._receive_task = asyncio.create_task(self._async_receive_data())

            # Start heartbeat task
            self._heartbeat_task = asyncio.create_task(self._async_heartbeat())

            # Start periodic reset task (every 4 hours)
            self._periodic_reset_task = asyncio.create_task(
                self._async_periodic_reset()
            )

            # Request initial device status with better sequencing
            await asyncio.sleep(2)  # Give connection time to establish
            await self.async_get_device_names()  # Get names first
            await asyncio.sleep(1)  # Wait for names to be received
            await self.async_sync_devices()  # Then get status/types
            await asyncio.sleep(1)  # Wait for status updates
            await self.async_sync_device_status()  # Finally sync current states

            _LOGGER.info(
                "ELRO Connects hub started successfully (Protocol: %s)", self.protocol
            )

        except Exception as ex:
            _LOGGER.error("Failed to start ELRO Connects hub: %s", ex)
            await self.async_stop()
            raise

    async def _async_connect(self) -> None:
        """Establish connection to the hub."""
        # Close existing socket if any
        if self._socket:
            self._socket.close()
            self._socket = None

        # Create new UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setblocking(False)

        _LOGGER.info("Connecting to ELRO hub at %s:%d", self._host, self._port)

        # Send initial connection request (always K1 format)
        await self._async_send_data_raw(f"IOT_KEY?{self._device_id}")

        # Reset connection tracking
        self._last_data_received = datetime.now()
        self._connection_issues = 0

    async def async_stop(self) -> None:
        """Stop the hub connection."""
        _LOGGER.info("Stopping ELRO Connects hub (reloading: %s)", self._reloading)
        self._running = False

        # Cancel all tasks
        for task in [
            self._receive_task,
            self._heartbeat_task,
            self._periodic_reset_task,
        ]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._receive_task = None
        self._heartbeat_task = None
        self._periodic_reset_task = None

        # Close socket
        if self._socket:
            self._socket.close()
            self._socket = None

        # Only clear callbacks if not reloading
        if not self._reloading:
            self._device_update_callbacks.clear()
            _LOGGER.info("ELRO Connects hub stopped and callbacks cleared")
        else:
            _LOGGER.info("ELRO Connects hub stopped (callbacks preserved for reload)")

    async def async_reload_safe(self) -> None:
        """Safely reload the connection without losing device state."""
        _LOGGER.info("Safely reloading ELRO Connects hub connection")
        self._reloading = True

        # Keep device state and callbacks but reset connection
        await self._async_reconnect()

        # Refresh device data
        await asyncio.sleep(1)
        await self.async_sync_device_status()
        await self.async_get_device_names()

        # Force update all devices to refresh entity states
        await self._refresh_all_devices()

        self._reloading = False
        _LOGGER.info("Safe reload completed")

    async def _refresh_all_devices(self) -> None:
        """Force refresh all device states to update entities."""
        _LOGGER.info("Refreshing %d devices", len(self._devices))
        for device in self._devices.values():
            # Mark as recently seen to ensure availability
            device.last_seen = datetime.now()
            await self._async_notify_device_update(device)
        _LOGGER.info("Device refresh completed")

    async def _async_reconnect(self) -> None:
        """Reconnect to the hub while preserving state."""
        _LOGGER.info("Reconnecting to ELRO hub")

        # Cancel only receive task to stop current connection
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # Establish new connection
        await self._async_connect()

        # Restart receive task
        self._receive_task = asyncio.create_task(self._async_receive_data())

        self._last_connection_reset = datetime.now()
        _LOGGER.info("Reconnection completed")

    async def _async_periodic_reset(self) -> None:
        """Periodically reset connection every 4 hours."""
        while self._running:
            try:
                # Wait for 4 hours (14400 seconds) but check running state periodically
                for _ in range(480):  # 480 * 30 seconds = 4 hours
                    if not self._running:
                        return  # type: ignore[unreachable]
                    await asyncio.sleep(30)

                if self._running:  # Double-check we're still running
                    _LOGGER.info("Performing scheduled 4-hour connection reset")
                    await self._async_reconnect()

            except asyncio.CancelledError:
                break
            except Exception as ex:
                _LOGGER.error("Error in periodic reset: %s", ex)
                # Wait 1 minute before retry, but check running state every 5 seconds
                for _ in range(12):  # 12 * 5 seconds = 1 minute
                    if not self._running:
                        return  # type: ignore[unreachable]
                    await asyncio.sleep(5)

    async def _async_send_data_raw(self, data: str) -> None:
        """Send raw data to the hub (for IOT_KEY queries)."""
        if not self._socket:
            raise RuntimeError("Socket not initialized")

        try:
            await self._hass.async_add_executor_job(
                self._send_data_sync, data.encode("utf-8")
            )
            _LOGGER.debug("Sent raw to %s:%d: %s", self._host, self._port, data)
        except Exception as ex:
            _LOGGER.error("Error sending data to %s:%d: %s", self._host, self._port, ex)
            self._connection_issues += 1
            if self._connection_issues >= 3:
                _LOGGER.warning("Multiple send failures, reconnecting")
                await self._async_reconnect()
            raise

    async def _async_send_data(self, data: str) -> None:
        """Send data to the hub using appropriate protocol (K1 or K2)."""
        if not self._socket:
            raise RuntimeError("Socket not initialized")

        try:
            if self._use_k2:
                # K2: Encode as encrypted binary
                try:
                    json_data = json.loads(data)
                    encoded_data = K2Codec.encode_k2_message(json_data)
                    _LOGGER.debug(
                        "Sending K2 message (%d bytes): %s...",
                        len(encoded_data),
                        encoded_data.hex()[:60],
                    )
                except Exception as ex:
                    _LOGGER.error("Failed to encode K2 message: %s", ex)
                    raise
            else:
                # K1: Send as plain JSON string
                encoded_data = data.encode("utf-8")
                _LOGGER.debug("Sending K1 message: %s", data[:100])

            await self._hass.async_add_executor_job(self._send_data_sync, encoded_data)

        except Exception as ex:
            _LOGGER.error("Error sending data to %s:%d: %s", self._host, self._port, ex)
            self._connection_issues += 1
            if self._connection_issues >= 3:
                _LOGGER.warning("Multiple send failures, reconnecting")
                await self._async_reconnect()
            raise

    def _send_data_sync(self, data: bytes) -> None:
        """Send data synchronously with proper error handling."""
        if not self._socket:
            raise RuntimeError("Socket not initialized")

        try:
            self._socket.setblocking(True)
            self._socket.settimeout(5.0)  # 5 second timeout for sending
            self._socket.sendto(data, (self._host, self._port))
        finally:
            self._socket.setblocking(False)

    async def _async_receive_data(self) -> None:
        """Receive data from the hub with K1/K2 auto-detection."""
        consecutive_errors = 0

        while self._running:
            try:
                if not self._socket:
                    _LOGGER.error("Socket is None during receive")
                    break

                try:
                    data, addr = await self._hass.async_add_executor_job(
                        self._receive_with_timeout
                    )
                    consecutive_errors = 0
                except socket.timeout:
                    continue
                except BlockingIOError:
                    await asyncio.sleep(0.1)
                    continue

                self._last_data_received = datetime.now()
                self._connection_issues = 0

                # Try to decode as plain JSON first (both K1 and K2 send JSON responses)
                try:
                    reply = data.decode("utf-8").strip()
                    _LOGGER.debug("Received message from %s: %s", addr, reply[:100])

                    # Parse JSON
                    if reply.startswith("{"):
                        msg = json.loads(reply)

                        # Detect protocol by message structure
                        if (
                            "action" in msg
                            and "msg" in msg
                            and "CMD_CODE" in msg.get("msg", {})
                        ):
                            # K2 structure detected
                            # Check if this is NODE_ACK (initial handshake response)
                            if msg.get("action") == "NODE_ACK":
                                # NODE_ACK from primary hub - this hub speaks K2
                                if (
                                    not self._detected_protocol
                                    and not self._force_protocol
                                ):
                                    if addr[0] == self._host:
                                        # Response from target hub = pure K2 hub
                                        self._detected_protocol = "K2"
                                        self._use_k2 = True
                                        _LOGGER.info(
                                            "🔍 Pure K2 hub detected at %s", addr[0]
                                        )
                                    else:
                                        # Response from different IP = mixed protocol
                                        self._detected_protocol = "K1 (mixed)"
                                        _LOGGER.info(
                                            "🔍 Mixed protocol: K2 hub at %s, K1 devices from %s",
                                            self._host,
                                            addr[0],
                                        )

                            await self._async_handle_message(msg)
                            # Send K2 acknowledgment (encrypted)
                            ack_msg = {"action": "APP_ACK"}
                            ack_data = K2Codec.encode_k2_message(ack_msg)
                            await self._hass.async_add_executor_job(
                                self._send_data_sync, ack_data
                            )

                        elif "params" in msg and "data" in msg["params"]:
                            # K1 structure
                            if not self._detected_protocol and not self._force_protocol:
                                self._detected_protocol = "K1"
                                self._use_k2 = False
                                _LOGGER.info("🔍 Protocol auto-detected: K1")

                            await self._async_handle_message(msg)
                            await self._async_send_data_raw("APP_answer_OK")

                        elif reply == "{ST_answer_OK}":
                            _LOGGER.debug("Received connection acknowledgment")
                        else:
                            await self._async_handle_message(msg)

                    else:
                        _LOGGER.debug("Received non-JSON message: %s", reply)

                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Binary data - try K2 decoding
                    decoded_json = K2Codec.decode_k2_message(data)
                    if decoded_json:
                        _LOGGER.debug(
                            "Decoded K2 binary message: %s", str(decoded_json)[:100]
                        )
                        await self._async_handle_message(decoded_json)
                    else:
                        _LOGGER.warning(
                            "Failed to decode message (%d bytes): %s",
                            len(data),
                            data.hex()[:60],
                        )

            except OSError as ex:
                if ex.errno == 11:
                    await asyncio.sleep(0.1)
                    continue
                else:
                    consecutive_errors += 1
                    _LOGGER.error(
                        "Socket OS error (count: %d): %s", consecutive_errors, ex
                    )
                    if consecutive_errors >= 5:
                        _LOGGER.error("Too many consecutive errors, reconnecting")
                        await self._async_reconnect()
                        consecutive_errors = 0
                    else:
                        await asyncio.sleep(1)
            except Exception as ex:
                consecutive_errors += 1
                _LOGGER.error(
                    "Error receiving data (count: %d): %s", consecutive_errors, ex
                )
                if consecutive_errors >= 5:
                    _LOGGER.error("Too many consecutive errors, reconnecting")
                    await self._async_reconnect()
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(1)

    def _receive_with_timeout(self) -> tuple[bytes, tuple[str, int]]:
        """Receive data with timeout handling."""
        if not self._socket:
            raise RuntimeError("Socket not initialized")

        self._socket.settimeout(1.0)  # 1 second timeout
        try:
            return self._socket.recvfrom(4096)
        except socket.timeout:
            raise
        except BlockingIOError as ex:
            raise socket.timeout("No data available") from ex
        finally:
            self._socket.setblocking(False)

    async def _async_handle_message(self, msg: dict[str, Any]) -> None:
        """Handle received message (K1 or K2 format)."""
        # K1 format: {"params": {"data": {...}}}
        # K2 format: {"action": "NODE_SEND", "devID": "...", "msg": {...}}

        # Try K1 format first
        if "params" in msg and "data" in msg["params"]:
            data = msg["params"]["data"]
            cmd_id = data.get("cmdId")
            _LOGGER.debug("Handling K1 message with cmdId: %s", cmd_id)

            if cmd_id == ElroCommands.DEVICE_STATUS_UPDATE:
                await self._async_handle_device_status_update(data)
            elif cmd_id == ElroCommands.DEVICE_ALARM_TRIGGER:
                await self._async_handle_device_alarm_trigger(data)
            elif cmd_id == ElroCommands.DEVICE_NAME_REPLY:
                await self._async_handle_device_name_reply(data)

        # Try K2 format
        elif "action" in msg and "msg" in msg:
            action = msg.get("action")
            inner_msg = msg.get("msg", {})
            cmd_code = inner_msg.get("CMD_CODE")

            _LOGGER.debug(
                "Handling K2 message with action: %s, CMD_CODE: %s", action, cmd_code
            )

            # Map K2 command codes to handlers
            if action in ["NODE_SEND", "APP_SEND"]:
                _LOGGER.debug("K2 message content: %s", inner_msg)
                if cmd_code == 17:  # UPLOAD_DEVICE_NAME
                    await self._async_handle_k2_device_name(inner_msg)
                elif cmd_code == 19:  # UPLOAD_DEVICE_STATUS
                    _LOGGER.debug("Calling K2 device status handler (CMD 19)")
                    await self._async_handle_k2_device_status(inner_msg)
                elif cmd_code == 55:  # Alternative status format
                    _LOGGER.debug("Calling K2 device status handler (CMD 55)")
                    await self._async_handle_k2_device_status(inner_msg)
                else:
                    _LOGGER.debug("Unhandled K2 CMD_CODE: %s", cmd_code)

    async def _async_handle_k2_device_name(self, msg: dict[str, Any]) -> None:
        """Handle K2 device name message."""
        rev_str1 = msg.get("rev_str1", "") or msg.get("data_str1", "")
        rev_str2 = msg.get("rev_str2", "") or msg.get("data_str2", "")

        if not rev_str1 or not rev_str2:
            return

        try:
            if len(rev_str1) >= 4:
                device_id = int(rev_str1[:4], 16)
                if len(rev_str2) >= 32:
                    name = self._hex_to_string(rev_str2[:32])
                    if name:
                        device = self._get_or_create_device(device_id)
                        device.name = name
                        device.last_seen = datetime.now()
                        _LOGGER.info("K2: Device %d name: %s", device_id, name)
                        await self._async_notify_device_update(device)
        except ValueError as ex:
            _LOGGER.debug("Could not parse K2 device name: %s", ex)

    async def _async_handle_k2_device_status(self, msg: dict[str, Any]) -> None:
        """Handle K2 device status message."""
        cmd_code = msg.get("CMD_CODE")
        data_str1 = msg.get("rev_str1") or msg.get("data_str1", "")
        data_str2 = msg.get("rev_str2") or msg.get("data_str2", "")

        _LOGGER.debug(
            "K2 status (CMD %s): data_str1='%s', data_str2='%s'",
            cmd_code,
            data_str1,
            data_str2,
        )

        if not data_str1:
            _LOGGER.debug("K2 status missing data_str1")
            return

        try:
            # CMD_CODE 55: Combined format - all data in data_str1, empty data_str2
            if cmd_code == 55 and not data_str2 and len(data_str1) >= 14:
                # Format: DDTTPPPPBBSS (14 chars)
                # Example: 0100034064AA00
                #   01 = device ID
                #   00 = padding
                #   03 = padding
                #   4064 = device type
                #   AA = battery (170 decimal)
                #   00 = state

                device_id = int(data_str1[:2], 16)
                device = self._get_or_create_device(device_id)

                # Type at chars 6-9
                device.device_type = data_str1[6:10]

                # Battery at chars 10-11
                device.battery_level = int(data_str1[10:12], 16)

                # State at chars 12-13
                status_code = data_str1[12:14]
                if device.device_type == ElroDeviceTypes.DOOR_WINDOW_SENSOR:
                    device.state = (
                        DEVICE_STATE_CLOSED
                        if status_code in ["AA", "00"]
                        else DEVICE_STATE_OPEN
                    )
                else:
                    device.state = (
                        DEVICE_STATE_ALARM
                        if status_code == "BB"
                        else (
                            DEVICE_STATE_NORMAL
                            if status_code in ["AA", "00"]
                            else DEVICE_STATE_UNKNOWN
                        )
                    )

                device.last_seen = datetime.now()
                _LOGGER.info(
                    "K2 (CMD 55): Device %d: type=%s, battery=%d%%, state=%s",
                    device_id,
                    device.device_type,
                    device.battery_level,
                    device.state,
                )
                await self._async_notify_device_update(device)
                return

            # Standard format: data_str1=device_id (4 chars), data_str2=type+battery+state
            if data_str2 and len(data_str1) >= 4:
                device_id = int(data_str1[:4], 16)
                device = self._get_or_create_device(device_id)

                if len(data_str2) >= 4:
                    device.device_type = data_str2[:4]
                if len(data_str2) >= 6:
                    device.battery_level = int(data_str2[4:6], 16)
                if len(data_str2) >= 8:
                    status_code = data_str2[6:8]
                    if device.device_type == ElroDeviceTypes.DOOR_WINDOW_SENSOR:
                        device.state = (
                            DEVICE_STATE_CLOSED
                            if status_code == "AA"
                            else DEVICE_STATE_OPEN
                        )
                    else:
                        device.state = (
                            DEVICE_STATE_ALARM
                            if status_code == "BB"
                            else (
                                DEVICE_STATE_NORMAL
                                if status_code == "AA"
                                else DEVICE_STATE_UNKNOWN
                            )
                        )

                device.last_seen = datetime.now()
                _LOGGER.info(
                    "K2 (CMD %s): Device %d: type=%s, battery=%d%%, state=%s",
                    cmd_code,
                    device_id,
                    device.device_type,
                    device.battery_level,
                    device.state,
                )
                await self._async_notify_device_update(device)

            # Fallback: Just create device so it exists
            elif not data_str2 and len(data_str1) >= 4:
                device_id = int(data_str1[:4], 16)
                device = self._get_or_create_device(device_id)
                device.last_seen = datetime.now()
                _LOGGER.info(
                    "K2 (CMD %s): Device %d seen (incomplete data)", cmd_code, device_id
                )
                await self._async_notify_device_update(device)

        except (ValueError, IndexError) as ex:
            _LOGGER.error(
                "K2 parse error (CMD %s): %s (data_str1=%s, data_str2=%s)",
                cmd_code,
                ex,
                data_str1,
                data_str2,
            )

    async def _async_handle_device_status_update(self, data: dict[str, Any]) -> None:
        """Handle K1 device status update."""
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
        _LOGGER.debug("K1: Updated device %d: %s", device_id, device.state)
        await self._async_notify_device_update(device)

    async def _async_handle_device_alarm_trigger(self, data: dict[str, Any]) -> None:
        """Handle K1 device alarm trigger."""
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
        """Handle K1 device name reply."""
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
                _LOGGER.info("K1: Device %d name: %s", device_id, name)
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
            self._devices[device_id] = ElroDevice(
                device_id, self._device_id
            )  # Pass hub device_id
            _LOGGER.info("Created new device: %d", device_id)
        return self._devices[device_id]

    async def _async_notify_device_update(self, device: ElroDevice) -> None:
        """Notify callbacks of device update."""
        _LOGGER.debug(
            "Notifying %d callbacks for device %d",
            len(self._device_update_callbacks),
            device.id,
        )
        for callback in self._device_update_callbacks:
            try:
                callback(device)
            except Exception as ex:
                _LOGGER.error("Error in device update callback: %s", ex)

    def _construct_message(self, data: str) -> str:
        """Construct message with proper format (K1 or K2 compatible)."""
        self._msg_id += 1

        if self._use_k2:
            # K2 format: Plain JSON with K2 structure (NOT encrypted)
            try:
                data_obj = json.loads(data)

                # Map K1 cmdId to K2 CMD_CODE
                cmd_code_map = {
                    ElroCommands.GET_ALL_EQUIPMENT_STATUS: 54,
                    ElroCommands.SYN_DEVICE_STATUS: 29,
                    ElroCommands.GET_DEVICE_NAME: 24,
                    ElroCommands.EQUIPMENT_CONTROL: 1,
                }

                cmd_id = data_obj.get("cmdId")
                cmd_code = cmd_code_map.get(cmd_id, cmd_id)

                message = {
                    "action": "APP_SEND",
                    "devID": self._device_id,
                    "msg": {
                        "msg_ID": self._msg_id,
                        "CMD_CODE": cmd_code,
                        "rev_str1": "",
                        "rev_str2": "",
                        "rev_str3": "",
                    },
                }

                return json.dumps(message, separators=(",", ":"))

            except Exception as ex:
                _LOGGER.error("Failed to construct K2 message: %s", ex)
                raise
        else:
            # K1 format (original)
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
        # Use K1 command - will be mapped to K2 if needed
        data = json.dumps(
            {"cmdId": ElroCommands.GET_ALL_EQUIPMENT_STATUS, "device_status": ""}
        )
        msg = self._construct_message(data)
        await self._async_send_data(msg)

    async def async_get_device_names(self) -> None:
        """Get device names."""
        # Use K1 command - will be mapped to K2 if needed
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
                # Wait for 30 seconds but check running state every 5 seconds
                for _ in range(6):  # 6 * 5 seconds = 30 seconds
                    if not self._running:
                        return  # type: ignore[unreachable]
                    await asyncio.sleep(5)

                # Check if we received data recently
                time_since_last_data = datetime.now() - self._last_data_received
                if time_since_last_data > timedelta(minutes=2):  # 2 minute timeout
                    _LOGGER.warning(
                        "No data received for %s, reconnecting", time_since_last_data
                    )
                    await self._async_reconnect()
                else:
                    _LOGGER.debug(
                        "Data received %s ago, connection healthy", time_since_last_data
                    )

                # Sync devices periodically to keep connection active
                try:
                    await self.async_sync_devices()
                except Exception as ex:
                    _LOGGER.error("Error in periodic sync: %s", ex)

            except asyncio.CancelledError:
                break
            except Exception as ex:
                _LOGGER.error("Error in heartbeat: %s", ex)
                if self._running:
                    # Wait 30 seconds before retry, but check running state every 5 seconds
                    for _ in range(6):  # 6 * 5 seconds = 30 seconds
                        if not self._running:
                            return  # type: ignore[unreachable]
                        await asyncio.sleep(5)
