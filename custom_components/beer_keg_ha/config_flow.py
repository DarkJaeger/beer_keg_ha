from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_WS_URL,
    CONF_EMPTY_WEIGHT,
    CONF_DEFAULT_FULL_WEIGHT,
    CONF_POUR_THRESHOLD,
    CONF_PER_KEG_FULL,
    CONF_FULL_VOLUME_L,
    CONF_BEER_SG,
    DEFAULT_FULL_VOLUME_L,
    DEFAULT_BEER_SG,
)


def _validate_ws_url(value: str) -> str:
    """Ensure the URL looks like a WebSocket URL."""
    v = (value or "").strip()
    if not (v.startswith("ws://") or v.startswith("wss://")):
        raise vol.Invalid("WebSocket URL must start with ws:// or wss://")
    return v


# -------------------------------------------------------------------
# INITIAL CONFIG FLOW (first-time setup)
# -------------------------------------------------------------------

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WS_URL): _validate_ws_url,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Beer Keg Scale config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """First step: ask for WebSocket URL."""
        if user_input is not None:
            # Use ws_url as unique_id just to avoid duplicate configs.
            await self.async_set_unique_id(user_input[CONF_WS_URL])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="Beer Keg Scale",
                data=user_input,  # contains CONF_WS_URL
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
        )

    async def async_step_import(self, user_input=None) -> FlowResult:
        """Handle import from YAML, if ever used."""
        return await self.async_step_user(user_input)


# -------------------------------------------------------------------
# OPTIONS FLOW (edit WS URL + advanced settings)
# -------------------------------------------------------------------

class BeerKegOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Beer Keg Scale options (edit WebSocket URL, thresholds, etc)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Main options step."""
        if user_input is not None:
            #
            # 1) Pull ws_url out of the submitted data and update the entry's data
            #
            new_ws_url = user_input.pop(
                CONF_WS_URL,
                self.config_entry.data.get(CONF_WS_URL, ""),
            )

            new_data = dict(self.config_entry.data)
            new_data[CONF_WS_URL] = new_ws_url

            #
            # 2) Merge remaining fields into options
            #
            new_options = dict(self.config_entry.options)
            new_options.update(user_input)

            # Update the config entry (data + options)
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
                options=new_options,
            )

            return self.async_create_entry(title="", data={})

        #
        # Show current values as defaults
        #
        current_ws = self.config_entry.data.get(CONF_WS_URL, "")

        opts = self.config_entry.options or {}

        schema = vol.Schema(
            {
                vol.Required(CONF_WS_URL, default=current_ws): _validate_ws_url,
                vol.Optional(
                    CONF_EMPTY_WEIGHT,
                    default=opts.get(CONF_EMPTY_WEIGHT, 0.0),
                ): float,
                vol.Optional(
                    CONF_DEFAULT_FULL_WEIGHT,
                    default=opts.get(CONF_DEFAULT_FULL_WEIGHT, 19.0),
                ): float,
                vol.Optional(
                    CONF_POUR_THRESHOLD,
                    default=opts.get(CONF_POUR_THRESHOLD, 0.15),
                ): float,
                vol.Optional(
                    CONF_PER_KEG_FULL,
                    default=opts.get(CONF_PER_KEG_FULL, "{}"),
                ): str,
                vol.Optional(
                    CONF_FULL_VOLUME_L,
                    default=opts.get(CONF_FULL_VOLUME_L, DEFAULT_FULL_VOLUME_L),
                ): float,
                vol.Optional(
                    CONF_BEER_SG,
                    default=opts.get(CONF_BEER_SG, DEFAULT_BEER_SG),
                ): float,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )


async def async_get_options_flow(
    config_entry: ConfigEntry,
) -> BeerKegOptionsFlowHandler:
    """Return the options flow handler."""
    return BeerKegOptionsFlowHandler(config_entry)

