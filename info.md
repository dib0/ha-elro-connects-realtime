## ELRO Connects Real-time

Real-time Home Assistant integration for ELRO Connects K1 and K2 security devices.

### Features
- Direct UDP communication (so events from connected devices are handled directly)
- Real-time event processing
- Battery monitoring for wireless devices
- Debug logging toggle in the integration options, which logs every UDP frame exchanged
  with the hub (and the protocol library's own decoding) for troubleshooting
- Support for multiple device types
- K2 (SF50GA) hubs are driven by the
  [elro-connects-k2-protocol](https://github.com/ldebruijn/elro-connects-k2-protocol)
  library, which resolves each device to a profile and creates one entity per hazard

### Supported Devices
- Door/Window sensors
- Fire/Smoke alarms
- CO alarms
- Gas alarms (K2)
- Heat alarms
- Water alarms
- CO2/temperature/humidity detectors (K2)
- Radiator thermostats (K2)

### Credits

Many thanks to [@ldebruijn](https://github.com/ldebruijn) for reverse engineering the ELRO
Connects K2 protocol and publishing it as a library - all K2 communication in this
integration runs on that work:

- [elro-connects-k2-protocol](https://github.com/ldebruijn/elro-connects-k2-protocol) - the
  K2 protocol library this integration depends on
- [elro-connects-k2-ha](https://github.com/ldebruijn/elro-connects-k2-ha) - a K2-only Home
  Assistant integration built on the same library
