# ELRO Connects Real-time Home Assistant Integration
[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

[![CI](https://github.com/dib0/ha-elro-connects-realtime/actions/workflows/ci.yml/badge.svg)](https://github.com/dib0/ha-elro-connects-realtime/actions/workflows/ci.yml)

A custom Home Assistant integration for ELRO Connects K1 and K2 security devices with **real-time event processing**. This integration provides direct communication with your ELRO Connects hub (K1 and K2), offering instant alarm notifications and device state changes.

K2 hubs (SF50GA) are handled by the [elro-connects-k2-protocol][k2lib] library by
[@ldebruijn](https://github.com/ldebruijn), which does all K2 wire work: XOR framing,
command routing, status decoding and the device profile registry. K1 hubs keep using this
integration's own plain-text UDP implementation.

## ✨ Key Features

- **🚀 Real-time Events**: Maintains persistent connection for instant alarm notifications
- **🔗 Direct Communication**: Communicates directly with the ELRO Connects hub via UDP, allowing events to be processed directly.
- **🔋 Battery Monitoring**: Track battery levels of wireless devices
- **🏠 Multiple Device Types**: Supports various ELRO Connects devices
- **🛠️ Service Calls**: Test alarms and sync devices via Home Assistant services
- **🔄 Auto Discovery**: Automatic device discovery and naming

## Supported Devices

### K1 hubs

| Device Type | Device Class | Features |
|-------------|--------------|----------|
| Door/Window Sensor | `door` | Open/Closed status, Battery level |
| Fire Alarm | `safety` | Alarm status, Battery level |
| CO Alarm | `safety` | Alarm status, Battery level |
| Heat Alarm | `safety` | Alarm status, Battery level |
| Water Alarm | `safety` | Alarm status, Battery level |

### K2 hubs

Entities come from the device profile registry in [elro-connects-k2-protocol][k2lib], so a
device gets one entity per hazard it actually reports plus battery and signal:

| Device Type | Entities |
|-------------|----------|
| Smoke alarm (GS530D, GS559A, GS592A, GS556) | `smoke` |
| CO alarm (GS816A, GS818A, GS827W) | `carbon_monoxide` |
| Gas alarm (GS870W, GS871A) | `gas` |
| CO + Gas combi (GS891A) | `carbon_monoxide` + `gas` |
| Heat alarm (GS412D/A) | `heat` |
| Water alarm (GS156D/A) | `moisture` |
| CO2/Temperature/Humidity (GS241A) | CO2, temperature and humidity sensors |
| Door/Window sensor (GS320D) | `door` |
| PIR motion sensor | `motion` |
| Radiator thermostat (GS361) | valve, open-window, setpoint, temperature, mode |
| Sockets, sirens, buttons, repeaters | battery and signal only |

Every K2 device also gets a battery sensor (skipped for mains-powered devices) and a signal
sensor (disabled by default; enable it in the entity settings when you need it).

## Installation

<!--### HACS (Recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed
2. In the HACS panel, go to "Integrations"
3. Click the "+" button and search for "ELRO Connects Real-time"
4. Install the integration
5. Restart Home Assistant
6. Go to Configuration → Integrations
7. Click "+" and search for "ELRO Connects Real-time"
8. Follow the configuration steps
-->
### Manual Installation

1. Download the latest release from the [releases page][releases]
2. Extract the archive
3. Copy the `elro_connects_realtime` directory to your `custom_components` directory
4. Restart Home Assistant
5. Go to Configuration → Integrations
6. Click "+" and search for "ELRO Connects Real-time"
7. Follow the configuration steps

## Configuration

### Prerequisites

Before setting up the integration, you need:

1. **ELRO Connects Hub**: A functioning ELRO Connects hub connected to your network
2. **Hub IP Address**: The local IP address of your hub (e.g., `192.168.1.100`)
3. **Device ID**: The unique identifier of your hub (usually starts with `ST_`)

### Finding Your Hub Information

#### Method 1: Router/Network Scanner
- Check your router's device list for "ELRO" or similar device
- Use a network scanner app to find devices on port 1025

#### Method 2: ELRO Connects Mobile App
- Open the ELRO Connects mobile app
- Go to hub settings to find the Device ID
- The IP address can be found in your router's DHCP client list

#### Method 3: Network Traffic Analysis
- Use Wireshark or similar tool to capture UDP traffic on port 1025
- Look for messages containing device identifiers starting with `ST_`

### Setup Steps

1. Go to **Configuration** → **Integrations**
2. Click the **"+"** button
3. Search for **"ELRO Connects Real-time"**
4. Enter your hub information:
   - **IP Address**: Your hub's local IP address
   - **Device ID**: Your hub's device identifier (e.g., `ST_ab4f224febfd` (This is case sensitive. For the K2 it has to be uppercase: `ST_AB4F224FEBFD`))
   - **Hub protocol**: Leave on `Auto-detect` unless detection picks the wrong one. The
     probe sends the K2 handshake and falls back to K1 when there is no K2 answer; the
     result is stored in the config entry, so it only runs once per hub. Pick `K1`/`K2`
     explicitly to skip it.
   - **Control Key**: Leave as default (`0`) unless specified otherwise (K1 only)
   - **App ID**: Leave as default (`0`) unless specified otherwise (K1 only)
5. Click **Submit**

The integration will automatically discover and configure your devices.

## Usage

### Entities

After setup, you'll see entities for each of your ELRO Connects devices:

#### Binary Sensors
- **Door/Window Sensors**: Show as `binary_sensor.device_name_door_window`
  - State: `on` (open) / `off` (closed)
- **Alarm Devices**: Show as `binary_sensor.device_name_alarm`
  - State: `on` (alarm triggered) / `off` (normal)

#### Sensors
- **Battery Levels**: Show as `sensor.device_name_battery`
  - Value: Battery percentage (0-100%)
- **K2 only** — signal strength (`sensor.device_name_signal`, 1-4 bars, disabled by
  default) plus any measurements the device reports, such as
  `sensor.device_name_co2`, `sensor.device_name_temperature` and
  `sensor.device_name_humidity`

### Services

The integration provides several services for device management:

#### `elro_connects_realtime.test_alarm`
Test the alarm on a specific device.

```yaml
service: elro_connects_realtime.test_alarm
target:
  entity_id: binary_sensor.smoke_detector_alarm
```

#### `elro_connects_realtime.sync_devices`
Force synchronization of all devices.

```yaml
service: elro_connects_realtime.sync_devices
```

#### `elro_connects_realtime.get_device_names`
Refresh device names from the hub.

```yaml
service: elro_connects_realtime.get_device_names
```

### Automation Examples

#### Fire Alarm Notification
```yaml
automation:
  - alias: "Fire Alarm Triggered"
    trigger:
      - platform: state
        entity_id: binary_sensor.fire_alarm_alarm
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "🔥 FIRE ALARM!"
          message: "Fire alarm has been triggered!"
          data:
            priority: high
            ttl: 0
```

#### Door/Window Monitor
```yaml
automation:
  - alias: "Door Left Open"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_door_window
        to: "on"
        for: "00:05:00"
    action:
      - service: notify.persistent_notification
        data:
          title: "Door Alert"
          message: "Front door has been open for 5 minutes"
```

#### Low Battery Alert
```yaml
automation:
  - alias: "Low Battery Alert"
    trigger:
      - platform: numeric_state
        entity_id: 
          - sensor.smoke_detector_battery
          - sensor.door_sensor_battery
        below: 20
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Low Battery"
          message: "{{ trigger.to_state.attributes.friendly_name }} battery is at {{ trigger.to_state.state }}%"
```

## Troubleshooting

### Common Issues

#### K2: port 1025 already in use
The K2 only answers requests that come from local UDP port 1025, so the integration binds
it. Nothing else on the Home Assistant host may hold that port — including a second copy of
this integration or `elro_test_tool.py` running on the same machine.

#### Connection Failed
- Verify the hub IP address is correct
- Ensure the hub is powered on and connected to your network
- Check that port 1025 is not blocked by your firewall
- Try pinging the hub IP address from your Home Assistant host

#### No Devices Discovered
- Wait a few minutes after setup for initial device discovery
- Use the `elro_connects.sync_devices` service to force discovery
- Check that your devices are properly paired with the hub
- Ensure devices have sufficient battery level

#### Devices Show as Unavailable
- Check device battery levels
- Verify devices are within range of the hub
- Use the `elro_connects.get_device_names` service to refresh
- Restart the integration if issues persist

### Debug Logging

Enable debug logging to troubleshoot issues:

```yaml
logger:
  default: info
  logs:
    custom_components.elro_connects_realtime: debug
    elro_connects_k2_protocol: debug   # K2 wire-level decoding
```

### Diagnostic Tools

Both tools talk to the hub outside Home Assistant. Stop Home Assistant first when testing a
K2 hub, or run them from another machine — see the port note above.

Use `python3` explicitly - on some systems `python` is still Python 2, which cannot parse
these files - and note that the K2 library needs Python 3.12 or newer.

```bash
python3 -m pip install --user elro-connects-k2-protocol==0.1.0

# Connectivity test, protocol auto-detected
python3 elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --test

# Watch live events for 5 minutes
python3 elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --monitor 300

# Interactive: sync, names, gateway info, alarm test/silence, pairing
python3 elro_test_tool.py --host 192.168.0.100 --device-id ST_2342400722 --interactive -v

# Minimal K2-only smoke test (edit HUB_IP / DEVICE_ID at the top of the file)
python3 test_elro_k2.py
```

On a distro-managed Python (Debian/Ubuntu), `pip` refuses to install with a PEP 668
"externally-managed-environment" error. Either add `--break-system-packages` to the command
above, use a virtualenv (`apt install python3-venv` first if `python3 -m venv` fails), or
skip installing altogether and point `PYTHONPATH` at a checkout of the library:

```bash
git clone https://github.com/ldebruijn/elro-connects-k2-protocol.git
PYTHONPATH=./elro-connects-k2-protocol python3 elro_test_tool.py --host 192.168.0.100 \
    --device-id ST_2342400722 --test
```

Home Assistant itself is unaffected: it installs the library from `manifest.json` into its
own environment.

### Network Requirements

- **Protocol**: UDP
- **Port**: 1025
- **Network**: Hub and Home Assistant must be on the same local network
- **Firewall**: Ensure UDP port 1025 is not blocked

## Development

### Setting Up Development Environment

1. Clone this repository
2. Create a virtual environment: `python3 -m venv venv` (Python 3.12 or newer)
3. Activate it: `source venv/bin/activate` (Linux/Mac) or `venv\Scripts\activate` (Windows)
4. Install dependencies: `pip install -r requirements_dev.txt`
5. Run tests: `pytest`
6. Run the same checks as CI:
   ```bash
   black --check custom_components/ elro_test_tool.py
   isort --check-only custom_components/ elro_test_tool.py
   mypy custom_components/ elro_test_tool.py
   ```
   The lint pins in `requirements_dev.txt` match `.github/workflows/ci.yml`; keep them in
   sync so local runs and CI agree.

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes and add tests
4. Ensure all tests pass: `pytest`
5. Submit a pull request

## Protocol Documentation

This integration is based on reverse engineering of the ELRO Connects mobile app. Both hub
generations communicate using UDP on port 1025 with JSON messages.

### K1 message format
Plain UTF-8 JSON:
```json
{
  "msgId": 1,
  "action": "appSend",
  "params": {
    "devTid": "ST_deviceid",
    "ctrlKey": "0",
    "appTid": "0",
    "data": {
      "cmdId": 29,
      "device_status": ""
    }
  }
}
```

### K2 message format
The same JSON, XOR-framed (byte 0 is a random seed, every following byte is XORed with
`seed ^ 0x23`) and wrapped in the `APP_SEND`/`CMD_CODE` envelope. All of that is
implemented in [elro-connects-k2-protocol][k2lib]; its
[protocol reference](https://github.com/ldebruijn/elro-connects-k2-protocol/blob/main/docs/protocol_reference.md)
documents the full command set.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- **[@ldebruijn](https://github.com/ldebruijn)** for working out the K2 protocol and
  publishing it as a library, which this integration uses for all K2 communication:
  - [elro-connects-k2-protocol][k2lib] - K2 local UDP protocol library
  - [elro-connects-k2-ha](https://github.com/ldebruijn/elro-connects-k2-ha) - his own K2 integration
- **[@jbouw](https://github.com/jbouw)** for the excellent foundation work:
  - [ha-elro-connects](https://github.com/jbouwh/ha-elro-connects) - Original Home Assistant integration
  - [lib-elro-connects](https://github.com/jbouwh/lib-elro-connects) - Core ELRO Connects library
- This is my original reverse engineering and implementation with thanks for **[@hildensia](https://github.com/hildensia)** for taking the code to a much higher level:
  - [elro_connects](https://github.com/dib0/elro_connects) - Original UDP communication implementation
- Home Assistant community for integration development guidelines
- ELRO for creating an accessible IoT ecosystem

## Support

- [Issues][issues]: Report bugs or request features
- [Discussions](https://github.com/dib0/ha-elro-connects-realtime/discussions): Ask questions or share ideas
- [Home Assistant Community](https://community.home-assistant.io/): General Home Assistant support

---

**Disclaimer**: This integration is not officially supported by ELRO. Use at your own risk.

[releases-shield]: https://img.shields.io/github/release/dib0/ha-elro-connects-realtime.svg?style=for-the-badge
[releases]: https://github.com/dib0/ha-elro-connects-realtime/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/dib0/ha-elro-connects-realtime.svg?style=for-the-badge
[commits]: https://github.com/dib0/ha-elro-connects-realtime/commits/main
[license-shield]: https://img.shields.io/github/license/dib0/ha-elro-connects-realtime.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[issues]: https://github.com/dib0/ha-elro-connects-realtime/issues
[k2lib]: https://github.com/ldebruijn/elro-connects-k2-protocol
