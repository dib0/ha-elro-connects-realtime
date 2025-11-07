#!/usr/bin/env python3
"""
ELRO Connects Real-time Diagnostic Tool

Uses the ACTUAL protocol format from decompiled Android app (SendCommand.java)
"""

import argparse
import asyncio
import json
import logging
import socket
import struct
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ELRO Protocol Constants
DEFAULT_PORT = 1025
DEFAULT_CTRL_KEY = "0"
DEFAULT_APP_ID = "0"

# Command constants (from SendCommand.java decompiled code)
class ElroCommands:
    """ELRO Connects command constants from actual Android app."""
    # Send commands
    EQUIPMENT_CONTROL = 1
    INCREASE_EQUIPMENT = 2
    REPLACE_EQUIPMENT = 3
    DELETE_EQUIPMENT = 4
    MODIFY_EQUIPMENT_NAME = 5
    CHOOSE_SCENE = 6
    CANCEL_INCREASE_EQUIPMENT = 7
    INCREASE_AUTOMATION = 8
    MODIFY_AUTOMATION = 9
    DELETE_AUTOMATION = 10
    SEND_ACK = 11
    SET_GATEWAY_INFO = 12
    UPLOAD_GATEWAY_INFO = 13
    GET_SUB_DEVICE_INFO = 16
    UPLOAD_DEVICE_NAME = 17
    UPLOAD_DEVICE_STATUS = 19
    INCREASE_SCENE = 23
    SYN_DEVICE_NAME = 24  # CORRECTED: Was 30 in our code!
    UPLOAD_SCENE_INFO = 26
    UPLOAD_SCENE_AUTO_INFO = 27
    UPLOAD_CURRENT_SCENE = 28
    SYN_DEVICE_STATUS = 29
    SYN_AUTOMATION = 30
    SYN_SCENE = 32
    SCENE_HANDLE = 38
    DELETE_SCENE = 39
    ALARM_LIST_SYNC = 44
    UPLOAD_ALARM_LOGS_INFO = 45
    DELETE_GATEWAY_LIST = 46
    SUB_DEVICE_ALARM_LIST_SYNC = 47
    UPLOAD_SUB_DEVICE_ALARM_LOGS_INFO = 48
    DELETE_SUB_DEVICE_ALARM_LIST = 49
    MODIFY_SUB_ROOM = 53
    SYN_ALL_DEVICE_STATUS = 54
    UPLOAD_ALL_DEVICE_STATUS = 55
    UPLOAD_ALL_DEVICE_STATUS2 = 56
    MODIFY_AUTOMATION_NAME = 57
    SYN_AUTOMATION_NAME = 58
    UPLOAD_AUTOMATION_NAME = 60
    SET_GATEWAY_VOICE = 61
    UPLOAD_ADD_DEVICE = 62
    UPLOAD_CO2_TH_2_4 = 64
    DELETE_CO2_TH_2_4_CHART = 65
    UPLOAD_SUB_DEVICE_INFO = 66
    GET_All_CO2_TH_2_4 = 67
    UPLOAD_GSM_INFO = 69
    SET_SIM_CODE_PHONE = 70
    SET_SIM_CODE_MESSAGE = 71
    GET_ALL_REPEATER_SUB_DEVICE = 75
    UPLOAD_REPEATER_SUB_DEVICE = 76
    SET_GATEWAY_WIFI = 77


class ElroTestTool:
    """ELRO Connects diagnostic and testing tool with CORRECT protocol."""
    
    def __init__(self, host: str, device_id: str, port: int = DEFAULT_PORT,
                 ctrl_key: str = DEFAULT_CTRL_KEY, app_id: str = DEFAULT_APP_ID):
        """Initialize the test tool."""
        self.host = host
        self.device_id = device_id
        self.port = port
        self.ctrl_key = ctrl_key
        self.app_id = app_id
        
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.last_received = datetime.now()
        self.receive_count = 0
        self.send_count = 0
        self.message_log: List[Dict[str, Any]] = []
        self._msg_id = 0  # For message tracking
        
        # Statistics
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "errors": 0,
            "reconnections": 0,
            "max_silence_duration": timedelta(0),
        }
        
        # Setup logging
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
    
    def send_message(self, message: str) -> bool:
        """Send a message to the hub."""
        if not self.sock:
            self.logger.error("Socket not initialized")
            return False
        
        try:
            encoded = message.encode("utf-8")
            self.sock.sendto(encoded, (self.host, self.port))
            self.stats["messages_sent"] += 1
            self.send_count += 1
            
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "direction": "SENT",
                "message": message,
                "bytes": len(encoded),
                "raw": encoded.hex(),
            }
            self.message_log.append(log_entry)
            
            self.logger.info(f"→ SENT: {message[:100]}...")
            self.logger.debug(f"  Raw bytes: {encoded.hex()[:120]}...")
            return True
        except Exception as ex:
            self.logger.error(f"Failed to send message: {ex}")
            self.stats["errors"] += 1
            return False
    
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
            
            # Try to decode as UTF-8
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                decoded = f"<binary data, {len(data)} bytes>"
            
            log_entry = {
                "timestamp": now.isoformat(),
                "direction": "RECEIVED",
                "message": decoded,
                "bytes": len(data),
                "raw": data.hex(),
                "source": f"{addr[0]}:{addr[1]}",
            }
            self.message_log.append(log_entry)
            
            self.logger.info(f"← RECEIVED: {decoded[:100]}...")
            self.logger.debug(f"  From: {addr}")
            self.logger.debug(f"  Raw bytes: {data.hex()[:120]}...")
            
            # Parse known message types
            self._parse_message(data, decoded)
            
        except asyncio.TimeoutError:
            pass
        except Exception as ex:
            self.logger.error(f"Error receiving data: {ex}")
            self.stats["errors"] += 1
    
    def _parse_message(self, data: bytes, decoded: str):
        """Parse and interpret received messages."""
        try:
            # Try to parse as JSON (ELRO format)
            if decoded.startswith("{"):
                try:
                    json_data = json.loads(decoded)
                    action = json_data.get("action", "")
                    
                    if action == "NODE_ACK":
                        dev_id = json_data.get("devID", "")
                        msg = json_data.get("msg", {})
                        cmd_code = msg.get("CMD_CODE", "")
                        self.logger.info(f"  → NODE_ACK from {dev_id}, CMD_CODE={cmd_code}")
                        
                    elif action == "APP_SEND":
                        # Response from hub
                        dev_id = json_data.get("devID", "")
                        msg = json_data.get("msg", {})
                        cmd_code = msg.get("CMD_CODE", "")
                        msg_id = msg.get("msg_ID", "")
                        rev_str1 = msg.get("rev_str1", "")
                        rev_str2 = msg.get("rev_str2", "")
                        
                        cmd_name = self._get_command_name(cmd_code)
                        
                        self.logger.info(
                            f"  → APP_SEND: devID={dev_id}, CMD={cmd_code}({cmd_name}), "
                            f"msgID={msg_id}, rev_str1={rev_str1[:20]}"
                        )
                        
                        # Parse specific responses
                        if cmd_code == ElroCommands.UPLOAD_DEVICE_NAME:
                            self._parse_device_name(rev_str1, rev_str2)
                        elif cmd_code == ElroCommands.UPLOAD_DEVICE_STATUS:
                            self._parse_device_status(rev_str1, rev_str2)
                            
                    else:
                        self.logger.info(f"  → JSON action: {action}")
                        
                except json.JSONDecodeError as e:
                    self.logger.debug(f"  Could not parse as JSON: {e}")
            
            # Check if it's an IOT_KEY response
            elif decoded.startswith("IOT_KEY"):
                self.logger.info("  → IOT_KEY response detected")
                
        except Exception as ex:
            self.logger.debug(f"  Could not parse message: {ex}")
    
    def _parse_device_name(self, device_id_hex: str, name_hex: str):
        """Parse device name from hex."""
        try:
            if len(device_id_hex) >= 4:
                device_id = int(device_id_hex[:4], 16)
                if len(name_hex) >= 32:
                    # Convert 32 hex chars to ASCII
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
    
    def _construct_message(self, cmd_code: int, rev_str1: str = "", 
                          rev_str2: str = "", rev_str3: str = "") -> str:
        """Construct message in ACTUAL ELRO UDP format from Android app."""
        self._msg_id += 1
        
        # This is the REAL format from SendCommand.java!
        message = {
            "action": "APP_SEND",  # NOT "appSend"!
            "devID": self.device_id,  # At root level, NOT in params!
            "msg": {  # NOT "params"!
                "msg_ID": self._msg_id,
                "CMD_CODE": cmd_code,  # NOT "cmdId"!
                "rev_str1": rev_str1,
                "rev_str2": rev_str2,
                "rev_str3": rev_str3
            }
        }
        
        return json.dumps(message)
    
    async def send_iot_key_query(self):
        """Send IOT_KEY query to hub."""
        message = f"IOT_KEY?{self.device_id}"
        self.send_message(message)
    
    async def send_sync_devices(self):
        """Send sync devices command (CMD_CODE=29)."""
        message = self._construct_message(
            cmd_code=ElroCommands.SYN_DEVICE_STATUS,
            rev_str1="",
            rev_str2=""
        )
        self.send_message(message)
    
    async def send_get_device_names(self):
        """Send get device names command (CMD_CODE=24)."""
        message = self._construct_message(
            cmd_code=ElroCommands.SYN_DEVICE_NAME,
            rev_str1="0",  # Device ID 0 = all devices
            rev_str2=""
        )
        self.send_message(message)
    
    async def send_get_all_status(self):
        """Send get all device status command (CMD_CODE=54)."""
        message = self._construct_message(
            cmd_code=ElroCommands.SYN_ALL_DEVICE_STATUS,
            rev_str1="",
            rev_str2=""
        )
        self.send_message(message)
    
    async def send_device_control(self, device_id: int, command: str):
        """Send device control command (CMD_CODE=1)."""
        device_id_hex = format(device_id, '04X')
        message = self._construct_message(
            cmd_code=ElroCommands.EQUIPMENT_CONTROL,
            rev_str1=device_id_hex,
            rev_str2=command
        )
        self.send_message(message)
    
    async def monitor_mode(self, duration: int):
        """Monitor incoming messages for specified duration."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info(f"Starting monitoring mode for {duration} seconds")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Using CORRECTED protocol from Android app")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return
        
        # Send initial IOT_KEY query
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
                
                # Receive messages
                await self.receive_messages(timeout=0.5)
                
                # Check for silence
                silence = datetime.now() - self.last_received
                if silence.total_seconds() > 30:
                    self.logger.warning(f"⚠ No data received for {silence}")
                
                # Send periodic status requests (every 30 seconds)
                if (datetime.now() - last_status_check).total_seconds() >= 30:
                    self.logger.info("\n--- Sending periodic status check ---")
                    await self.send_sync_devices()
                    last_status_check = datetime.now()
                
                await asyncio.sleep(0.1)
        
        except KeyboardInterrupt:
            self.logger.info("\nMonitoring interrupted by user")
        finally:
            self._print_statistics()
    
    async def test_connectivity(self):
        """Test basic connectivity to the hub."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info("Testing connectivity to ELRO Connects hub")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Using CORRECTED protocol from decompiled Android app!")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return False
        
        # Test 1: IOT_KEY query
        self.logger.info("Test 1: Sending IOT_KEY query...")
        await self.send_iot_key_query()
        
        # Wait for response
        response_received = False
        for _ in range(10):  # Wait up to 5 seconds
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
        
        # Wait for responses
        initial_count = self.receive_count
        for _ in range(20):  # Wait up to 10 seconds
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after sync request\n")
        else:
            self.logger.warning("⚠ No additional messages received after sync\n")
        
        # Test 3: Get device names (CMD_CODE=24, CORRECTED!)
        self.logger.info("Test 3: Requesting device names (CMD_CODE=24)...")
        await self.send_get_device_names()
        
        # Wait for responses
        initial_count = self.receive_count
        for _ in range(20):  # Wait up to 10 seconds
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after name request\n")
        else:
            self.logger.warning("⚠ No additional messages received\n")
        
        # Test 4: Get all status (CMD_CODE=54)
        self.logger.info("Test 4: Requesting all device status (CMD_CODE=54)...")
        await self.send_get_all_status()
        
        # Wait for responses
        initial_count = self.receive_count
        for _ in range(20):  # Wait up to 10 seconds
            await self.receive_messages(timeout=0.5)
        
        new_messages = self.receive_count - initial_count
        if new_messages > 0:
            self.logger.info(f"✓ Received {new_messages} messages after all status request\n")
        else:
            self.logger.warning("⚠ No additional messages received\n")
        
        self._print_statistics()
        return True
    
    async def interactive_mode(self):
        """Interactive mode for manual testing."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info("Interactive mode - ELRO Connects Test Tool")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"Using CORRECTED PROTOCOL!")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return
        
        self.running = True
        
        # Start receiver task
        receiver_task = asyncio.create_task(self._receive_loop())
        
        print("\nAvailable commands:")
        print("  1 - Send IOT_KEY query")
        print("  2 - Sync devices (CMD_CODE=29)")
        print("  3 - Get device names (CMD_CODE=24)")
        print("  4 - Get all device status (CMD_CODE=54)")
        print("  5 - Send custom APP_SEND message")
        print("  s - Show statistics")
        print("  l - Show message log")
        print("  q - Quit")
        print()
        
        try:
            while self.running:
                try:
                    cmd = await asyncio.get_event_loop().run_in_executor(
                        None, input, "Command: "
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
                    elif cmd == "5":
                        cmd_code = int(input("CMD_CODE: "))
                        rev_str1 = input("rev_str1: ")
                        rev_str2 = input("rev_str2: ")
                        rev_str3 = input("rev_str3 (optional): ")
                        msg = self._construct_message(cmd_code, rev_str1, rev_str2, rev_str3)
                        self.send_message(msg)
                    elif cmd == "s":
                        self._print_statistics()
                    elif cmd == "l":
                        self._print_message_log()
                    elif cmd == "q":
                        break
                    else:
                        print("Unknown command")
                except EOFError:
                    break
                except ValueError as e:
                    print(f"Invalid input: {e}")
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
        print(f"Messages sent:     {self.stats['messages_sent']}")
        print(f"Messages received: {self.stats['messages_received']}")
        print(f"Errors:            {self.stats['errors']}")
        print(f"Max silence:       {self.stats['max_silence_duration']}")
        print(f"{'='*70}\n")
    
    def _print_message_log(self, last_n: int = 20):
        """Print recent message log."""
        print(f"\n{'='*70}")
        print(f"Message Log (last {last_n} messages)")
        print(f"{'='*70}")
        
        for entry in self.message_log[-last_n:]:
            direction_symbol = "→" if entry["direction"] == "SENT" else "←"
            print(f"\n{direction_symbol} {entry['timestamp']}")
            msg = entry['message']
            if len(msg) > 200:
                msg = msg[:200] + "..."
            print(f"  {msg}")
            if 'source' in entry:
                print(f"  From: {entry['source']}")
            print(f"  Bytes: {entry['bytes']}")
        
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
                        "protocol": "CORRECTED - From Android app decompile"
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
        description="ELRO Connects Real-time Diagnostic Tool - CORRECTED PROTOCOL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This tool now uses the CORRECT protocol format from the decompiled Android app!

Protocol format (from SendCommand.java):
{
  "action": "APP_SEND",
  "devID": "ST_2342400722",
  "msg": {
    "msg_ID": 1,
    "CMD_CODE": 29,
    "rev_str1": "",
    "rev_str2": "",
    "rev_str3": ""
  }
}

Examples:
  # Test with corrected protocol
  python elro_test_tool_fixed.py --host 192.168.0.100 --device-id ST_2342400722 --test
  
  # Monitor for 5 minutes
  python elro_test_tool_fixed.py --host 192.168.0.100 --device-id ST_2342400722 --monitor 300
  
  # Interactive mode
  python elro_test_tool_fixed.py --host 192.168.0.100 --device-id ST_2342400722 --interactive -v
        """
    )
    
    parser.add_argument("--host", required=True, help="Hub IP address")
    parser.add_argument("--device-id", required=True, help="Device ID (e.g., ST_2342400722)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default: {DEFAULT_PORT})")
    
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
