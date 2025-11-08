"""Constants for the ELRO Connects Real-time integration."""

DOMAIN = "elro_connects_realtime"

# Configuration constants
CONF_HOST = "host"
CONF_DEVICE_ID = "device_id"
CONF_CTRL_KEY = "ctrl_key"
CONF_APP_ID = "app_id"

# Default values
DEFAULT_PORT = 1025
DEFAULT_CTRL_KEY = "0"
DEFAULT_APP_ID = "0"


# ELRO Connects command constants (from original code)
class ElroCommands:
    """ELRO Connects command constants."""

    # Send commands
    SWITCH_TIMER = -34
    DELETE_EQUIPMENT_DETAIL = -4
    EQUIPMENT_CONTROL = 1
    INCREACE_EQUIPMENT = 2
    REPLACE_EQUIPMENT = 3
    DELETE_EQUIPMENT = 4
    MODIFY_EQUIPMENT_NAME = 5
    CHOOSE_SCENE_GROUP = 6
    CANCEL_INCREACE_EQUIPMENT = 7
    INCREACE_SCENE = 8
    MODIFY_SCENE = 9
    DELETE_SCENE = 10
    GET_DEVICE_NAME = 14
    GET_ALL_EQUIPMENT_STATUS = 15
    GET_ALL_SCENE_INFO = 18
    TIME_CHECK = 21
    INCREACE_SCENE_GROUP = 23
    SYN_DEVICE_NAME = 24  # K2 command for device names
    SYN_DEVICE_STATUS = 29
    SYN_SCENE = 31
    SCENE_HANDLE = 32
    SCENE_GROUP_DELETE = 33
    MODEL_SWITCH_TIMER = 34
    MODEL_TIMER_SYN = 35
    UPLOAD_MODEL_TIMER = 36
    MODEL_TIMER_DEL = 37
    SYN_ALL_DEVICE_STATUS = 54  # K2 hub command
    SEND_TIMEZONE = 251

    # Receive commands
    DEVICE_NAME_REPLY = 17
    DEVICE_STATUS_UPDATE = 19
    DEVICE_ALARM_TRIGGER = 25
    SCENE_STATUS_UPDATE = 26


class ElroDeviceTypes:
    """ELRO device type constants."""

    CO_ALARM = "0000"
    WATER_ALARM = "0004"
    HEAT_ALARM = "0003"
    FIRE_ALARM = "0005"
    DOOR_WINDOW_SENSOR = "0101"


# Device state constants
DEVICE_STATE_UNKNOWN = "unknown"
DEVICE_STATE_NORMAL = "normal"
DEVICE_STATE_ALARM = "alarm"
DEVICE_STATE_OPEN = "open"
DEVICE_STATE_CLOSED = "closed"

# Attribute names
ATTR_DEVICE_ID = "device_id"
ATTR_DEVICE_TYPE = "device_type"
ATTR_BATTERY_LEVEL = "battery_level"
ATTR_LAST_SEEN = "last_seen"
