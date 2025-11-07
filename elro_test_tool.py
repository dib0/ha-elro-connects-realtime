#!/usr/bin/env python3
"""
ELRO Connects Real-time Diagnostic Tool with K1/K2 Support

K1: Original working protocol from Android app
K2: XOR encrypted variant with auto-detection
"""

import argparse
import asyncio
import json
import logging
import socket
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import random

# K2 Codec (inline for portability)
class K2Codec:
    """K2 protocol encoder/decoder"""
    XOR_CONSTANT = 0x23
    
    @classmethod
    def encode_k2_message(cls, json_data: dict) -> bytes:
        """Encode JSON to K2 binary format"""
        json_str = json.dumps(json_data, separators=(',', ':'))
        utf8_bytes = json_str.encode('utf-8')
        hex_string = utf8_bytes.hex()
        
        xor_key = random.randint(0, 255)
        xor_mask = xor_key ^ cls.XOR_CONSTANT
        
        encrypted = bytearray([xor_key])
        for i in range(0, len(hex_string), 2):
            hex_byte = int(hex_string[i:i+2], 16)
            encrypted.append(hex_byte ^ xor_mask)
        
        return bytes(encrypted)
    
    @classmethod
    def decode_k2_message(cls, data: bytes) -> Optional[dict]:
        """Decode K2 binary to JSON"""
        if not data or len(data) < 2:
            return None
        
        try:
            xor_key = data[0]
            xor_mask = xor_key ^ cls.XOR_CONSTANT
            
            hex_parts = []
            for i in range(1, len(data)):
                decrypted_byte = data[i] ^ xor_mask
                hex_parts.append(f'{decrypted_byte:02x}')
            
            hex_string = ''.join(hex_parts)
            decoded_bytes = bytes.fromhex(hex_string)
            json_str = decoded_bytes.decode('utf-8')
            
            # Truncate at first complete JSON
            if '}}' in json_str:
                json_str = json_str[:json_str.index('}}') + 2]
            elif '}' in json_str:
                json_str = json_str[:json_str.index('}') + 1]
            
            return json.loads(json_str)
        except Exception:
            return None
    
    @staticmethod
    def is_k2_message(data: bytes) -> bool:
        """Check if data is K2 encrypted"""
        if not data or len(data) < 2:
            return False
        if data[0] == 0x7B:  # '{'
            return False
        try:
            text = data.decode('utf-8')
            return not text.strip().startswith('{')
        except UnicodeDecodeError:
            return True


# ELRO Protocol Constants
DEFAULT_PORT = 1025
DEFAULT_CTRL_KEY = "0"
DEFAULT_APP_ID = "0"


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


class ElroTestTool:
    """ELRO Connects diagnostic tool with K1/K2 auto-detection."""
    
    def __init__(self, host: str, device_id: str, port: int = DEFAULT_PORT,
                 force_protocol: Optional[str] = None):
        """Initialize the test tool."""
        self.host = host
        self.device_id = device_id
        self.port = port
        self.force_protocol = force_protocol
        
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.last_received = datetime.now()
        self.receive_count = 0
        self.send_count = 0
        self.message_log: List[Dict[str, Any]] = []
        self._msg_id = 0
        
        # Protocol detection
        self.detected_protocol: Optional[str] = None
        self.use_k2 = False
        
        # Statistics
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "k1_messages": 0,
            "k2_messages": 0,
            "errors": 0,
            "max_silence_duration": timedelta(0),
        }
        
        self.logger = logging.getLogger("ElroTestTool")
    
    @property
    def protocol(self) -> str:
        """Get current protocol"""
        if self.force_protocol:
            return self.force_protocol.upper()
        if self.detected_protocol:
            return self.detected_protocol
        return "UNKNOWN"
    
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
    
    def send_message(self, message: str | bytes, description: str = "") -> bool:
        """Send a message to the hub (string or bytes)."""
        if not self.sock:
            self.logger.error("Socket not initialized")
            return False
        
        try:
            if isinstance(message, str):
                encoded = message.encode("utf-8")
                protocol_used = "K1"
            else:
                encoded = message
                protocol_used = "K2"
            
            self.sock.sendto(encoded, (self.host, self.port))
            self.stats["messages_sent"] += 1
            self.send_count += 1
            
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "direction": "SENT",
                "protocol": protocol_used,
                "description": description,
                "message": message if isinstance(message, str) else f"<K2 binary {len(message)} bytes>",
                "bytes": len(encoded),
                "raw": encoded.hex(),
            }
            self.message_log.append(log_entry)
            
            self.logger.info(f"→ SENT [{protocol_used}] {description}")
            if isinstance(message, str):
                self.logger.debug(f"  Message: {message[:100]}...")
            self.logger.debug(f"  Raw bytes: {encoded.hex()[:120]}...")
            return True
        except Exception as ex:
            self.logger.error(f"Failed to send message: {ex}")
            self.stats["errors"] += 1
            return False
    
    def _construct_k1_message(self, cmd_code: int, rev_str1: str = "", 
                              rev_str2: str = "", rev_str3: str = "") -> str:
        """Construct K1 message in ELRO UDP format from Android app."""
        self._msg_id += 1
        
        message = {
            "action": "APP_SEND",
            "devID": self.device_id,
            "msg": {
                "msg_ID": self._msg_id,
                "CMD_CODE": cmd_code,
                "rev_str1": rev_str1,
                "rev_str2": rev_str2,
                "rev_str3": rev_str3
            }
        }
        
        return json.dumps(message)
    
    def _construct_k2_message(self, cmd_code: int, rev_str1: str = "", 
                              rev_str2: str = "", rev_str3: str = "") -> bytes:
        """Construct K2 message (encoded)."""
        self._msg_id += 1
        
        message = {
            "action": "APP_SEND",
            "devID": self.device_id,
            "msg": {
                "msg_ID": self._msg_id,
                "CMD_CODE": cmd_code,
                "rev_str1": rev_str1,
                "rev_str2": rev_str2,
                "rev_str3": rev_str3
            }
        }
        
        return K2Codec.encode_k2_message(message)
    
    def _send_command(self, cmd_code: int, description: str, 
                      rev_str1: str = "", rev_str2: str = "", rev_str3: str = ""):
        """Send command using appropriate protocol."""
        if self.use_k2 or (self.force_protocol and self.force_protocol.lower() == "k2"):
            # K2: Send encrypted
            message = self._construct_k2_message(cmd_code, rev_str1, rev_str2, rev_str3)
            self.send_message(message, description)
        else:
            # K1: Send plain JSON
            message = self._construct_k1_message(cmd_code, rev_str1, rev_str2, rev_str3)
            self.send_message(message, description)
    
    async def receive_messages(self, timeout: float = 1.0):
        """Receive messages from the hub."""
        if not self.sock:
            return
        
        try:
            loop = asyncio.get_event_loop()
            data, addr = await asyncio.wait_for(
                loop.sock_recvfrom(self.sock, 4096),
                timeout=timeout
            )
            
            now = datetime.now()
            silence_duration = now - self.last_received
            if silence_duration > self.stats["max_silence_duration"]:
                self.stats["max_silence_duration"] = silence_duration
            
            self.last_received = now
            self.stats["messages_received"] += 1
            self.receive_count += 1
            
            # Detect and decode message
            is_k2 = K2Codec.is_k2_message(data)
            
            if is_k2:
                # K2 encrypted message
                decoded_json = K2Codec.decode_k2_message(data)
                if decoded_json:
                    decoded = json.dumps(decoded_json)
                    protocol = "K2"
                    self.stats["k2_messages"] += 1
                    
                    # Auto-detect K2
                    if not self.detected_protocol and not self.force_protocol:
                        self.detected_protocol = "K2"
                        self.use_k2 = True
                        self.logger.info(f"🔍 Protocol detected: K2")
                else:
                    decoded = f"<K2 decode failed, {len(data)} bytes>"
                    protocol = "K2?"
            else:
                # K1 plain text
                try:
                    decoded = data.decode("utf-8")
                    protocol = "K1"
                    self.stats["k1_messages"] += 1
                    
                    # Auto-detect K1
                    if not self.detected_protocol and not self.force_protocol:
                        self.detected_protocol = "K1"
                        self.use_k2 = False
                        self.logger.info(f"🔍 Protocol detected: K1")
                except UnicodeDecodeError:
                    decoded = f"<binary data, {len(data)} bytes>"
                    protocol = "???"
            
            log_entry = {
                "timestamp": now.isoformat(),
                "direction": "RECEIVED",
                "protocol": protocol,
                "message": decoded,
                "bytes": len(data),
                "raw": data.hex(),
                "source": f"{addr[0]}:{addr[1]}",
            }
            self.message_log.append(log_entry)
            
            self.logger.info(f"← RECV [{protocol}] {decoded[:100]}...")
            self.logger.debug(f"  From: {addr}")
            self.logger.debug(f"  Raw bytes: {data.hex()[:120]}...")
            
            # Parse message
            if is_k2 and decoded_json:
                self._parse_json_message(decoded_json)
            elif decoded.startswith("{"):
                try:
                    json_data = json.loads(decoded)
                    self._parse_json_message(json_data)
                except json.JSONDecodeError:
                    pass
            elif "IOT_KEY" in decoded:
                self.logger.info("  → IOT_KEY response detected")
            
        except asyncio.TimeoutError:
            pass
        except Exception as ex:
            self.logger.error(f"Error receiving data: {ex}")
            self.stats["errors"] += 1
    
    def _parse_json_message(self, json_data: dict):
        """Parse JSON message (K1 or K2)."""
        try:
            action = json_data.get("action", "")
            
            if action == "NODE_ACK":
                dev_id = json_data.get("devID", "")
                msg = json_data.get("msg", {})
                cmd_code = msg.get("CMD_CODE", "")
                self.logger.info(f"  → NODE_ACK from {dev_id}, CMD_CODE={cmd_code}")
            
            elif action == "APP_SEND" or action == "NODE_SEND":
                dev_id = json_data.get("devID", "")
                msg = json_data.get("msg", {})
                cmd_code = msg.get("CMD_CODE", "")
                msg_id = msg.get("msg_ID", "")
                rev_str1 = msg.get("rev_str1", "") or msg.get("data_str1", "")
                rev_str2 = msg.get("rev_str2", "") or msg.get("data_str2", "")
                
                cmd_name = self._get_command_name(cmd_code)
                
                self.logger.info(
                    f"  → {action}: devID={dev_id}, CMD={cmd_code}({cmd_name}), msgID={msg_id}"
                )
                
                # Parse specific responses
                if cmd_code == ElroCommands.UPLOAD_DEVICE_NAME:
                    self._parse_device_name(rev_str1, rev_str2)
                elif cmd_code == ElroCommands.UPLOAD_DEVICE_STATUS:
                    self._parse_device_status(rev_str1, rev_str2)
            
            else:
                self.logger.info(f"  → JSON action: {action}")
        
        except Exception as ex:
            self.logger.debug(f"  Could not parse JSON message: {ex}")
    
    def _parse_device_name(self, device_id_hex: str, name_hex: str):
        """Parse device name from hex."""
        try:
            if len(device_id_hex) >= 4:
                device_id = int(device_id_hex[:4], 16)
                if len(name_hex) >= 32:
                    name_bytes = bytes.fromhex(name_hex[:32])
                    name = ''.join(chr(b) for b in name_bytes if b != 0).replace('@', '').replace('$', '')
                    self.logger.info(f"    Device {device_id}: '{name}'")
        except Exception as e:
            self.logger.debug(f"    Could not parse device name: {e}")
    
    def _parse_device_status(self, device_id_hex: str, status_data: str):
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
                    f"    Device {device_id}: Type={device_type}, Battery={battery}, State={state}"
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
    
    async def send_iot_key_query(self):
        """Send IOT_KEY query to hub."""
        message = f"IOT_KEY?{self.device_id}"
        self.send_message(message, "IOT_KEY query")
    
    async def send_sync_devices(self):
        """Send sync devices command (CMD_CODE=29)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_DEVICE_STATUS,
            description="Sync device status (CMD=29)",
            rev_str1="",
            rev_str2=""
        )
    
    async def send_get_device_names(self):
        """Send get device names command (CMD_CODE=24)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_DEVICE_NAME,
            description="Get device names (CMD=24)",
            rev_str1="0",
            rev_str2=""
        )
    
    async def send_get_all_status(self):
        """Send get all device status command (CMD_CODE=54)."""
        self._send_command(
            cmd_code=ElroCommands.SYN_ALL_DEVICE_STATUS,
            description="Get all device status (CMD=54)",
            rev_str1="",
            rev_str2=""
        )
    
    async def test_connectivity(self):
        """Test basic connectivity to the hub."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info("Testing connectivity to ELRO Connects hub")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Protocol: {self.force_protocol.upper() if self.force_protocol else 'Auto-detect'}")
        self.logger.info(f"{'='*70}\n")
        
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
            self.logger.info("✓ IOT_KEY query successful\n")
        else:
            self.logger.error("✗ No response to IOT_KEY query\n")
            return False
        
        # Test 2: Sync devices (CMD_CODE=29)
        self.logger.info("Test 2: Requesting device sync (CMD_CODE=29)...")
        await self.send_sync_devices()
        
        initial_count = self.receive_count
        for _ in range(20):
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after sync request\n")
        else:
            self.logger.warning("⚠ No additional messages received after sync\n")
        
        # Test 3: Get device names (CMD_CODE=24)
        self.logger.info("Test 3: Requesting device names (CMD_CODE=24)...")
        await self.send_get_device_names()
        
        initial_count = self.receive_count
        for _ in range(20):
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after name request\n")
        else:
            self.logger.warning("⚠ No additional messages received\n")
        
        # Test 4: Get all status (CMD_CODE=54)
        self.logger.info("Test 4: Requesting all device status (CMD_CODE=54)...")
        await self.send_get_all_status()
        
        initial_count = self.receive_count
        for _ in range(20):
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after all status request\n")
        else:
            self.logger.warning("⚠ No additional messages received\n")
        
        self._print_statistics()
        return True
    
    async def monitor_mode(self, duration: int):
        """Monitor incoming messages for specified duration."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info(f"Starting monitoring mode for {duration} seconds")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Protocol: {self.force_protocol.upper() if self.force_protocol else 'Auto-detect'}")
        self.logger.info(f"{'='*70}\n")
        
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
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= duration:
                    break
                
                await self.receive_messages(timeout=0.5)
                
                silence = datetime.now() - self.last_received
                if silence.total_seconds() > 30:
                    self.logger.warning(f"⚠ No data received for {silence}")
                
                if (datetime.now() - last_status_check).total_seconds() >= 30:
                    self.logger.info("\n--- Sending periodic status check ---")
                    await self.send_sync_devices()
                    last_status_check = datetime.now()
                
                await asyncio.sleep(0.1)
        
        except KeyboardInterrupt:
            self.logger.info("\nMonitoring interrupted by user")
        finally:
            self._print_statistics()
    
    async def interactive_mode(self):
        """Interactive mode for manual testing."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info("Interactive mode - ELRO Connects Test Tool")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Protocol: {self.force_protocol.upper() if self.force_protocol else 'Auto-detect (current: {self.protocol})'}")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return
        
        self.running = True
        receiver_task = asyncio.create_task(self._receive_loop())
        
        print("\nAvailable commands:")
        print("  1 - Send IOT_KEY query")
        print("  2 - Sync devices (CMD_CODE=29)")
        print("  3 - Get device names (CMD_CODE=24)")
        print("  4 - Get all device status (CMD_CODE=54)")
        print("  p - Toggle protocol (K1/K2/Auto)")
        print("  s - Show statistics")
        print("  q - Quit")
        print()
        
        try:
            while self.running:
                try:
                    cmd = await asyncio.get_event_loop().run_in_executor(
                        None, input, f"[{self.protocol}] Command: "
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
                    elif cmd == "p":
                        if self.force_protocol is None:
                            self.force_protocol = "k2"
                            self.use_k2 = True
                            print("Forced K2 protocol")
                        elif self.force_protocol == "k2":
                            self.force_protocol = "k1"
                            self.use_k2 = False
                            print("Forced K1 protocol")
                        else:
                            self.force_protocol = None
                            self.use_k2 = False
                            print("Auto-detect protocol")
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
    
    async def _receive_loop(self):
        """Background task for receiving messages."""
        while self.running:
            try:
                await self.receive_messages(timeout=0.5)
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self.logger.error(f"Error in receive loop: {ex}")
    
    def _print_statistics(self):
        """Print communication statistics."""
        print(f"\n{'='*70}")
        print("Communication Statistics")
        print(f"{'='*70}")
        print(f"Protocol detected:  {self.protocol}")
        print(f"Messages sent:      {self.stats['messages_sent']}")
        print(f"Messages received:  {self.stats['messages_received']}")
        print(f"  - K1 messages:    {self.stats['k1_messages']}")
        print(f"  - K2 messages:    {self.stats['k2_messages']}")
        print(f"Errors:             {self.stats['errors']}")
        print(f"Max silence:        {self.stats['max_silence_duration']}")
        print(f"{'='*70}\n")
    
    def save_log(self, filename: str):
        """Save message log to file."""
        try:
            with open(filename, 'w') as f:
                json.dump({
                    "test_info": {
                        "host": self.host,
                        "device_id": self.device_id,
                        "port": self.port,
                        "timestamp": datetime.now().isoformat(),
                        "protocol_detected": self.protocol,
                    },
                    "statistics": {
                        k: str(v) if isinstance(v, timedelta) else v
                        for k, v in self.stats.items()
                    },
                    "messages": self.message_log,
                }, f, indent=2)
            self.logger.info(f"Log saved to {filename}")
        except Exception as ex:
            self.logger.error(f"Failed to save log: {ex}")
    
    def cleanup(self):
        """Clean up resources."""
        self.running = False
        if self.sock:
            self.sock.close()
            self.sock = None


def setup_logging(verbose: bool = False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)8s] %(message)s',
        datefmt='%H:%M:%S'
    )


async def main():
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
        """
    )
    
    parser.add_argument("--host", required=True, help="Hub IP address")
    parser.add_argument("--device-id", required=True, help="Device ID (e.g., ST_2342400722)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default: {DEFAULT_PORT})")
    parser.add_argument("--protocol", choices=['k1', 'k2'], help="Force protocol (default: auto-detect)")
    
    # Modes
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--test", action="store_true", help="Run connectivity tests")
    mode_group.add_argument("--monitor", type=int, metavar="SECONDS", help="Monitor mode for specified duration")
    mode_group.add_argument("--interactive", action="store_true", help="Interactive mode")
    
    # Options
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--save-log", metavar="FILE", help="Save message log to JSON file")
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    tool = ElroTestTool(
        host=args.host,
        device_id=args.device_id,
        port=args.port,
        force_protocol=args.protocol,
    )
    
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
        tool.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
