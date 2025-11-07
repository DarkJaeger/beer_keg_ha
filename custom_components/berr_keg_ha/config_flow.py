from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from .const import (
    DOMAIN, CONF_WS_URL,
    CONF_EMPTY_WEIGHT, CONF_DEFAULT_FULL_WEIGHT, CONF_POUR_THRESHOLD, CONF_PER_KEG_FULL,
    CONF_FULL_VOLUME_L, CONF_BEER_SG, DEFAULT_FULL_VOLUME_L, DEFAULT_BEER_SG
)

STEP_USER_SCHEMA = vol.Schema({ vol.Required(CONF_WS_URL): str })

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_WS_URL])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Beer Keg Scale", data=user_input)
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

    async def async_step_import(self, user_input=None) -> FlowResult:
        return await self.async_step_user(user_input)

class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        data = self.entry.options or {}
        schema = vol.Schema({
            vol.Optional(CONF_EMPTY_WEIGHT, default=data.get(CONF_EMPTY_WEIGHT, 0.0)): float,
            vol.Optional(CONF_DEFAULT_FULL_WEIGHT, default=data.get(CONF_DEFAULT_FULL_WEIGHT, 19.0)): float,
            vol.Optional(CONF_POUR_THRESHOLD, default=data.get(CONF_POUR_THRESHOLD, 0.15)): float,
            vol.Optional(CONF_PER_KEG_FULL, default=data.get(CONF_PER_KEG_FULL, "{}")): str,
            vol.Optional(CONF_FULL_VOLUME_L, default=data.get(CONF_FULL_VOLUME_L, DEFAULT_FULL_VOLUME_L)): float,
            vol.Optional(CONF_BEER_SG, default=data.get(CONF_BEER_SG, DEFAULT_BEER_SG)): float,
        })
        return self.async_show_form(step_id="init", data_schema=schema)

async def async_get_options_flow(config_entry):
    return OptionsFlowHandler(config_entry)
