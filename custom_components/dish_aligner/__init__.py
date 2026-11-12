"""The Dish Aligner integration.

Backend for a motorized directional Wi-Fi dish:
  aim / auto_align / survey / join / home / abort services,
  plus a bundled radial Lovelace card auto-registered on the frontend.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_DWELL_SECONDS,
    CONF_PENDING_RELOADS,
    CONF_RADIO_DEVICE,
    CONF_STEP_DEGREES,
    CONF_SURVEY_STEP_DEGREES,
    CONF_SWEEP_DEGREES,
    DEFAULT_DWELL_SECONDS,
    DEFAULT_STEP_DEGREES,
    DEFAULT_SURVEY_STEP_DEGREES,
    DEFAULT_SWEEP_DEGREES,
    DOMAIN,
    OPENWRT_DOMAIN,
    SERVICE_ABORT,
    SERVICE_AIM,
    SERVICE_AUTO_ALIGN,
    SERVICE_HOME,
    SERVICE_SURVEY,
    SERVICE_JOIN,
)
from .controller import DishController
from .ubus import ubus_client_from_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]
CARD_URL = "/dish_aligner_frontend/dish-aligner-card.js"
ASSOC_REFRESH = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dish Aligner from a config entry."""
    ubus = ubus_client_from_device(hass, entry.data[CONF_RADIO_DEVICE])
    controller = DishController(hass, entry, ubus)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller

    # Entities we auto-enabled during setup only start reporting after the
    # openwrt entry re-creates them - reload it once, only if they're still
    # missing (so restarts don't bounce it needlessly)
    pending = [
        e for e in entry.data.get(CONF_PENDING_RELOADS, [])
        if hass.states.get(e) is None
    ]
    if pending:
        owrt = _openwrt_entry(hass, entry.data[CONF_RADIO_DEVICE])
        if owrt is not None:
            _LOGGER.info(
                "Reloading OpenWrt entry so auto-enabled entities appear: %s",
                pending,
            )
            hass.async_create_task(
                hass.config_entries.async_reload(owrt.entry_id)
            )

    if not hass.services.has_service(DOMAIN, SERVICE_AIM):
        _register_services(hass)

    # Keep the associated SSID/BSSID fresh for the card's connected-AP marker
    hass.async_create_task(controller.refresh_assoc())
    async def _assoc_tick(_now):
        await controller.refresh_assoc()

    entry.async_on_unload(
        async_track_time_interval(hass, _assoc_tick, ASSOC_REFRESH)
    )

    # Serve + auto-load the bundled card
    www = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig("/dish_aligner_frontend", str(www), cache_headers=False)]
    )
    frontend.add_extra_js_url(hass, CARD_URL)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _openwrt_entry(hass: HomeAssistant, device_id: str):
    """The openwrt-domain config entry that owns `device_id`."""
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        return None
    for entry_id in dev.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == OPENWRT_DOMAIN:
            return entry
    return None


def _controller(hass: HomeAssistant) -> DishController:
    controllers = list(hass.data.get(DOMAIN, {}).values())
    if not controllers:
        raise HomeAssistantError("Dish Aligner isn't set up")
    return controllers[0]


def _register_services(hass: HomeAssistant) -> None:
    async def aim(call: ServiceCall):
        await _controller(hass).aim(call.data["azimuth"])

    async def auto_align(call: ServiceCall):
        c = _controller(hass)
        await c.auto_align(
            call.data.get("sweep_degrees", c._opt(CONF_SWEEP_DEGREES, DEFAULT_SWEEP_DEGREES)),
            call.data.get("step_degrees", c._opt(CONF_STEP_DEGREES, DEFAULT_STEP_DEGREES)),
            call.data.get("dwell_seconds", c._opt(CONF_DWELL_SECONDS, DEFAULT_DWELL_SECONDS)),
        )

    async def survey(call: ServiceCall):
        c = _controller(hass)
        await c.survey(
            call.data.get("step_degrees", c._opt(CONF_SURVEY_STEP_DEGREES, DEFAULT_SURVEY_STEP_DEGREES)),
            call.data.get("dwell_seconds", c._opt(CONF_DWELL_SECONDS, DEFAULT_DWELL_SECONDS)),
        )

    async def join(call: ServiceCall):
        await _controller(hass).join(
            call.data["ssid"],
            call.data.get("password", ""),
            call.data.get("encryption"),
            call.data.get("bssid"),
        )

    async def home(call: ServiceCall):
        await _controller(hass).home()

    async def abort(call: ServiceCall):
        _controller(hass).abort()

    hass.services.async_register(DOMAIN, SERVICE_AIM, aim)
    hass.services.async_register(DOMAIN, SERVICE_AUTO_ALIGN, auto_align)
    hass.services.async_register(DOMAIN, SERVICE_SURVEY, survey)
    hass.services.async_register(DOMAIN, SERVICE_JOIN, join)
    hass.services.async_register(DOMAIN, SERVICE_HOME, home)
    hass.services.async_register(DOMAIN, SERVICE_ABORT, abort)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        controller = hass.data[DOMAIN].pop(entry.entry_id, None)
        if controller:
            controller.abort()
    return unload_ok
