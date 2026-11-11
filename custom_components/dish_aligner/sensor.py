"""Sensors exposing Dish Aligner controller state.

  sensor.<name>_status     idle|homing|aiming|aligning|surveying|joining
  sensor.<name>_survey     AP count; attrs = deduped AP list (best first)
  sensor.<name>_alignment  last auto-align result {azimuth, dbm, verified}
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_DISH_DEVICE, DOMAIN
from .controller import DishController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    controller: DishController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DishStatusSensor(controller, entry),
            DishSurveySensor(controller, entry),
            DishAlignmentSensor(controller, entry),
        ]
    )


class _DishBase(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, controller: DishController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class DishStatusSensor(_DishBase):
    _attr_name = "Status"
    _attr_icon = "mdi:satellite-uplink"

    def __init__(self, controller, entry):
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self):
        return self._controller.status

    @property
    def extra_state_attributes(self):
        attrs = {"azimuth": self._controller.azimuth}
        if self._controller.progress:
            attrs["progress"] = self._controller.progress
        conn = self._controller.connected
        if conn:
            if conn.get("ssid"):
                attrs["connected_ssid"] = conn["ssid"]
            if conn.get("bssid"):
                attrs["connected_bssid"] = conn["bssid"]
        if self._controller.last_error:
            attrs["last_error"] = self._controller.last_error
        return attrs


class DishSurveySensor(_DishBase, RestoreEntity):
    _attr_name = "Survey"
    _attr_icon = "mdi:wifi-marker"

    def __init__(self, controller, entry):
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_survey"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Repopulate the AP list after a restart so a re-survey (which drops
        # the uplink for minutes) isn't needed just to redraw the card
        last = await self.async_get_last_state()
        if last is not None and (aps := last.attributes.get("access_points")):
            self._controller.survey_results = list(aps)
            self._controller._notify()

    @property
    def native_value(self):
        return len(self._controller.survey_results)

    @property
    def extra_state_attributes(self):
        return {"access_points": self._controller.survey_results}


class DishAlignmentSensor(_DishBase, RestoreEntity):
    _attr_name = "Last alignment"
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, controller, entry):
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_alignment"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", "none"):
            self._controller.alignment = dict(last.attributes)

    @property
    def native_value(self):
        a = self._controller.alignment
        return a["azimuth"] if a else None

    @property
    def extra_state_attributes(self):
        return self._controller.alignment or {}
