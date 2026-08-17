#!/usr/bin/env python3
"""
ELRO Connects Real-time Diagnostic Tool with K1/K2 Support

Run this with Python 3.12 or newer:

    python3 -m pip install elro-connects-k2-protocol==0.1.0
    python3 elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --test

("python" is still Python 2 on some systems, which cannot even parse this file.)

K1: plain-text UDP JSON, spoken directly by this tool.
K2: XOR-framed UDP JSON, spoken through the elro-connects-k2-protocol library
    (https://github.com/ldebruijn/elro-connects-k2-protocol) - the same library
    the integration itself uses, so what you see here is what Home Assistant
    sees.
"""

# Deliberately the first Python 3 only syntax in this file: a "python" that is
# really Python 2 cannot parse anything below either, and stops here - so its
# SyntaxError quotes this line, which says what to do about it.
RUN_THIS_WITH_PYTHON3: bool = True  # run: python3 elro_test_tool.py (3.12+)

import argparse
import asyncio
import json
import logging
import socket
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

try:
    from elro_connects_k2_protocol import K2Gateway, SubDevice, UpdateSource
    from elro_connects_k2_protocol.protocol import (
        UDP_PORT,
        build_activation,
        decrypt_message,
        encrypt_message,
    )
except ImportError:  # pragma: no cover - setup help, not a runtime path
    sys.exit(
        "The K2 protocol library is missing (Python "
        f"{sys.version_info.major}.{sys.version_info.minor}, needs 3.12+).\n"
        "Install it with:\n"
        "    python3 -m pip install --user elro-connects-k2-protocol==0.1.0\n"
        "A distro-managed Python refuses that with a PEP 668 error; there, add\n"
        "--break-system-packages, use a virtualenv, or skip installing and point\n"
        "PYTHONPATH at a checkout of the library instead:\n"
        "    PYTHONPATH=/path/to/elro-connects-k2-protocol python3 " + sys.argv[0]
    )

# ELRO Protocol Constants
DEFAULT_PORT = UDP_PORT
DEFAULT_CTRL_KEY = "0"
DEFAULT_APP_ID = "0"

PROTOCOL_K1 = "K1"
PROTOCOL_K2 = "K2"

# Actions only a hub sends. Our own request loops back when the hub address is
# this host, so "IOT_KEY?" must not be mistaken for an answer.
_HUB_ACTIONS = frozenset({"NODE_ACK", "NODE_SEND", "APP_SEND"})


class ElroCommands:
    """ELRO Connects command constants from Android app."""

    EQUIPMENT_CONTROL = 1
    INCREASE_EQUIPMENT = 2
    DELETE_EQUIPMENT = 4
    MODIFY_EQUIPMENT_NAME = 5
    SYN_DEVICE_NAME = 24
    SYN_DEVICE_STATUS = 29
    SYN_ALL_DEVICE_STATUS = 54
    UPLOAD_DEVICE_NAME = 17
    UPLOAD_DEVICE_STATUS = 19


# ---------------------------------------------------------------------------
# Protocol detection
# ---------------------------------------------------------------------------


class _DetectProtocol(asyncio.DatagramProtocol):
    """Resolve as soon as a decodable K2 frame arrives from the hub."""

    def __init__(self, host: str, result: "asyncio.Future[bool]") -> None:
        self._host = host
        self._result = result

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if addr[0] != self._host or self._result.done():
            return
        if not data or data[0] == 0x7B:  # '{' -> plain text, so a K1 hub
            self._result.set_result(False)
            return
        _text, obj = decrypt_message(data)
        if isinstance(obj, dict) and obj.get("action") in _HUB_ACTIONS:
            self._result.set_result(True)


async def detect_protocol(host: str, device_id: str, timeout: float = 3.0) -> str:
    """Return "K2" when the hub answers the XOR-framed handshake, else "K1"."""
    logger = logging.getLogger("ElroTestTool")
    loop = asyncio.get_running_loop()
    result: "asyncio.Future[bool]" = loop.create_future()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    try:
        # A K2 only answers requests that come from port 1025.
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError as ex:
        sock.close()
        logger.warning("Cannot bind UDP port %d (%s); assuming K1", UDP_PORT, ex)
        return PROTOCOL_K1

    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _DetectProtocol(host, result), sock=sock
    )
    try:
        transport.sendto(encrypt_message(build_activation(device_id)), (host, UDP_PORT))
        is_k2 = await asyncio.wait_for(result, timeout=timeout)
    except asyncio.TimeoutError:
        logger.info("No K2 answer within %.1fs, falling back to K1", timeout)
        is_k2 = False
    finally:
        transport.close()

    protocol = PROTOCOL_K2 if is_k2 else PROTOCOL_K1
    logger.info("Protocol detected: %s", protocol)
    return protocol


# ---------------------------------------------------------------------------
# K2 - everything on the wire is handled by the protocol library
# ---------------------------------------------------------------------------


class K2TestTool:
    """Diagnostics for a K2 hub, driven through K2Gateway."""

    def __init__(self, host: str, device_id: str):
        self.host = host
        self.device_id = device_id
        self.gateway = K2Gateway(host, device_id)
        self.update_log: List[Dict[str, Any]] = []
        self.last_received = datetime.now()
        self.stats: Dict[str, Any] = {
            "push_updates": 0,
            "poll_updates": 0,
            "paired_updates": 0,
            "max_silence_duration": timedelta(0),
        }
        self.logger = logging.getLogger("ElroTestTool")

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        self.gateway.add_update_callback(self._on_update)
        await self.gateway.connect()
        self.logger.info("Bound UDP port %d and activated session", UDP_PORT)

    async def close(self) -> None:
        await self.gateway.disconnect()

    def _on_update(self, sub_id: int, device: SubDevice, source: UpdateSource) -> None:
        now = datetime.now()
        silence = now - self.last_received
        if silence > self.stats["max_silence_duration"]:
            self.stats["max_silence_duration"] = silence
        self.last_received = now

        key = {
            UpdateSource.PUSH: "push_updates",
            UpdateSource.POLL: "poll_updates",
            UpdateSource.PAIRED: "paired_updates",
        }[source]
        self.stats[key] += 1

        self.update_log.append(
            {
                "timestamp": now.isoformat(),
                "source": source.name,
                "sub_id": sub_id,
                "device_type": device.device_type,
                "profile": device.profile.name,
                "alarm_state": device.alarm_state.name,
                "battery_pct": device.battery_pct,
                "signal_bars": device.signal_bars,
                "raw_status": device.raw_status,
            }
        )
        self.logger.info(
            "<- [%s] sub=%d %s alarm=%s battery=%d%% signal=%d",
            source.name,
            sub_id,
            device.profile.name,
            device.alarm_state.name,
            device.battery_pct,
            device.signal_bars,
        )

    # -- commands ----------------------------------------------------------

    async def sync(self) -> Dict[int, SubDevice]:
        self.logger.info("-> CMD_CODE 54 (sync all devices)")
        await self.gateway.activate()
        devices: Dict[int, SubDevice] = await self.gateway.sync_devices()
        self._print_devices(devices)
        return devices

    async def show_names(self) -> None:
        self.logger.info("-> CMD_CODE 24 (sync device names)")
        await self.gateway.activate()
        names = await self.gateway.sync_device_names()
        if not names:
            print("  No custom names stored in the hub")
        for sub_id, name in sorted(names.items()):
            print(f"  Sub {sub_id:>3}  {name!r}")

    async def show_gateway_info(self) -> None:
        self.logger.info("-> CMD_CODE 12 (gateway info)")
        await self.gateway.activate()
        info = await self.gateway.get_gateway_info()
        if info is None:
            print("  No answer to the gateway info request")
            return
        print(f"  Device name:  {info.device_name}")
        print(f"  IP:           {info.ip}")
        print(f"  Product key:  {info.product_key}")
        print(f"  data_str1:    {info.raw_data_str1}")
        print(f"  data_str2:    {info.raw_data_str2}")

    async def test_alarm(self, sub_id: int) -> None:
        device = self.gateway.devices.get(sub_id)
        if device is None:
            print(f"  Unknown sub-device {sub_id}; run a sync first")
            return
        action = device.profile.test_action
        if action is None:
            print(f"  {device.profile.name} does not support an alarm test")
            return
        self.logger.info("-> CMD_CODE 1 test on sub %d (payload %s)", sub_id, action)
        await self.gateway.activate()
        self.gateway.send_device_action(sub_id, action)

    async def silence_alarm(self, sub_id: int) -> None:
        device = self.gateway.devices.get(sub_id)
        if device is None:
            print(f"  Unknown sub-device {sub_id}; run a sync first")
            return
        action = device.profile.mute_action
        if action is None:
            print(f"  {device.profile.name} cannot be silenced remotely")
            return
        self.logger.info("-> CMD_CODE 1 silence on sub %d (payload %s)", sub_id, action)
        await self.gateway.activate()
        self.gateway.send_device_action(sub_id, action)

    def _print_devices(self, devices: Dict[int, SubDevice]) -> None:
        if not devices:
            print("  No devices reported")
            return
        for sub_id, device in sorted(devices.items()):
            extras = []
            if device.co2_ppm is not None:
                extras.append(f"co2={device.co2_ppm}ppm")
            if device.temperature_c is not None:
                extras.append(f"temp={device.temperature_c}C")
            if device.humidity_pct is not None:
                extras.append(f"humidity={device.humidity_pct}%")
            if device.temperature_setpoint is not None:
                extras.append(f"setpoint={device.temperature_setpoint}C")
            if device.valve_open is not None:
                extras.append(f"valve={'open' if device.valve_open else 'closed'}")
            print(
                f"  Sub {sub_id:>3}  {device.profile.name:<38} "
                f"type={device.raw_type} signal={device.signal_bars} "
                f"battery={device.battery_pct}% status={device.alarm_state.name}"
                + (f"  {' '.join(extras)}" if extras else "")
                + (f"  name={device.nickname!r}" if device.nickname else "")
            )

    # -- modes -------------------------------------------------------------

    async def test_connectivity(self) -> bool:
        _banner(
            "Testing connectivity to ELRO Connects hub (K2)", self.host, self.device_id
        )
        await self.connect()

        print("\nTest 1: sync all devices (CMD_CODE 54 -> 55/56)")
        devices = await self.sync()
        if not devices:
            self.logger.error("[FAIL] Hub did not report any devices")
            return False
        self.logger.info("[OK] %d device(s) reported", len(devices))

        print("\nTest 2: device names (CMD_CODE 24 -> 17)")
        await self.show_names()

        print("\nTest 3: gateway info (CMD_CODE 12 -> 13)")
        await self.show_gateway_info()

        print("\nTest 4: listening 10s for unsolicited pushes (CMD_CODE 19)")
        await asyncio.sleep(10)

        self._print_statistics()
        return True

    async def monitor_mode(self, duration: int) -> None:
        _banner(f"Monitoring for {duration}s (K2)", self.host, self.device_id)
        await self.connect()
        await self.sync()

        start = datetime.now()
        last_sync = datetime.now()
        try:
            while (datetime.now() - start).total_seconds() < duration:
                await asyncio.sleep(1)
                silence = datetime.now() - self.last_received
                if silence.total_seconds() > 120:
                    self.logger.warning("[WARN] No update received for %s", silence)
                if (datetime.now() - last_sync).total_seconds() >= 60:
                    print("\n--- periodic re-sync ---")
                    await self.sync()
                    last_sync = datetime.now()
        except KeyboardInterrupt:
            self.logger.info("Monitoring interrupted by user")
        finally:
            self._print_statistics()

    async def interactive_mode(self) -> None:
        _banner("Interactive mode (K2)", self.host, self.device_id)
        await self.connect()

        print("\nAvailable commands:")
        print("  1 - Sync all devices (CMD_CODE 54)")
        print("  2 - Sync device names (CMD_CODE 24)")
        print("  3 - Gateway info (CMD_CODE 12)")
        print("  4 - Test alarm on a device (CMD_CODE 1)")
        print("  5 - Silence alarm on a device (CMD_CODE 1)")
        print("  6 - Pair a new device (CMD_CODE 2, 60s window)")
        print("  k - Send session keepalive (IOT_KEY?)")
        print("  s - Show statistics")
        print("  q - Quit")
        print()

        loop = asyncio.get_event_loop()
        while True:
            try:
                cmd = (
                    (await loop.run_in_executor(None, input, "[K2] Command: "))
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                break

            if cmd == "1":
                await self.sync()
            elif cmd == "2":
                await self.show_names()
            elif cmd == "3":
                await self.show_gateway_info()
            elif cmd in ("4", "5"):
                raw = await loop.run_in_executor(None, input, "  Sub-device id: ")
                try:
                    sub_id = int(raw.strip())
                except ValueError:
                    print("  Not a number")
                    continue
                if cmd == "4":
                    await self.test_alarm(sub_id)
                else:
                    await self.silence_alarm(sub_id)
            elif cmd == "6":
                print("  Opening pairing window; trigger the detector now...")
                result = await self.gateway.pair_new_device()
                if result is None:
                    print("  Nothing joined (or the hub refused the request)")
                else:
                    print(
                        f"  Joined: sub {result.device.sub_id} "
                        f"{result.device.profile.name} "
                        f"(already_known={result.already_known})"
                    )
            elif cmd == "k":
                await self.gateway.activate()
                print("  Sent IOT_KEY?")
            elif cmd == "s":
                self._print_statistics()
            elif cmd == "q":
                break
            else:
                print("  Unknown command")

        self._print_statistics()

    def _print_statistics(self) -> None:
        print(f"\n{'='*70}")
        print("Communication Statistics")
        print(f"{'='*70}")
        print(f"Protocol:            {PROTOCOL_K2}")
        print(f"Devices known:       {len(self.gateway.devices)}")
        print(f"Push updates:        {self.stats['push_updates']}")
        print(f"Poll updates:        {self.stats['poll_updates']}")
        print(f"Pairing updates:     {self.stats['paired_updates']}")
        print(f"Max silence:         {self.stats['max_silence_duration']}")
        print(f"{'='*70}\n")

    def save_log(self, filename: str) -> None:
        try:
            with open(filename, "w") as handle:
                json.dump(
                    {
                        "test_info": {
                            "host": self.host,
                            "device_id": self.device_id,
                            "port": UDP_PORT,
                            "timestamp": datetime.now().isoformat(),
                            "protocol": PROTOCOL_K2,
                        },
                        "statistics": {
                            k: str(v) if isinstance(v, timedelta) else v
                            for k, v in self.stats.items()
                        },
                        "devices": [
                            {
                                "sub_id": d.sub_id,
                                "raw_type": d.raw_type,
                                "device_type": d.device_type,
                                "profile": d.profile.name,
                                "model_hints": list(d.profile.model_hints),
                                "signal_bars": d.signal_bars,
                                "battery_pct": d.battery_pct,
                                "alarm_state": d.alarm_state.name,
                                "raw_status": d.raw_status,
                                "nickname": d.nickname,
                                "co2_ppm": d.co2_ppm,
                                "temperature_c": d.temperature_c,
                                "humidity_pct": d.humidity_pct,
                            }
                            for d in sorted(
                                self.gateway.devices.values(), key=lambda x: x.sub_id
                            )
                        ],
                        "updates": self.update_log,
                    },
                    handle,
                    indent=2,
                )
            self.logger.info("Log saved to %s", filename)
        except Exception as ex:
            self.logger.error("Failed to save log: %s", ex)


# ---------------------------------------------------------------------------
# K1 - plain-text UDP, unchanged wire behaviour
# ---------------------------------------------------------------------------


class K1TestTool:
    """Diagnostics for a K1 hub, speaking plain-text UDP JSON directly."""

    def __init__(self, host: str, device_id: str, port: int = DEFAULT_PORT):
        """Initialize the test tool."""
        self.host = host
        self.device_id = device_id
        self.port = port

        self.sock: Optional[socket.socket] = None
        self.running = False
        self.last_received = datetime.now()
        self.receive_count = 0
        self.send_count = 0
        self.message_log: List[Dict[str, Any]] = []
        self._msg_id = 0

        # Statistics
        self.stats: Dict[str, Any] = {
            "messages_sent": 0,
            "messages_received": 0,
            "errors": 0,
            "max_silence_duration": timedelta(0),
        }

        self.logger = logging.getLogger("ElroTestTool")

    def setup_socket(self) -> bool:
        """Create and configure the UDP socket."""
        try:
            if self.sock:
                self.sock.close()

            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setblocking(False)

            self.logger.info(f"Socket created for {self.host}:{self.port}")
            return True
        except Exception as ex:
            self.logger.error(f"Failed to create socket: {ex}")
            return False

    def send_message(self, message: str, description: str = "") -> bool:
        """Send a message to the hub."""
        if not self.sock:
            self.logger.error("Socket not initialized")
            return False

        try:
            encoded = message.encode("utf-8")
            self.sock.sendto(encoded, (self.host, self.port))
            self.stats["messages_sent"] += 1
            self.send_count += 1

            self.message_log.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "direction": "SENT",
                    "protocol": PROTOCOL_K1,
                    "description": description,
                    "message": message,
                    "bytes": len(encoded),
                    "raw": encoded.hex(),
                }
            )

            self.logger.info(f"-> SENT [K1] {description}")
            self.logger.debug(f"  Message: {message[:100]}...")
            return True
        except Exception as ex:
            self.logger.error(f"Failed to send message: {ex}")
            self.stats["errors"] += 1
            return False

    def _construct_message(
        self,
        cmd_code: int,
        rev_str1: str = "",
        rev_str2: str = "",
        rev_str3: str = "",
    ) -> str:
        """Construct a message in the ELRO UDP format from the Android app."""
        self._msg_id += 1

        message = {
            "action": "APP_SEND",
            "devID": self.device_id,
            "msg": {
                "msg_ID": self._msg_id,
                "CMD_CODE": cmd_code,
                "rev_str1": rev_str1,
                "rev_str2": rev_str2,
                "rev_str3": rev_str3,
            },
        }

        return json.dumps(message)

    def _send_command(
        self,
        cmd_code: int,
        description: str,
        rev_str1: str = "",
        rev_str2: str = "",
        rev_str3: str = "",
    ) -> None:
        """Send a command to the hub."""
        self.send_message(
            self._construct_message(cmd_code, rev_str1, rev_str2, rev_str3), description
        )

    async def receive_messages(self, timeout: float = 1.0) -> None:
        """Receive messages from the hub."""
        if not self.sock:
            return

        try:
            loop = asyncio.get_event_loop()
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(self.sock, 4096), timeout=timeout
            )

            now = datetime.now()
            silence_duration = now - self.last_received
            if silence_duration > self.stats["max_silence_duration"]:
                self.stats["max_silence_duration"] = silence_duration

            self.last_received = now
            self.stats["messages_received"] += 1
            self.receive_count += 1

            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                self.logger.warning(
                    f"<- Non-text message ({len(data)} bytes): {data.hex()[:60]} "
                    "- is this hub a K2? Try --protocol k2"
                )
                self.stats["errors"] += 1
                return

            self.message_log.append(
                {
                    "timestamp": now.isoformat(),
                    "direction": "RECEIVED",
                    "protocol": PROTOCOL_K1,
                    "message": decoded,
                    "bytes": len(data),
                    "raw": data.hex(),
                    "source": f"{addr[0]}:{addr[1]}",
                }
            )

            self.logger.info(f"<- RECV [K1] {decoded[:100]}...")
            self.logger.debug(f"  From: {addr}")

            if decoded.startswith("{"):
                try:
                    self._parse_json_message(json.loads(decoded))
                except json.JSONDecodeError:
                    pass
            elif "IOT_KEY" in decoded:
                self.logger.info("  -> IOT_KEY response detected")

        except asyncio.TimeoutError:
            pass
        except Exception as ex:
            self.logger.error(f"Error receiving data: {ex}")
            self.stats["errors"] += 1

    def _parse_json_message(self, json_data: dict) -> None:
        """Parse a JSON message from the hub."""
        try:
            action = json_data.get("action", "")

            if action == "NODE_ACK":
                dev_id = json_data.get("devID", "")
                msg = json_data.get("msg", {})
                cmd_code = msg.get("CMD_CODE", "")
                self.logger.info(f"  -> NODE_ACK from {dev_id}, CMD_CODE={cmd_code}")

            elif action in ("APP_SEND", "NODE_SEND"):
                dev_id = json_data.get("devID", "")
                msg = json_data.get("msg", {})
                cmd_code = msg.get("CMD_CODE", "")
                msg_id = msg.get("msg_ID", "")
                rev_str1 = msg.get("rev_str1", "") or msg.get("data_str1", "")
                rev_str2 = msg.get("rev_str2", "") or msg.get("data_str2", "")

                cmd_name = self._get_command_name(cmd_code)

                self.logger.info(
                    f"  -> {action}: devID={dev_id}, CMD={cmd_code}({cmd_name}), "
                    f"msgID={msg_id}"
                )

                if cmd_code == ElroCommands.UPLOAD_DEVICE_NAME:
                    self._parse_device_name(rev_str1, rev_str2)
                elif cmd_code == ElroCommands.UPLOAD_DEVICE_STATUS:
                    self._parse_device_status(rev_str1, rev_str2)

            else:
                self.logger.info(f"  -> JSON action: {action}")

        except Exception as ex:
            self.logger.debug(f"  Could not parse JSON message: {ex}")

    def _parse_device_name(self, device_id_hex: str, name_hex: str) -> None:
        """Parse device name from hex."""
        try:
            if len(device_id_hex) >= 4:
                device_id = int(device_id_hex[:4], 16)
                if len(name_hex) >= 32:
                    name_bytes = bytes.fromhex(name_hex[:32])
                    name = (
                        "".join(chr(b) for b in name_bytes if b != 0)
                        .replace("@", "")
                        .replace("$", "")
                    )
                    self.logger.info(f"    Device {device_id}: '{name}'")
        except Exception as e:
            self.logger.debug(f"    Could not parse device name: {e}")

    def _parse_device_status(self, device_id_hex: str, status_data: str) -> None:
        """Parse device status."""
        try:
            if len(device_id_hex) >= 4:
                device_id = int(device_id_hex[:4], 16)
                device_type = status_data[:4] if len(status_data) >= 4 else "????"
                battery = "N/A"
                if len(status_data) >= 6:
                    battery = f"{int(status_data[4:6], 16)}%"
                state = status_data[6:] if len(status_data) > 6 else ""

                self.logger.info(
                    f"    Device {device_id}: Type={device_type}, "
                    f"Battery={battery}, State={state}"
                )
        except Exception as e:
            self.logger.debug(f"    Could not parse device status: {e}")

    def _get_command_name(self, command_id: int) -> str:
        """Get human-readable command name."""
        command_map = {
            1: "EQUIPMENT_CONTROL",
            17: "UPLOAD_DEVICE_NAME",
            19: "UPLOAD_DEVICE_STATUS",
            24: "SYN_DEVICE_NAME",
            29: "SYN_DEVICE_STATUS",
            54: "SYN_ALL_DEVICE_STATUS",
        }
        return command_map.get(command_id, f"CMD_{command_id}")

    async def send_iot_key_query(self) -> None:
        """Send IOT_KEY query to hub."""
        self.send_message(f"IOT_KEY?{self.device_id}", "IOT_KEY query")

    async def send_sync_devices(self) -> None:
        """Send sync devices command (CMD_CODE=29)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_DEVICE_STATUS,
            description="Sync device status (CMD=29)",
        )

    async def send_get_device_names(self) -> None:
        """Send get device names command (CMD_CODE=24)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_DEVICE_NAME,
            description="Get device names (CMD=24)",
            rev_str1="0",
        )

    async def send_get_all_status(self) -> None:
        """Send get all device status command (CMD_CODE=54)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_ALL_DEVICE_STATUS,
            description="Get all device status (CMD=54)",
        )

    async def test_connectivity(self) -> bool:
        """Test basic connectivity to the hub."""
        _banner(
            "Testing connectivity to ELRO Connects hub (K1)", self.host, self.device_id
        )

        if not self.setup_socket():
            return False

        # Test 1: IOT_KEY query
        self.logger.info("Test 1: Sending IOT_KEY query...")
        await self.send_iot_key_query()

        response_received = False
        for _ in range(10):
            await self.receive_messages(timeout=0.5)
            if self.receive_count > 0:
                response_received = True
                break

        if response_received:
            self.logger.info("[OK] IOT_KEY query successful\n")
        else:
            self.logger.error("[FAIL] No response to IOT_KEY query\n")
            return False

        for description, sender in (
            ("Test 2: Requesting device sync (CMD_CODE=29)...", self.send_sync_devices),
            (
                "Test 3: Requesting device names (CMD_CODE=24)...",
                self.send_get_device_names,
            ),
            (
                "Test 4: Requesting all device status (CMD_CODE=54)...",
                self.send_get_all_status,
            ),
        ):
            self.logger.info(description)
            await sender()

            initial_count = self.receive_count
            for _ in range(20):
                await self.receive_messages(timeout=0.5)

            new_messages = self.receive_count - initial_count
            if new_messages > 0:
                self.logger.info(f"[OK] Received {new_messages} messages\n")
            else:
                self.logger.warning("[WARN] No additional messages received\n")

        self._print_statistics()
        return True

    async def monitor_mode(self, duration: int) -> None:
        """Monitor incoming messages for specified duration."""
        _banner(f"Monitoring for {duration}s (K1)", self.host, self.device_id)

        if not self.setup_socket():
            return

        await self.send_iot_key_query()
        await asyncio.sleep(1)

        self.running = True
        self.last_received = datetime.now()
        start_time = datetime.now()
        last_status_check = datetime.now()

        try:
            while self.running:
                if (datetime.now() - start_time).total_seconds() >= duration:
                    break

                await self.receive_messages(timeout=0.5)

                silence = datetime.now() - self.last_received
                if silence.total_seconds() > 30:
                    self.logger.warning(f"[WARN] No data received for {silence}")

                if (datetime.now() - last_status_check).total_seconds() >= 30:
                    self.logger.info("\n--- Sending periodic status check ---")
                    await self.send_sync_devices()
                    last_status_check = datetime.now()

                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            self.logger.info("\nMonitoring interrupted by user")
        finally:
            self._print_statistics()

    async def interactive_mode(self) -> None:
        """Interactive mode for manual testing."""
        _banner("Interactive mode (K1)", self.host, self.device_id)

        if not self.setup_socket():
            return

        self.running = True
        receiver_task = asyncio.create_task(self._receive_loop())

        print("\nAvailable commands:")
        print("  1 - Send IOT_KEY query")
        print("  2 - Sync devices (CMD_CODE=29)")
        print("  3 - Get device names (CMD_CODE=24)")
        print("  4 - Get all device status (CMD_CODE=54)")
        print("  s - Show statistics")
        print("  q - Quit")
        print()

        try:
            while self.running:
                try:
                    cmd = await asyncio.get_event_loop().run_in_executor(
                        None, input, "[K1] Command: "
                    )
                    cmd = cmd.strip().lower()

                    if cmd == "1":
                        await self.send_iot_key_query()
                    elif cmd == "2":
                        await self.send_sync_devices()
                    elif cmd == "3":
                        await self.send_get_device_names()
                    elif cmd == "4":
                        await self.send_get_all_status()
                    elif cmd == "s":
                        self._print_statistics()
                    elif cmd == "q":
                        break
                    else:
                        print("Unknown command")
                except EOFError:
                    break
        except KeyboardInterrupt:
            print("\nInterrupted by user")
        finally:
            self.running = False
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            self._print_statistics()

    async def _receive_loop(self) -> None:
        """Background task for receiving messages."""
        while self.running:
            try:
                await self.receive_messages(timeout=0.5)
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self.logger.error(f"Error in receive loop: {ex}")

    def _print_statistics(self) -> None:
        """Print communication statistics."""
        print(f"\n{'='*70}")
        print("Communication Statistics")
        print(f"{'='*70}")
        print(f"Protocol:            {PROTOCOL_K1}")
        print(f"Messages sent:       {self.stats['messages_sent']}")
        print(f"Messages received:   {self.stats['messages_received']}")
        print(f"Errors:              {self.stats['errors']}")
        print(f"Max silence:         {self.stats['max_silence_duration']}")
        print(f"{'='*70}\n")

    def save_log(self, filename: str) -> None:
        """Save message log to file."""
        try:
            with open(filename, "w") as f:
                json.dump(
                    {
                        "test_info": {
                            "host": self.host,
                            "device_id": self.device_id,
                            "port": self.port,
                            "timestamp": datetime.now().isoformat(),
                            "protocol": PROTOCOL_K1,
                        },
                        "statistics": {
                            k: str(v) if isinstance(v, timedelta) else v
                            for k, v in self.stats.items()
                        },
                        "messages": self.message_log,
                    },
                    f,
                    indent=2,
                )
            self.logger.info(f"Log saved to {filename}")
        except Exception as ex:
            self.logger.error(f"Failed to save log: {ex}")

    def cleanup(self) -> None:
        """Clean up resources."""
        self.running = False
        if self.sock:
            self.sock.close()
            self.sock = None


def _banner(title: str, host: str, device_id: str) -> None:
    """Print a mode banner."""
    print(f"\n{'='*70}")
    print(title)
    print(f"Hub: {host}:{DEFAULT_PORT}")
    print(f"Device ID: {device_id}")
    print(f"{'='*70}\n")


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)8s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # The protocol library logs every decoded frame at DEBUG.
    logging.getLogger("elro_connects_k2_protocol").setLevel(level)


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ELRO Connects Real-time Diagnostic Tool - K1/K2 Support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect protocol
  python elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --test

  # Force K2 protocol
  python elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --protocol k2 --test

  # Monitor for 5 minutes
  python elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --monitor 300

  # Interactive mode
  python elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --interactive -v

Note: the K2 needs UDP port 1025 free on this machine - stop Home Assistant (or
run this from another host) before using the tool against a K2 hub.
        """,
    )

    parser.add_argument("--host", required=True, help="Hub IP address")
    parser.add_argument(
        "--device-id", required=True, help="Device ID (e.g., ST_2342400722)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"UDP port, K1 only (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--protocol", choices=["k1", "k2"], help="Force protocol (default: auto-detect)"
    )

    # Modes
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--test", action="store_true", help="Run connectivity tests"
    )
    mode_group.add_argument(
        "--monitor", type=int, metavar="SECONDS", help="Monitor mode for a duration"
    )
    mode_group.add_argument(
        "--interactive", action="store_true", help="Interactive mode"
    )

    # Options
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--save-log", metavar="FILE", help="Save log to JSON file")

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.protocol:
        protocol = args.protocol.upper()
    else:
        protocol = await detect_protocol(args.host, args.device_id)

    tool: Any
    if protocol == PROTOCOL_K2:
        tool = K2TestTool(host=args.host, device_id=args.device_id)
    else:
        tool = K1TestTool(host=args.host, device_id=args.device_id, port=args.port)

    try:
        if args.test:
            await tool.test_connectivity()
        elif args.monitor:
            await tool.monitor_mode(args.monitor)
        elif args.interactive:
            await tool.interactive_mode()

        if args.save_log:
            tool.save_log(args.save_log)

    finally:
        if isinstance(tool, K2TestTool):
            await tool.close()
        else:
            tool.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
