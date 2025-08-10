# ELRO Connects Real-time Home Assistant Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

A custom Home Assistant integration for ELRO Connects security devices with **real-time event processing**. This integration provides direct communication with your ELRO Connects hub, offering instant alarm notifications and device state changes.

## ✨ Key Features

- **🚀 Real-time Events**: Maintains persistent connection for instant alarm notifications
- **🔗 Direct Communication**: Communicates directly with the ELRO Connects hub via UDP, allowing events to be processed directly.
- **🔋 Battery Monitoring**: Track battery levels of wireless devices
- **🏠 Multiple Device Types**: Supports various ELRO Connects devices
- **🛠️ Service Calls**: Test alarms and sync devices via Home Assistant services
- **🔄 Auto Discovery**: Automatic device discovery and naming

## Supported Devices

| Device Type | Device Class | Features |
|-------------|--------------|----------|
| Door/Window Sensor | `door` | Open/Closed status, Battery level |
| Fire Alarm | `safety` | Alarm status, Battery level |
| CO Alarm | `safety` | Alarm status, Battery level |
| Heat Alarm | `safety` | Alarm status, Battery level |
| Water Alarm | `safety` | Alarm status, Battery level |

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
   - **Device ID**: Your hub's device identifier (e.g., `ST_ab4f224febfd`)
   - **Control Key**: Leave as default (`0`) unless specified otherwise
   - **App ID**: Leave as default (`0`) unless specified otherwise
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
```

### Network Requirements

- **Protocol**: UDP
- **Port**: 1025
- **Network**: Hub and Home Assistant must be on the same local network
- **Firewall**: Ensure UDP port 1025 is not blocked

## Development

### Setting Up Development Environment

1. Clone this repository
2. Create a virtual environment: `python -m venv venv`
3. Activate it: `source venv/bin/activate` (Linux/Mac) or `venv\Scripts\activate` (Windows)
4. Install dependencies: `pip install -r requirements_dev.txt`
5. Run tests: `pytest`

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes and add tests
4. Ensure all tests pass: `pytest`
5. Submit a pull request

## Protocol Documentation

This integration is based on reverse engineering of the ELRO Connects mobile app. The hub communicates using UDP on port 1025 with JSON messages.

### Message Format
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

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- **[@jbouw](https://github.com/jbouw)** for the excellent foundation work:
  - [ha-elro-connects](https://github.com/jbouwh/ha-elro-connects) - Original Home Assistant integration
  - [lib-elro-connects](https://github.com/jbouwh/lib-elro-connects) - Core ELRO Connects library
- This is my original reverse engineering and implementation with thanks for **[@hildensia]](https://github.com/hildensia)** for taking the code to a much higher level:
  - [elro_connects](https://github.com/dib0/elro_connects) - Original UDP communication implementation
- Thanks to the original Python script authors for reverse engineering the ELRO Connects protocol
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
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[issues]: https://github.com/dib0/ha-elro-connects-realtime/issues
