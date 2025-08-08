"""Config flow for ELRO Connects Real-time integration."""
from __future__ import annotations

import asyncio
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
    CONF_DEVICE_ID,
    CONF_HOST,
    DEFAULT_APP_ID,
    DEFAULT_CTRL_KEY,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required(CONF_DEVICE_ID): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
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

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    try:
        # Test UDP connection to the device
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5.0)
        
        # Try to send IOT_KEY query
        test_message = f'IOT_KEY?{data[CONF_DEVICE_ID]}'
        await hass.async_add_executor_job(
            sock.sendto, 
            test_message.encode('utf-8'), 
            (data[CONF_HOST], DEFAULT_PORT)
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
    return {"title": f"ELRO Connects Real-time Hub ({data[CONF_HOST]})"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ELRO Connects Real-time."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
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
                
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "device_id_example": "ST_dc4f224febfd",
                "host_example": "192.168.1.100"
            }
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
