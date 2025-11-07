#!/usr/bin/env python3
"""
ELRO Connects Real-time Diagnostic Tool

Command-line utility for testing and debugging ELRO Connects hub communication.
Helps diagnose connectivity issues and protocol differences between K1 and K2 connectors.
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

# Command constants (from your const.py)
class ElroCommands:
    """ELRO Connects command constants."""
    # Send commands
    EQUIPMENT_CONTROL = 1
    GET_DEVICE_NAME = 14
    GET_ALL_EQUIPMENT_STATUS = 15
    SYN_DEVICE_STATUS = 29
    SYN_DEVICE_NAME = 30
    
    # Receive commands
    DEVICE_NAME_REPLY = 17
    DEVICE_STATUS_UPDATE = 19
    DEVICE_ALARM_TRIGGER = 25
    SCENE_STATUS_UPDATE = 26


class ElroTestTool:
    """ELRO Connects diagnostic and testing tool."""
    
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
            
            self.logger.info(f"→ SENT: {message}")
            self.logger.debug(f"  Raw bytes: {encoded.hex()}")
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
            
            self.logger.info(f"← RECEIVED: {decoded}")
            self.logger.debug(f"  From: {addr}")
            self.logger.debug(f"  Raw bytes: {data.hex()}")
            
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
            # Check if it's a command response
            if decoded.startswith("IOT_KEY"):
                self.logger.info("  → IOT_KEY response detected")
            elif len(data) >= 2:
                # Try to parse as binary command
                command_id = struct.unpack(">H", data[:2])[0]
                command_name = self._get_command_name(command_id)
                if command_name:
                    self.logger.info(f"  → Command ID: {command_id} ({command_name})")
        except Exception as ex:
            self.logger.debug(f"  Could not parse message: {ex}")
    
    def _get_command_name(self, command_id: int) -> Optional[str]:
        """Get human-readable command name."""
        command_map = {
            17: "DEVICE_NAME_REPLY",
            19: "DEVICE_STATUS_UPDATE",
            25: "DEVICE_ALARM_TRIGGER",
            26: "SCENE_STATUS_UPDATE",
        }
        return command_map.get(command_id)
    
    async def send_iot_key_query(self):
        """Send IOT_KEY query to hub."""
        message = f"IOT_KEY?{self.device_id}"
        self.send_message(message)
    
    async def send_sync_devices(self):
        """Send sync devices command."""
        # Format: {DEVICE_CTRL_KEY}{APP_ID}{DEVICE_ID}{CMD_ID}
        message = f"{self.ctrl_key}{self.app_id}{self.device_id}{ElroCommands.SYN_DEVICE_STATUS}"
        self.send_message(message)
    
    async def send_get_device_names(self):
        """Send get device names command."""
        message = f"{self.ctrl_key}{self.app_id}{self.device_id}{ElroCommands.SYN_DEVICE_NAME}"
        self.send_message(message)
    
    async def send_get_all_status(self):
        """Send get all equipment status command."""
        message = f"{self.ctrl_key}{self.app_id}{self.device_id}{ElroCommands.GET_ALL_EQUIPMENT_STATUS}"
        self.send_message(message)
    
    async def monitor_mode(self, duration: int):
        """Monitor incoming messages for specified duration."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info(f"Starting monitoring mode for {duration} seconds")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return
        
        # Send initial IOT_KEY query
        await self.send_iot_key_query()
        
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
        
        # Test 2: Sync devices
        self.logger.info("Test 2: Requesting device sync...")
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
        
        # Test 3: Get device names
        self.logger.info("Test 3: Requesting device names...")
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
        
        self._print_statistics()
        return True
    
    async def interactive_mode(self):
        """Interactive mode for manual testing."""
        self.logger.info(f"\n{'='*70}")
        self.logger.info("Interactive mode - ELRO Connects Test Tool")
        self.logger.info(f"Hub: {self.host}:{self.port}")
        self.logger.info(f"Device ID: {self.device_id}")
        self.logger.info(f"{'='*70}\n")
        
        if not self.setup_socket():
            return
        
        self.running = True
        
        # Start receiver task
        receiver_task = asyncio.create_task(self._receive_loop())
        
        print("\nAvailable commands:")
        print("  1 - Send IOT_KEY query")
        print("  2 - Sync devices")
        print("  3 - Get device names")
        print("  4 - Get all equipment status")
        print("  5 - Send custom message")
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
                        msg = input("Enter message: ")
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
            print(f"  {entry['message']}")
            if 'source' in entry:
                print(f"  From: {entry['source']}")
            print(f"  Bytes: {entry['bytes']}, Raw: {entry['raw'][:60]}...")
        
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
        description="ELRO Connects Real-time Diagnostic Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test basic connectivity
  python elro_test_tool.py --host 192.168.1.100 --device-id ST_abc123 --test
  
  # Monitor for 5 minutes
  python elro_test_tool.py --host 192.168.1.100 --device-id ST_abc123 --monitor 300
  
  # Interactive mode with verbose logging
  python elro_test_tool.py --host 192.168.1.100 --device-id ST_abc123 --interactive -v
  
  # Save log to file
  python elro_test_tool.py --host 192.168.1.100 --device-id ST_abc123 --test --save-log test_results.json
        """
    )
    
    parser.add_argument("--host", required=True, help="Hub IP address")
    parser.add_argument("--device-id", required=True, help="Device ID (e.g., ST_abc123)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default: {DEFAULT_PORT})")
    parser.add_argument("--ctrl-key", default=DEFAULT_CTRL_KEY, help=f"Control key (default: {DEFAULT_CTRL_KEY})")
    parser.add_argument("--app-id", default=DEFAULT_APP_ID, help=f"App ID (default: {DEFAULT_APP_ID})")
    
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
        ctrl_key=args.ctrl_key,
        app_id=args.app_id
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
