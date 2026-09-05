"""Config flow for ELRO Connects Real-time integration."""

from __future__ import annotations

import logging
import socket
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_APP_ID,
    CONF_CTRL_KEY,
    CONF_DEBUG_LOGGING,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PROTOCOL,
    DEFAULT_APP_ID,
    DEFAULT_CTRL_KEY,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_PORT,
    DOMAIN,
    PROTOCOL_AUTO,
    PROTOCOL_K1,
    PROTOCOL_K2,
)
from .detect import async_detect_protocol
from .discovery import DiscoveredHub, async_discover_hubs

_LOGGER = logging.getLogger(__name__)

# Field of the "which hub?" form, and the value of its last option. Neither is
# ever stored in a config entry, so they live here rather than in const.py.
CONF_SELECTED_HUB = "selected_hub"
SELECT_MANUAL = "manual"

STEP_MANUAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required(CONF_DEVICE_ID): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_PROTOCOL, default=PROTOCOL_AUTO): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[PROTOCOL_AUTO, PROTOCOL_K1, PROTOCOL_K2],
                translation_key="protocol",
            )
        ),
        vol.Optional(CONF_CTRL_KEY, default=DEFAULT_CTRL_KEY): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Optional(CONF_APP_ID, default=DEFAULT_APP_ID): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_MANUAL_DATA_SCHEMA with
    values provided by the user.

    Also resolves the hub generation: with the protocol left on "auto" the hub
    is probed once here and the answer is stored in the entry, so setup does not
    have to repeat the probe on every restart.
    """
    protocol = data.get(CONF_PROTOCOL, PROTOCOL_AUTO)
    if protocol == PROTOCOL_AUTO:
        protocol = await async_detect_protocol(data[CONF_HOST], data[CONF_DEVICE_ID])

    if protocol == PROTOCOL_K1:
        # The K2 handshake already ran (or the user picked K1); confirm the K1
        # hub is reachable with the plain-text query it understands.
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5.0)

            test_message = f"IOT_KEY?{data[CONF_DEVICE_ID]}"
            await hass.async_add_executor_job(
                sock.sendto,
                test_message.encode("utf-8"),
                (data[CONF_HOST], DEFAULT_PORT),
            )

            # Try to receive response (basic connectivity test)
            try:
                await hass.async_add_executor_job(sock.recv, 1024)
            except socket.timeout:
                # Timeout is acceptable as we just want to test connectivity
                pass
            finally:
                sock.close()

        except Exception as ex:
            _LOGGER.error("Error connecting to ELRO Connects hub: %s", ex)
            raise CannotConnect from ex

    # Return info that you want to store in the config entry.
    return {
        "title": f"ELRO Connects Real-time Hub ({data[CONF_HOST]})",
        CONF_PROTOCOL: protocol,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for ELRO Connects Real-time."""

    VERSION = 1

    def __init__(self) -> None:
        """Set up the state this flow carries between its steps."""
        # Written by async_step_user, read by async_step_pick_hub: the hubs the
        # broadcast scan found, keyed by device ID.
        self._discovered: dict[str, DiscoveredHub] = {}

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlow:
        """Return the options flow, which is what adds the "Configure" button.

        Home Assistant calls this from the event loop and it does no I/O, so the
        usual @callback decorator would only be documentation - and it types as
        untyped in a lint environment without Home Assistant installed, which is
        what CI runs.
        """
        return OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Scan the network for hubs and offer what was found.

        Only a K2 answers the discovery broadcast, so a household with a K1 hub
        drops straight through to the manual form and never sees this step, and
        so does one that is adding a hub alongside a K2 that is already running.
        """
        if self._async_port_holding_entries():
            # A configured K2 holds UDP port 1025 for its session. A second
            # socket on that port would be handed some of the datagrams meant
            # for it - an alarm push among them - so a further hub is added by
            # hand rather than by scanning past a live one.
            _LOGGER.debug(
                "Skipping hub discovery: a configured hub may be using UDP port %d",
                DEFAULT_PORT,
            )
            return await self.async_step_manual()

        configured = self._async_current_ids()
        self._discovered = {
            hub.device_id: hub
            for hub in await async_discover_hubs(self.hass)
            if hub.device_id not in configured
        }
        if not self._discovered:
            return await self.async_step_manual()
        return self._async_show_pick_hub_form()

    def _async_port_holding_entries(self) -> list[config_entries.ConfigEntry]:
        """Return the configured entries that may be holding UDP port 1025.

        Only a K2 session binds that port; a K1 entry sends from whatever source
        port the OS hands out, so it is not in the way. Anything not resolved to
        K1 counts as a maybe: an entry that has never finished a setup still has
        its protocol on "auto", and entries written before the values were
        lower-cased hold "K2" - both are normalised in __init__.py rather than
        here.
        """
        return [
            entry
            for entry in self._async_current_entries()
            if str(entry.data.get(CONF_PROTOCOL, PROTOCOL_AUTO)).lower() != PROTOCOL_K1
        ]

    async def async_step_pick_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure the hub picked from the discovery list."""
        if user_input is None:
            return self._async_show_pick_hub_form()

        selected = user_input[CONF_SELECTED_HUB]
        if selected == SELECT_MANUAL:
            return await self.async_step_manual()

        hub = self._discovered[selected]
        await self.async_set_unique_id(hub.device_id)
        # Already-configured hubs are filtered out of the list above, so this
        # only fires when a second flow got there first. The host is passed
        # along anyway: a hub that just answered is the best source there is for
        # where it currently lives.
        self._abort_if_unique_id_configured(updates={CONF_HOST: hub.host})

        # Nothing left to ask or probe: the hub answered the K2 handshake, which
        # settles the protocol, and ctrl_key/app_id are K1-only.
        _LOGGER.debug("Configuring discovered K2 hub %s at %s", selected, hub.host)
        return self.async_create_entry(
            title=f"ELRO Connects Real-time Hub ({hub.host})",
            data={
                CONF_HOST: hub.host,
                CONF_DEVICE_ID: hub.device_id,
                CONF_PROTOCOL: PROTOCOL_K2,
                CONF_CTRL_KEY: DEFAULT_CTRL_KEY,
                CONF_APP_ID: DEFAULT_APP_ID,
            },
        )

    def _async_show_pick_hub_form(self) -> FlowResult:
        """Show the discovered hubs, with manual entry as the way past them."""
        options = [
            selector.SelectOptionDict(
                value=hub.device_id, label=f"{hub.device_id} ({hub.host})"
            )
            for hub in self._discovered.values()
        ]
        # A select that carries its own labels shows them verbatim instead of
        # looking them up in the translations, which is what the hub entries
        # above need; this one is spelled out in English to match them.
        options.append(
            selector.SelectOptionDict(
                value=SELECT_MANUAL, label="Enter the hub details manually"
            )
        )
        return self.async_show_form(
            step_id="pick_hub",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SELECTED_HUB, default=next(iter(self._discovered))
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Set up a hub from details typed in by hand.

        The only route to a K1 hub, and the fallback whenever discovery comes up
        empty.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Check if already configured
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=info["title"],
                    data={**user_input, CONF_PROTOCOL: info[CONF_PROTOCOL]},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=STEP_MANUAL_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "device_id_example": "ST_dc4f224febfd",
                "host_example": "192.168.1.100",
            },
        )


class OptionsFlow(config_entries.OptionsFlow):
    """Handle the options of a configured hub.

    Only debug logging so far. It lives in the options rather than in the entry
    data so it can be flipped without re-running the config flow, and applying it
    does not reload the entry: see _async_options_updated in __init__.py.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # self.config_entry is provided by the framework; assigning it in
        # __init__ has been deprecated since Home Assistant 2024.11.
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEBUG_LOGGING,
                        default=self.config_entry.options.get(
                            CONF_DEBUG_LOGGING, DEFAULT_DEBUG_LOGGING
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
