"""Repairs notifications that say why a hub is not reporting any devices.

Both "no devices in Home Assistant" reports looked identical from the outside:
the integration set up without complaint and then simply stayed empty, with the
reason visible only to someone willing to turn on debug logging and read UDP
frames. One of them turned out to be a firewall rule blocking Chinese address
space — the hub apparently needs to reach the ELRO cloud before it answers
anything, even locally.

So the conclusions the log already draws are raised in Settings > Repairs as
well, where a user who never enables debug logging still sees them. The
notification clears itself as soon as the hub reports a device.

No @callback decorators here: these run from the event loop and do no I/O, but
the decorator types as untyped in a lint environment without Home Assistant
installed, which is what CI runs (see the note in config_flow.py).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_ID, CONF_HOST, DOMAIN
from .k2_hub import ElroK2Hub
from .models import ElroHub

_LOGGER = logging.getLogger(__name__)

# Which explanation the notification carries. The hub either says nothing at all
# (blocked in one direction or the other), answers without ever acknowledging an
# activation ping (so it is ignoring our commands - usually a Device ID that does
# not match), or answers normally and reports an empty device list.
ISSUE_HUB_SILENT = "hub_silent"
ISSUE_HUB_NOT_ARMED = "hub_not_armed"
ISSUE_HUB_NO_DEVICES = "hub_no_devices"

LEARN_MORE_URL = (
    "https://github.com/dib0/ha-elro-connects-realtime"
    "#no-devices-appear-in-home-assistant"
)

# Only the K2 hub counts frames, so a K1 gets the generic wording.
_NO_COUNTERS = "no device list was returned (frame counters are K2-only)"


def _issue_id(entry: ConfigEntry) -> str:
    """Return the issue ID for an entry.

    One issue per hub whatever the cause, so the two translation keys above
    replace each other instead of stacking up two cards about one hub.
    """
    return f"hub_not_reporting_{entry.entry_id}"


def async_clear_hub_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Withdraw the notification for a hub that is reporting devices again."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


def async_report_hub_not_reporting(
    hass: HomeAssistant, entry: ConfigEntry, hub: ElroHub
) -> None:
    """Raise the notification for a hub that produced no devices."""
    if isinstance(hub, ElroK2Hub):
        details = hub.traffic_summary()
        if hub.frames_received == 0:
            translation_key = ISSUE_HUB_SILENT
        elif hub.session_armed is False:
            translation_key = ISSUE_HUB_NOT_ARMED
        else:
            translation_key = ISSUE_HUB_NO_DEVICES
    else:
        details = _NO_COUNTERS
        translation_key = ISSUE_HUB_NO_DEVICES

    _LOGGER.warning(
        "Raising a repair notification for hub %s (%s): %s",
        entry.data[CONF_DEVICE_ID],
        translation_key,
        details,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders={
            "host": entry.data[CONF_HOST],
            "device_id": entry.data[CONF_DEVICE_ID],
            "details": details,
        },
        learn_more_url=LEARN_MORE_URL,
    )
