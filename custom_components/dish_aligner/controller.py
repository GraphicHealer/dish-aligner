"""Dish orchestration: aim, auto-align, survey, join, home, abort.

Movement model: set number.*_azimuth_target -> binary_sensor.*_azimuth_moving
turns on -> wait for it to clear -> dwell -> measure. Position is open-loop
so surveys optionally re-home first.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    CONF_QUALITY_ENTITY,
    CONF_RADIO_DEVICE,
    CONF_SIGNAL_ENTITY,
    CONF_SPEEDTEST_SENSOR,
    CONF_STEP_DEGREES,
    CONF_SURVEY_STEP_DEGREES,
    CONF_SWEEP_DEGREES,
    CONF_TARGET_ENTITY,
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
    OPENWRT_DOMAIN,
    SIGNAL_SAMPLES,
    SPEEDTEST_DURATION,
    SPEEDTEST_RESULT_TIMEOUT,
    SPEEDTEST_URL,
)
from .ubus import UbusClient, UbusError

_LOGGER = logging.getLogger(__name__)

SETTLE_POLL = 0.2          # s between moving-sensor polls
MOVE_START_TIMEOUT = 5.0   # s to see "moving" after commanding a target
MOVE_TIMEOUT = 30.0        # s for any single move to complete
HOME_TIMEOUT = 60.0        # s for the whole homing routine incl. return move
HOME_SCRIPT_GRACE = 13.5   # home_azimuth's fixed 12s delay + margin
SAMPLE_SPACING = 0.5       # s between signal samples at a stop
JOIN_TIMEOUT = 45.0        # s to wait for the new uplink to come up

STATUS_IDLE = "idle"
STATUS_HOMING = "homing"
STATUS_AIMING = "aiming"
STATUS_ALIGNING = "aligning"
STATUS_SURVEYING = "surveying"
STATUS_JOINING = "joining"
STATUS_FINDING = "finding"

# WPA auth -> OpenWrt wireless.encryption value
ENCRYPTION_MAP = {
    "sae": "sae-mixed",
    "psk": "psk2",
    "owe": "owe",
}


class DishController:
    """Runs the dish state machine for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ubus: UbusClient) -> None:
        self.hass = hass
        self.entry = entry
        self.ubus = ubus

        data = entry.data
        self.angle_entity: str = data[CONF_ANGLE_ENTITY]
        self.target_entity: str = data[CONF_TARGET_ENTITY]
        self.moving_entity: str = data[CONF_MOVING_ENTITY]
        self.home_button: str | None = data.get(CONF_HOME_BUTTON)
        self.signal_entity: str = data[CONF_SIGNAL_ENTITY]
        self.quality_entity: str | None = data.get(CONF_QUALITY_ENTITY)
        self.bitrate_entity: str | None = data.get(CONF_BITRATE_ENTITY)
        self.noise_entity: str | None = data.get(CONF_NOISE_ENTITY)

        self.status = STATUS_IDLE
        self.progress: dict | None = None        # {"step": i, "total": n, "azimuth": a}
        self.survey_results: list[dict] = []     # deduped APs, best-first
        self.alignment: dict | None = None       # last auto-align result
        self.connected: dict | None = None       # {"ssid": ..., "bssid": ...}
        self.last_error: str | None = None       # last failed op's message, for the card

        self._task: asyncio.Task | None = None
        self._listeners: list[Callable] = []

    # -- plumbing ------------------------------------------------------------

    def add_listener(self, cb: Callable) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    def _opt(self, key: str, default: Any) -> Any:
        # Options override config-flow data; fall back to the default
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _is_excluded(self, ap: dict) -> bool:
        """Match an AP against the excluded_aps option (SSID or BSSID).

        Lets users hide infrastructure APs (e.g. the RV's own router) that
        the dish will always hear but never want to join.
        """
        raw = self._opt(CONF_EXCLUDED_APS, "") or ""
        if not raw:
            return False
        names = {t.strip().lower() for t in raw.split(",") if t.strip()}
        return (
            (ap.get("bssid") or "").lower() in names
            or (ap.get("ssid") or "").lower() in names
        )

    def _state(self, entity_id: str):
        return self.hass.states.get(entity_id)

    def _num(self, entity_id: str) -> float | None:
        st = self._state(entity_id)
        if st is None or st.state in ("unavailable", "unknown", "none", ""):
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    @property
    def azimuth(self) -> float | None:
        return self._num(self.angle_entity)

    @property
    def moving(self) -> bool:
        st = self._state(self.moving_entity)
        return st is not None and st.state == "on"

    def _guard_idle(self) -> None:
        if self._task is not None and not self._task.done():
            raise HomeAssistantError(f"Dish is busy ({self.status}) - abort first")

    def _start(self, status: str, coro) -> asyncio.Task:
        self._guard_idle()
        self.status = status
        self.progress = None
        self.last_error = None
        self._notify()

        async def runner():
            try:
                await coro
            except asyncio.CancelledError:
                _LOGGER.info("Dish %s aborted", status)
            except (HomeAssistantError, UbusError) as err:
                self.last_error = str(err)
                _LOGGER.error("Dish %s failed: %s", status, err)
            except Exception as err:
                self.last_error = f"Unexpected error: {err}"
                _LOGGER.exception("Dish %s crashed", status)
            finally:
                self.status = STATUS_IDLE
                self.progress = None
                self._notify()

        self._task = self.hass.async_create_task(runner())
        return self._task

    # -- primitives -----------------------------------------------------------

    async def _wait_moving(self, want: bool, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.moving != want:
            if time.monotonic() > deadline:
                raise HomeAssistantError(
                    f"Timed out waiting for moving={want} on {self.moving_entity}"
                )
            await asyncio.sleep(SETTLE_POLL)

    async def _wait_settled(self, timeout: float, quiet_for: float = 2.5) -> None:
        """Wait for `moving` to stay False for `quiet_for` seconds.

        During stall-homing the stepper's commanded count reaches the
        unreachable target and `moving` blips off while the ESP-side script
        is still in its settle delay + return move. A bare wait_moving(False)
        exits there and lets the next command race the script - this waits
        through short blips.
        """
        deadline = time.monotonic() + timeout
        quiet = 0.0
        while time.monotonic() < deadline:
            if self.moving:
                quiet = 0.0
            else:
                quiet += SETTLE_POLL
                if quiet >= quiet_for:
                    return
            await asyncio.sleep(SETTLE_POLL)
        raise HomeAssistantError(
            f"Timed out waiting for the dish to settle on {self.moving_entity}"
        )

    async def _home_and_wait(self) -> None:
        """Press the home button and wait for the whole routine to finish.

        The ESP script is: drive to -40 (stall), fixed 12s delay, re-zero,
        then a return move. `moving` is False through most of the delay, so
        we first sleep past it (HOME_SCRIPT_GRACE), then wait for the return
        move to finish and the sensor to stay quiet - covers both the blip
        and the case where moving never turns on at all.
        """
        await self._press(self.home_button)
        await asyncio.sleep(HOME_SCRIPT_GRACE)
        await self._wait_settled(HOME_TIMEOUT - HOME_SCRIPT_GRACE)

    async def _move(self, azimuth: float) -> None:
        """Command an absolute displayed azimuth and wait for the dish to stop."""
        azimuth = round(azimuth % 360)
        await self.hass.services.async_call(
            "number", "set_value",
            {"entity_id": self.target_entity, "value": azimuth},
            blocking=True,
        )
        try:
            await self._wait_moving(True, MOVE_START_TIMEOUT)
        except HomeAssistantError:
            # Very short moves can complete inside the sensor poll window and
            # never show "on" - fine as long as it's settled now
            if self.moving:
                raise
        await self._wait_moving(False, MOVE_TIMEOUT)

    async def _read_signal_once(self) -> float | None:
        """Live dBm straight off the radio - the HA entity only updates on the
        openwrt coordinator's ~60s poll, far too slow for per-stop sampling."""
        try:
            info = await self.ubus.iwinfo_info(
                self._opt(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE)
            )
            sig = info.get("signal")
            return float(sig) if sig is not None else None
        except (UbusError, TypeError, ValueError):
            return self._num(self.signal_entity)  # entity as fallback

    async def _signal_dbm(self, samples: int = SIGNAL_SAMPLES) -> float | None:
        """Median of several signal reads, or None if the link is down."""
        vals = []
        for _ in range(samples):
            v = await self._read_signal_once()
            if v is not None:
                vals.append(v)
            await asyncio.sleep(SAMPLE_SPACING)
        return statistics.median(vals) if vals else None

    async def refresh_assoc(self) -> None:
        """Refresh the currently-associated SSID/BSSID (for the card highlight).

        Called on a slow interval and after joins; failures are swallowed -
        the radio may be mid-scan and the next poll will catch it.
        """
        try:
            info = await self.ubus.iwinfo_info(
                self._opt(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE)
            )
        except Exception:  # noqa: BLE001 - non-critical; retry next poll
            return
        conn = {
            "ssid": info.get("ssid"),
            "bssid": (info.get("bssid") or "").lower() or None,
        }
        if conn != self.connected:
            self.connected = conn
            self._notify()

    async def _press(self, entity_id: str) -> None:
        await self.hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )

    # -- services --------------------------------------------------------------

    async def aim(self, azimuth: float) -> None:
        """Rotate to an absolute displayed azimuth."""
        self._start(STATUS_AIMING, self._move(azimuth))

    async def home(self) -> None:
        """Sensorless re-home: stall against the separator, re-zero."""
        if not self.home_button:
            raise HomeAssistantError("No home button resolved for this dish")

        self._start(STATUS_HOMING, self._home_and_wait())

    async def auto_align(self, sweep: float, step: float, dwell: float) -> None:
        """Sweep an arc, find the dBm peak, park there, verify if configured."""

        async def run():
            center = self.azimuth
            if center is None:
                raise HomeAssistantError("Current angle unavailable")
            stops = [center + d for d in _spread(-sweep / 2, sweep / 2, step)]
            samples: list[tuple[float, float]] = []
            for i, az in enumerate(stops):
                self.progress = {"step": i + 1, "total": len(stops), "azimuth": az % 360}
                self._notify()
                await self._move(az)
                await asyncio.sleep(dwell)
                dbm = await self._signal_dbm()
                if dbm is not None:
                    samples.append((az, dbm))
            if not samples:
                raise HomeAssistantError("No signal seen anywhere in the sweep")
            best_az = _interp_bearing(samples)
            best_dbm = max(s[1] for s in samples)
            await self._move(best_az)
            await asyncio.sleep(dwell)
            self.alignment = {
                "azimuth": best_az,
                "dbm": best_dbm,
                "verified": None,
            }
            self._notify()
            # Built-in verification: a timed download on the radio itself,
            # measuring the actual dish uplink (not HA's path)
            if self._opt(CONF_BUILTIN_SPEEDTEST, DEFAULT_BUILTIN_SPEEDTEST):
                try:
                    self.alignment["verified"] = await self.ubus.speedtest(
                        SPEEDTEST_URL, SPEEDTEST_DURATION
                    )
                except UbusError as err:
                    self.alignment["verified"] = {"error": str(err)}
                    _LOGGER.warning("Built-in speed test failed: %s", err)
                self._notify()
            # External verification: call a service (speedtestdotnet.speedtest
            # & co.) or press/run an entity, then pull the result back from
            # the configured download-speed sensor
            svc = (self._opt(CONF_VERIFY_SERVICE, "") or "").strip()
            trigger = self._opt(CONF_VERIFY_SPEEDTEST, None)
            result_sensor = self._opt(CONF_SPEEDTEST_SENSOR, None)
            if svc or trigger:
                prev_updated = None
                if result_sensor:
                    st = self._state(result_sensor)
                    prev_updated = st.last_updated if st else None
                try:
                    if svc:
                        domain, _, method = svc.partition(".")
                        if not domain or not method:
                            raise HomeAssistantError(
                                f"verify_service '{svc}' isn't 'domain.service'"
                            )
                        # Speedtest services can take a minute - don't block,
                        # the result sensor tells us when it's done
                        await self.hass.services.async_call(
                            domain, method, {}, blocking=False
                        )
                    else:
                        domain = trigger.split(".", 1)[0]
                        action = "press" if domain == "button" else "turn_on"
                        await self.hass.services.async_call(
                            domain, action,
                            {"entity_id": trigger},
                            blocking=result_sensor is None,
                        )
                except Exception as err:  # noqa: BLE001
                    self.alignment["verified_ext"] = {"error": str(err)}
                    _LOGGER.warning("External speed test trigger failed: %s", err)
                    self._notify()
                else:
                    if result_sensor:
                        self.alignment["verified_ext"] = (
                            await self._await_speedtest_result(
                                result_sensor, prev_updated
                            )
                        )
                    self._notify()

        self._start(STATUS_ALIGNING, run())

    async def survey(self, step: float, dwell: float) -> None:
        """Full-rotation AP survey, deduplicated per BSSID at strongest angle."""

        async def run():
            # Clear stale results up front - the card repopulates live below
            self.survey_results = []
            self._notify()
            if self._opt(CONF_AUTO_HOME, DEFAULT_AUTO_HOME) and self.home_button:
                self.status = STATUS_HOMING
                self._notify()
                await self._home_and_wait()
                self.status = STATUS_SURVEYING
                self._notify()

            iface = self._opt(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE)
            found: dict[str, dict] = {}
            stops = list(range(0, 360, int(step)))
            for i, az in enumerate(stops):
                self.progress = {"step": i + 1, "total": len(stops), "azimuth": az}
                self._notify()
                await self._move(az)
                await asyncio.sleep(dwell)
                try:
                    aps = await self.ubus.iwinfo_scan(iface)
                except UbusError as err:
                    _LOGGER.warning("scan at %d deg failed: %s", az, err)
                    continue
                for ap in aps:
                    bssid = (ap.get("bssid") or ap.get("ssid") or "").lower()
                    if not bssid or self._is_excluded(ap):
                        continue
                    entry = found.setdefault(bssid, {
                        "ssid": ap.get("ssid") or "",
                        "bssid": bssid,
                        "channel": ap.get("channel"),
                        "crypto": ap.get("encryption") or ap.get("crypto") or {},
                        "dbm": None,
                        "azimuth": None,
                        "samples": [],
                    })
                    if not entry["ssid"] and ap.get("ssid"):
                        entry["ssid"] = ap["ssid"]
                    sig = ap.get("signal")
                    if sig is not None:
                        entry["samples"].append((az, sig))
                        if entry["dbm"] is None or sig > entry["dbm"]:
                            entry["dbm"] = sig
                # Re-estimate bearings from everything heard so far and
                # republish - the card shows the survey filling in live,
                # with markers settling onto their interpolated bearings
                for entry in found.values():
                    peak = _interp_bearing(entry["samples"])
                    if peak is not None:
                        entry["azimuth"] = round(peak, 1)
                self.survey_results = sorted(
                    found.values(),
                    key=lambda a: (a["dbm"] is not None, a["dbm"]),
                    reverse=True,
                )
                self._notify()

        self._start(STATUS_SURVEYING, run())

    async def join(
        self,
        ssid: str,
        password: str,
        encryption: str | None,
        bssid: str | None = None,
    ) -> None:
        """Aim at the AP's best survey angle, then repoint wwan at it.

        Rewrites the STA iface UCI on the NanoBeam (ssid/encryption/key/bssid)
        and bounces the interface. Snapshots the previous values first and
        rolls back if the link doesn't come up - a failed join must never
        leave the radio without an uplink.
        """

        async def run():
            ap = self._find_ap(ssid, bssid)
            if ap is None and not bssid:
                # Not in the survey (or a hidden net) - go find it on the air:
                # directed probe at this heading, sweep if nothing answers
                self.status = STATUS_FINDING
                self._notify()
                ap = await self._find_on_air(ssid)
                self.status = STATUS_JOINING
                self._notify()
            if ap is None:
                raise HomeAssistantError(
                    f"Couldn't hear {ssid} anywhere - check the name or run a survey"
                )
            if ap.get("azimuth") is not None:
                await self._move(ap["azimuth"])

            enc = encryption or _encryption_for(ap)
            sections = await self.ubus.uci_state("wireless")
            sta, sta_sec = next(
                (
                    (name, sec)
                    for name, sec in sections.items()
                    if sec.get("mode") == "sta" and sec.get("network") == "wwan"
                ),
                (None, None),
            )
            if sta is None:
                raise UbusError("No STA-mode wireless iface bound to wwan found")

            # Snapshot before touching anything so we can roll back
            previous = {
                k: sta_sec.get(k, "") for k in ("ssid", "encryption", "key", "bssid")
            }

            values = {
                "ssid": ssid,
                "encryption": enc,
                "key": password if enc != "none" else "",
            }
            # Pin the association to the surveyed BSSID so the radio can't
            # wander onto a weaker same-SSID AP
            if ap and ap.get("bssid"):
                values["bssid"] = ap["bssid"]

            await self.ubus.uci_set("wireless", sta, values)
            await self.ubus.uci_commit("wireless")
            await self._bounce_wwan()

            deadline = time.monotonic() + JOIN_TIMEOUT
            while time.monotonic() < deadline:
                dbm = await self._read_signal_once()
                if dbm is not None and dbm != 0:
                    await self.refresh_assoc()
                    return
                await asyncio.sleep(2)

            # Failed - work out why, then restore the previous uplink config
            reason = await self._diagnose_join_failure(ap)
            _LOGGER.warning(
                "Join to %s timed out (%s); rolling back to %s",
                ssid, reason, previous["ssid"],
            )
            await self.ubus.uci_set("wireless", sta, previous)
            await self.ubus.uci_commit("wireless")
            await self._bounce_wwan()
            raise HomeAssistantError(
                f"Couldn't connect to {ssid} - {reason}. "
                f"Rolled back to {previous['ssid']}"
            )

        self._start(STATUS_JOINING, run())

    async def _await_speedtest_result(
        self, entity_id: str, prev_updated, timeout: float = SPEEDTEST_RESULT_TIMEOUT
    ) -> dict:
        """Poll a speedtest result sensor until it reports a new value."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self._state(entity_id)
            if (
                st is not None
                and st.last_updated != prev_updated
                and st.state not in ("unavailable", "unknown", "none", "")
            ):
                try:
                    val = float(st.state)
                except (TypeError, ValueError):
                    pass
                else:
                    return {
                        "mbps": round(val, 1),
                        "unit": st.attributes.get("unit_of_measurement") or "Mbps",
                        "sensor": entity_id,
                        "source": "external",
                    }
            await asyncio.sleep(2)
        return {"error": f"timed out waiting for {entity_id} to update"}

    async def _diagnose_join_failure(self, ap: dict | None) -> str:
        """Rescan after a failed association to classify the reason."""
        iface = self._opt(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE)
        try:
            aps = await self.ubus.iwinfo_scan(iface)
        except UbusError:
            return "association timed out"
        bssid = ap.get("bssid") if ap else None
        if bssid and any(
            (a.get("bssid") or "").lower() == bssid.lower() for a in aps
        ):
            return (
                "the AP is in range but rejected the association - "
                "check the password or encryption type"
            )
        return "the AP wasn't heard at this heading - signal too weak or wrong bearing"

    async def _bounce_wwan(self) -> None:
        await self.ubus.interface("down", "wwan")
        await asyncio.sleep(2)
        await self.ubus.interface("up", "wwan")

    def _find_ap(self, ssid: str, bssid: str | None) -> dict | None:
        """Survey entry by BSSID if given, else strongest match for the SSID."""
        if bssid:
            b = bssid.lower()
            hit = next((a for a in self.survey_results if a["bssid"] == b), None)
            if hit:
                return hit
        return next((a for a in self.survey_results if a["ssid"] == ssid), None)

    async def _find_on_air(self, ssid: str) -> dict | None:
        """Locate an SSID (incl. hidden) by directed probe + rotation.

        Probes the current heading first - the ~50deg beam often catches it.
        Otherwise sweeps the full circle, tracking the strongest response.
        Returns a survey-shaped AP dict (merged into survey_results) or None.
        """
        iface = self._opt(CONF_WIFI_INTERFACE, DEFAULT_WIFI_INTERFACE)

        hits = await self.ubus.iw_probe(iface, ssid)
        if hits:
            best = max(hits, key=lambda h: h.get("signal") or -999)
            return self._merge_survey_hit(ssid, best, self.azimuth)

        step = int(self._opt(CONF_SURVEY_STEP_DEGREES, DEFAULT_SURVEY_STEP_DEGREES))
        cur = self.azimuth or 0
        stops = [int((cur + k * step)) % 360 for k in range(1, int(360 / step))]
        hits_by_bssid: dict[str, dict] = {}
        samples: dict[str, list] = {}
        for i, az in enumerate(stops):
            self.progress = {"step": i + 1, "total": len(stops), "azimuth": az}
            self._notify()
            await self._move(az)
            try:
                hits = await self.ubus.iw_probe(iface, ssid)
            except UbusError as err:
                _LOGGER.warning("probe at %d deg failed: %s", az, err)
                continue
            for h in hits:
                b = (h.get("bssid") or "").lower()
                sig = h.get("signal")
                if not b or sig is None or self._is_excluded(h):
                    continue
                hits_by_bssid[b] = h
                samples.setdefault(b, []).append((az, sig))
        merged = {}
        for b, samps in samples.items():
            merged[b] = self._merge_survey_hit(
                ssid, hits_by_bssid[b], _interp_bearing(samps), samples=samps
            )
        if not merged:
            return None
        best_b = max(merged, key=lambda b: merged[b]["dbm"] or -999)
        return merged[best_b]

    def _merge_survey_hit(
        self, ssid: str, hit: dict, azimuth, samples: list | None = None
    ) -> dict:
        """Fold a probe result into survey_results (keyed by BSSID)."""
        ap = {
            "ssid": ssid,
            "bssid": hit["bssid"],
            "channel": hit.get("channel"),
            "crypto": hit.get("crypto") or {"enabled": True},  # parsed from RSN/WPA IEs
            "dbm": hit.get("signal"),
            "azimuth": azimuth,
        }
        if samples:
            ap["samples"] = [list(s) for s in samples]
        existing = next(
            (a for a in self.survey_results if a["bssid"] == ap["bssid"]), None
        )
        if existing:
            if (ap["dbm"] or -999) > (existing["dbm"] or -999):
                existing.update(ap)
        else:
            self.survey_results.append(ap)
            self.survey_results.sort(
                key=lambda a: (a["dbm"] is not None, a["dbm"]), reverse=True
            )
        self._notify()
        return existing or ap

    def abort(self) -> None:
        """Cancel whatever is running."""
        if self._task is not None and not self._task.done():
            self._task.cancel()


def _interp_bearing(samples: list[tuple[float, float]]) -> float | None:
    """Estimate an AP's true bearing from (azimuth, dBm) sweep samples.

    Every AP is audible across several stops inside the beamwidth, but keeping
    only the loudest stop quantizes all of them to the sweep grid - markers
    clump on the stop bearings. Antenna gain is roughly Gaussian in dB, so a
    parabola through the strongest sample and its azimuth neighbors recovers
    the sub-step bearing of the true peak.
    """
    pts = sorted(p for p in samples if p[1] is not None)
    if not pts:
        return None
    if len(pts) < 3:
        return max(pts, key=lambda p: p[1])[0] % 360
    imax = max(range(len(pts)), key=lambda i: pts[i][1])
    x0, y0 = pts[imax]

    def near(p):
        # unwrap a neighbor's azimuth so it sits adjacent to the peak
        x = p[0]
        while x - x0 > 180:
            x -= 360
        while x - x0 < -180:
            x += 360
        return x, p[1]

    xm, ym = near(pts[(imax - 1) % len(pts)])
    xp, yp = near(pts[(imax + 1) % len(pts)])
    if not (xm < x0 < xp):
        return x0 % 360
    # parabola through (xm,ym),(x0,y0),(xp,yp); vertex is the bearing estimate
    a = ((yp - y0) / (xp - x0) - (y0 - ym) / (x0 - xm)) / (xp - xm)
    if a >= 0:  # not concave - noise; no meaningful peak
        return x0 % 360
    b = (y0 - ym) / (x0 - xm) - a * (xm + x0)
    peak = -b / (2 * a)
    if not xm <= peak <= xp:
        return x0 % 360
    return peak % 360


def _spread(lo: float, hi: float, step: float) -> list[float]:
    """Offsets from lo to hi inclusive, ordered center-out for less slew."""
    pts, d = [], lo
    while d <= hi:
        pts.append(d)
        d += step
    return sorted(pts, key=abs)


def _encryption_for(ap: dict | None) -> str:
    """Map iwinfo crypto onto an OpenWrt wireless.encryption value."""
    if not ap:
        return "psk2"
    crypto = ap.get("crypto") or {}
    if not crypto.get("enabled", True):
        return "none"
    auths = crypto.get("authentication") or []
    for a in auths:
        if a in ENCRYPTION_MAP:
            return ENCRYPTION_MAP[a]
    return "psk2"
