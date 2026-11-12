"""Config flow for Dish Aligner.

Steps:
  user    - dependency gate (openwrt + esphome loaded), pick the two devices
  verify  - auto-resolve the entities each device exposes, enable any
            disabled OpenWrt sensors we need, sanity-check the uplink
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_ANGLE_ENTITY,
    CONF_AUTO_HOME,
    CONF_BITRATE_ENTITY,
    CONF_BUILTIN_SPEEDTEST,
    CONF_DISH_DEVICE,
    CONF_DWELL_SECONDS,
    CONF_EXCLUDED_APS,
    CONF_HOME_BUTTON,
    CONF_MOVING_ENTITY,
    CONF_NOISE_ENTITY,
    CONF_PENDING_RELOADS,
    CONF_QUALITY_ENTITY,
    CONF_RADIO_DEVICE,
    CONF_SIGNAL_ENTITY,
    CONF_STEP_DEGREES,
    CONF_SURVEY_STEP_DEGREES,
    CONF_SWEEP_DEGREES,
    CONF_TARGET_ENTITY,
    CONF_SPEEDTEST_SENSOR,
    CONF_VERIFY_SERVICE,
    CONF_VERIFY_SPEEDTEST,
    CONF_WIFI_INTERFACE,
    DEFAULT_AUTO_HOME,
    DEFAULT_BUILTIN_SPEEDTEST,
    DEFAULT_DWELL_SECONDS,
    DEFAULT_STEP_DEGREES,
    DEFAULT_SURVEY_STEP_DEGREES,
    DEFAULT_SWEEP_DEGREES,
    DEFAULT_WIFI_INTERFACE,
    DISH_ENTITY_SIGNATURES,
    DOMAIN,
    ESPHOME_DOMAIN,
    OPENWRT_DOMAIN,
    RADIO_ENTITY_SIGNATURES,
)
from .ubus import ubus_client_from_device

BAD_STATES = {"unavailable", "unknown", "none", ""}


def _entry_loaded(hass, domain: str) -> bool:
    """True if a config entry for `domain` exists and is loaded."""
    return any(
        e.state == ConfigEntryState.LOADED
        for e in hass.config_entries.async_entries(domain)
    )


def _match_entity(registry, device_id: str, domain: str, needle: str):
    """Find an entity on `device_id` whose id/name contains `needle`."""
    for entity in er.async_entries_for_device(registry, device_id):
        if entity.domain != domain:
            continue
        haystack = f"{entity.entity_id} {entity.original_name or ''}".lower()
        if needle in haystack:
            return entity
    return None


class DishAlignerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._missing: list[str] = []
        self._enabled: list[str] = []
        self._ifaces: list[str] = []

    # -- step 1: dependency gate + device pickers ---------------------------

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if not _entry_loaded(self.hass, OPENWRT_DOMAIN):
            return self.async_abort(reason="missing_openwrt")
        if not _entry_loaded(self.hass, ESPHOME_DOMAIN):
            return self.async_abort(reason="missing_esphome")

        if user_input is not None:
            self._data[CONF_DISH_DEVICE] = user_input[CONF_DISH_DEVICE]
            self._data[CONF_RADIO_DEVICE] = user_input[CONF_RADIO_DEVICE]
            return await self.async_step_packages()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DISH_DEVICE): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(integration=ESPHOME_DOMAIN)
                    ),
                    vol.Required(CONF_RADIO_DEVICE): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(integration=OPENWRT_DOMAIN)
                    ),
                }
            ),
            errors=errors,
        )

    # -- step 2: required packages on the radio ------------------------------

    async def _client(self):
        if getattr(self, "_ubus", None) is None:
            self._ubus = ubus_client_from_device(
                self.hass, self._data[CONF_RADIO_DEVICE]
            )
        return self._ubus

    async def async_step_packages(self, user_input=None):
        """Check iwinfo/iw/ACL on the radio; block until they're in place."""
        client = await self._client()
        status = await client.package_status()
        missing = [k for k, v in status.items() if not v]
        marks = "\n".join(
            f"{'✅' if ok else '❌'} `{name}`" for name, ok in status.items()
        )

        if missing:
            return self.async_show_menu(
                step_id="packages",
                menu_options=["recheck", "skip_packages"],
                description_placeholders={
                    "status": marks,
                    "missing": ", ".join(missing),
                },
            )
        return self.async_show_menu(
            step_id="packages",
            menu_options=["continue_setup"],
            description_placeholders={"status": marks, "missing": "none"},
        )

    async def async_step_recheck(self, user_input=None):
        return await self.async_step_packages()

    async def async_step_skip_packages(self, user_input=None):
        return await self.async_step_radio()

    async def async_step_continue_setup(self, user_input=None):
        return await self.async_step_radio()

    # -- step 3: pick which wireless interface on the radio ------------------

    async def async_step_radio(self, user_input=None):
        if user_input is not None:
            self._data[CONF_WIFI_INTERFACE] = user_input[CONF_WIFI_INTERFACE]
            return await self.async_step_verify()

        # Ask the radio which wireless interfaces it has; reuses the client
        # built in the packages step (creds come from the openwrt entry)
        try:
            client = await self._client()
            self._ifaces = await client.iwinfo_devices()
        except Exception:  # noqa: BLE001 - fall back to a text field
            self._ifaces = []

        if len(self._ifaces) == 1:
            self._data[CONF_WIFI_INTERFACE] = self._ifaces[0]
            return await self.async_step_verify()

        if not self._ifaces:
            schema = vol.Schema(
                {vol.Required(CONF_WIFI_INTERFACE, default=DEFAULT_WIFI_INTERFACE): str}
            )
        else:
            # Prefer the STA-mode iface for the default - that's the uplink
            sta = await self._sta_iface(client)
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_WIFI_INTERFACE, default=sta or self._ifaces[0]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._ifaces, mode="dropdown"
                        )
                    )
                }
            )
        return self.async_show_form(
            step_id="radio",
            data_schema=schema,
            description_placeholders={"iface_count": str(len(self._ifaces))},
        )

    async def _sta_iface(self, client) -> str | None:
        """Interface whose iwinfo mode is Client - the dish's uplink."""
        for dev in self._ifaces:
            try:
                info = await client.iwinfo_info(dev)
            except Exception:  # noqa: BLE001
                continue
            if info.get("mode") == "Client":
                return dev
        return None

    # -- step 4: resolve entities, enable what's needed, sanity-check -------

    async def async_step_verify(self, user_input=None):
        registry = er.async_get(self.hass)
        self._missing = []
        self._enabled = []

        resolved: dict[str, str] = {}

        for domain, needle, key, required in DISH_ENTITY_SIGNATURES:
            ent = _match_entity(registry, self._data[CONF_DISH_DEVICE], domain, needle)
            if ent is None:
                if required:
                    self._missing.append(f"{domain} matching '{needle}' on the rotator")
                continue
            resolved[key] = ent.entity_id

        for domain, needle, key, required in RADIO_ENTITY_SIGNATURES:
            ent = _match_entity(registry, self._data[CONF_RADIO_DEVICE], domain, needle)
            if ent is None:
                if required:
                    self._missing.append(f"{domain} matching '{needle}' on the radio")
                continue
            resolved[key] = ent.entity_id
            # Enable disabled entities we need (quality/bitrate/noise ship
            # disabled on the OpenWrt integration)
            if ent.disabled_by is not None:
                registry.async_update_entity(ent.entity_id, disabled_by=None)
                self._enabled.append(ent.entity_id)

        if self._missing:
            return self.async_abort(reason="missing_entities")

        self._data.update(resolved)
        if self._enabled:
            self._data[CONF_PENDING_RELOADS] = self._enabled

        # Sanity-check the uplink: a working iwinfo setup gives a real dBm
        # value; -0/unavailable means iwinfo or the rpcd ACL isn't in place.
        signal = resolved[CONF_SIGNAL_ENTITY]
        state = self.hass.states.get(signal)
        dbm_ok = False
        if state is not None and state.state.lower() not in BAD_STATES:
            try:
                dbm_ok = float(state.state) != 0
            except (TypeError, ValueError):
                pass

        return self.async_show_menu(
            step_id="verify",
            menu_options=["finish"] if dbm_ok else ["finish", "retry"],
            description_placeholders={
                "signal_entity": signal,
                "signal_state": state.state if state else "missing",
                "enabled_entities": ", ".join(self._enabled) or "none",
            },
        )

    async def async_step_finish(self, user_input=None):
        return self.async_create_entry(title="Dish Aligner", data=self._data)

    async def async_step_retry(self, user_input=None):
        return await self.async_step_verify()

    # -- options -------------------------------------------------------------

    @staticmethod
    def async_get_options_flow(config_entry):
        return DishAlignerOptionsFlow()


class DishAlignerOptionsFlow(config_entries.OptionsFlow):
    """Scan parameters and behaviour toggles."""

    async def async_step_init(self, user_input=None):
        opts = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_WIFI_INTERFACE,
                        default=opts.get(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE),
                    ): str,
                    vol.Optional(
                        CONF_SWEEP_DEGREES,
                        default=opts.get(CONF_SWEEP_DEGREES, DEFAULT_SWEEP_DEGREES),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=5, max=90, step=1, unit_of_measurement="°")
                    ),
                    vol.Optional(
                        CONF_STEP_DEGREES,
                        default=opts.get(CONF_STEP_DEGREES, DEFAULT_STEP_DEGREES),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=15, step=1, unit_of_measurement="°")
                    ),
                    vol.Optional(
                        CONF_DWELL_SECONDS,
                        default=opts.get(CONF_DWELL_SECONDS, DEFAULT_DWELL_SECONDS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=30, step=1, unit_of_measurement="s")
                    ),
                    vol.Optional(
                        CONF_SURVEY_STEP_DEGREES,
                        default=opts.get(CONF_SURVEY_STEP_DEGREES, DEFAULT_SURVEY_STEP_DEGREES),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=5, max=45, step=1, unit_of_measurement="°")
                    ),
                    vol.Optional(
                        CONF_AUTO_HOME,
                        default=opts.get(CONF_AUTO_HOME, DEFAULT_AUTO_HOME),
                    ): bool,
                    vol.Optional(
                        CONF_EXCLUDED_APS,
                        default=opts.get(CONF_EXCLUDED_APS, ""),
                    ): str,
                    vol.Optional(
                        CONF_BUILTIN_SPEEDTEST,
                        default=opts.get(
                            CONF_BUILTIN_SPEEDTEST, DEFAULT_BUILTIN_SPEEDTEST
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_VERIFY_SERVICE,
                        default=opts.get(CONF_VERIFY_SERVICE, ""),
                    ): str,
                    vol.Optional(CONF_VERIFY_SPEEDTEST): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["button", "script"])
                    ),
                    vol.Optional(CONF_SPEEDTEST_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )
