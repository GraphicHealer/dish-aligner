"""Minimal Ubus-over-HTTP client for talking to the OpenWrt radio.

Reuses the credentials already stored on the FaserF/ha-openwrt config entry -
no second password to configure. Only the handful of calls the dish needs:
iwinfo scan/info, uci get/set/commit, network.interface up/down, and `iw`
(via file.exec) for directed SSID probes that resolve hidden networks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import OPENWRT_DOMAIN

_LOGGER = logging.getLogger(__name__)

NULL_TOKEN = "00000000000000000000000000000000"
TIMEOUT = aiohttp.ClientTimeout(total=15)
IW_PATHS = ("/usr/sbin/iw", "/sbin/iw", "/usr/bin/iw", "/bin/iw")
APK_PATHS = ("/sbin/apk", "/usr/sbin/apk", "/bin/apk")
PROBE_SETTLE = 3.0  # s to let probe responses come back before dumping


class UbusError(HomeAssistantError):
    """Ubus call failed."""


class UbusClient:
    """Async JSON-RPC client for /ubus on the radio."""

    def __init__(self, hass: HomeAssistant, base_url: str, username: str, password: str) -> None:
        self._session = async_get_clientsession(hass)
        self._url = f"{base_url.rstrip('/')}/ubus"
        self._username = username
        self._password = password
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._rpc_id = 0

    async def _post(self, params: list, timeout: aiohttp.ClientTimeout = TIMEOUT) -> Any:
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "call",
            "params": params,
        }
        async with self._session.post(self._url, json=payload, timeout=timeout) as resp:
            body = await resp.json(content_type=None)
        result = body.get("result")
        if not isinstance(result, list) or not result:
            raise UbusError(f"Malformed ubus response: {body!r}")
        code = result[0]
        if code != 0:
            # -32002/6 = session expired or access denied
            raise UbusError(f"ubus {params[1]}.{params[2]} failed, code {code}")
        return result[1] if len(result) > 1 else {}

    async def _login(self) -> None:
        data = await self._post(
            [
                NULL_TOKEN,
                "session",
                "login",
                {"username": self._username, "password": self._password},
            ]
        )
        token = data.get("ubus_rpc_session")
        if not token:
            raise UbusError("Ubus login returned no session token")
        self._token = token

    async def call(self, obj: str, method: str, params: dict | None = None,
                   needs_session: bool = False,
                   timeout: aiohttp.ClientTimeout = TIMEOUT) -> Any:
        """Call a ubus object method, re-logging-in once on expiry.

        needs_session injects the live token into the params - rpcd's
        file.exec requires it echoed inside the call as a CSRF check, and
        it must be refreshed on the re-login retry path too.
        """
        async with self._lock:
            if self._token is None:
                await self._login()
            p = params or {}
            if needs_session:
                p = {**p, "session": self._token}
            try:
                return await self._post([self._token, obj, method, p], timeout)
            except UbusError as err:
                if "code 6" in str(err) or "-32002" in str(err):
                    _LOGGER.debug("Ubus session expired/denied, re-login")
                    self._token = None
                    await self._login()
                    if needs_session:
                        p = {**p, "session": self._token}
                    return await self._post([self._token, obj, method, p], timeout)
                raise

    # -- convenience wrappers ------------------------------------------------

    async def iwinfo_scan(self, device: str) -> list[dict]:
        """Return [{ssid, bssid, channel, signal, crypto}, ...] for `device`."""
        data = await self.call("iwinfo", "scan", {"device": device})
        return data.get("results", []) or []

    async def uci_state(self, package: str) -> dict:
        """Return uci state for a package: {section_name: {values...}}."""
        return await self.call("uci", "state", {"config": package})

    async def uci_set(self, config: str, section: str, values: dict) -> None:
        await self.call(
            "uci",
            "set",
            {"config": config, "section": section, "values": values},
        )

    async def uci_commit(self, config: str) -> None:
        await self.call("uci", "commit", {"config": config})

    async def interface(self, action: str, interface: str) -> None:
        """action: 'up' | 'down'"""
        await self.call("network.interface", action, {"interface": interface})

    async def iwinfo_devices(self) -> list[str]:
        """Wireless interface names on the radio, e.g. ['phy0-sta0']."""
        data = await self.call("iwinfo", "devices", {})
        return data.get("devices", []) or []

    async def _file_exec(self, command: str, params: list[str],
                         timeout: aiohttp.ClientTimeout = TIMEOUT) -> dict:
        """file.exec on OpenWrt >=24ish: `command` is the program path and the
        session id must be echoed inside the params (rpcd CSRF check)."""
        return await self.call(
            "file", "exec",
            {"command": command, "params": params},
            needs_session=True,
            timeout=timeout,
        )

    async def speedtest(self, url: str, duration: float = 12.0) -> dict:
        """Best available on-radio test: librespeed-cli if installed (full
        ping/jitter/down/up via its JSON output), else the stock
        uclient-fetch fallback that only reports download Mbps."""
        path = ""
        try:
            data = await self._file_exec(
                "/bin/sh",
                ["-c", "command -v librespeed-cli || command -v librespeed-cli-rust"],
            )
            path = (data.get("stdout") or "").strip().splitlines()[0].strip()
        except (UbusError, IndexError):
            pass
        if path:
            try:
                return await self._librespeed(path)
            except UbusError as err:
                _LOGGER.warning(
                    "librespeed-cli failed (%s); using fetch fallback", err
                )
        return await self.speedtest_down(url, duration)

    async def _librespeed(self, path: str) -> dict:
        """Run `librespeed-cli --json` and normalise the result."""
        data = await self._file_exec(
            path, ["--json"], timeout=aiohttp.ClientTimeout(total=150)
        )
        try:
            results = json.loads(data.get("stdout") or "")
        except ValueError as err:
            raise UbusError(f"librespeed-cli returned no JSON: {err}") from err
        if isinstance(results, dict):
            results = [results]
        if not results:
            raise UbusError("librespeed-cli ran but returned no result")
        best = max(results, key=lambda r: r.get("download") or 0)
        return {
            "mbps": round(best.get("download") or 0, 1),
            "upload_mbps": round(best.get("upload") or 0, 1),
            "ping_ms": best.get("ping"),
            "jitter_ms": best.get("jitter"),
            "server": best.get("name") or best.get("server"),
            "source": "librespeed",
        }

    async def speedtest_down(self, url: str, duration: float = 12.0) -> dict:
        """Downlink test run *on the radio*: pull `url` for up to `duration`
        seconds with uclient-fetch, count received bytes, return Mbps.

        Measures the dish's own uplink path end-to-end - exactly what
        auto-align optimizes. Uses only stock OpenWrt tools (timeout +
        uclient-fetch), so there's nothing extra to install.
        """
        cmd = (
            f"timeout {int(duration)} uclient-fetch -q "
            f"--no-check-certificate -O- '{url}' 2>/dev/null | wc -c"
        )
        t0 = time.monotonic()
        data = await self._file_exec(
            "/bin/sh", ["-c", cmd],
            timeout=aiohttp.ClientTimeout(total=duration + 8),
        )
        elapsed = time.monotonic() - t0
        try:
            nbytes = int((data.get("stdout") or "0").strip())
        except ValueError:
            nbytes = 0
        if nbytes <= 0:
            raise UbusError(
                "download test returned no data - is the uplink actually up?"
            )
        mbps = nbytes * 8 / 1_000_000 / max(elapsed, 0.1)
        return {
            "mbps": round(mbps, 1),
            "bytes": nbytes,
            "seconds": round(elapsed, 1),
            "source": "radio",
        }

    async def _apk_installed(self, pkg: str) -> bool | None:
        """`apk info -e <pkg>` exit status - None if apk itself can't run."""
        for path in APK_PATHS:
            try:
                data = await self._file_exec(path, ["info", "-e", pkg])
            except UbusError:
                continue
            try:
                return int(data.get("code", 0)) == 0
            except (TypeError, ValueError):
                return bool((data.get("stdout") or "").strip())
        return None

    async def package_status(self) -> dict[str, bool]:
        """Check the radio-side dependencies dish_aligner needs."""
        status: dict[str, bool] = {}

        installed = await self._apk_installed("iwinfo")
        if installed is None:
            # No apk (older opkg systems) - the call itself is the test
            try:
                await self.iwinfo_devices()
                installed = True
            except UbusError:
                installed = False
        status["iwinfo"] = installed

        installed = await self._apk_installed("iw")
        if installed is None:
            try:
                await self._exec(["--version"])
                installed = True
            except UbusError:
                installed = False
        status["iw"] = installed

        # our rpcd ACL file (grants iwinfo/file/exec to the ubus session)
        try:
            await self.call(
                "file", "stat",
                {"path": "/usr/share/rpcd/acl.d/homeassistant.json"},
            )
            status["rpcd ACL"] = True
        except UbusError:
            status["rpcd ACL"] = False

        # librespeed-cli is optional - enables full ping/upload results for
        # the post-align speed test when installed (pkg or static binary)
        try:
            data = await self._file_exec(
                "/bin/sh",
                ["-c", "command -v librespeed-cli || command -v librespeed-cli-rust"],
            )
            status["librespeed-cli (optional)"] = bool(
                (data.get("stdout") or "").strip()
            )
        except UbusError:
            status["librespeed-cli (optional)"] = False

        return status

    async def iwinfo_info(self, device: str) -> dict:
        return await self.call("iwinfo", "info", {"device": device})

    async def _exec(self, params: list[str]) -> dict:
        """file.exec over ubus, trying each known `iw` path."""
        last: Exception | None = None
        for path in IW_PATHS:
            try:
                return await self._file_exec(path, params)
            except UbusError as err:
                last = err
        raise last or UbusError("iw not found on the radio")

    async def iw_probe(self, device: str, ssid: str) -> list[dict]:
        """Directed probe for one SSID (finds hidden networks too).

        Sends a probe request carrying `ssid` - a hidden AP that owns it must
        respond - then parses `iw scan dump` for matching BSS entries.
        Returns [{bssid, ssid, signal, channel}].
        """
        await self._exec(["dev", device, "scan", "trigger", "ssid", ssid])
        await asyncio.sleep(PROBE_SETTLE)
        data = await self._exec(["dev", device, "scan", "dump"])
        return _parse_iw_dump(data.get("stdout", "") or "", ssid)


def _parse_iw_dump(text: str, want_ssid: str) -> list[dict]:
    """Parse `iw scan dump` stdout into matching AP dicts."""
    aps: list[dict] = []
    cur: dict | None = None

    def flush():
        if cur is not None and cur.get("ssid") == want_ssid and cur.get("bssid"):
            cur["crypto"] = cur.get("crypto") or {"enabled": False}
            aps.append(cur)

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("BSS "):
            flush()
            cur = {"bssid": line.split()[1].split("(")[0].lower()}
        elif cur is not None:
            if line.startswith("signal:"):
                try:
                    cur["signal"] = float(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("SSID:"):
                cur["ssid"] = line[5:].strip()
            elif re.match(r"(RSN|WPA)\s*:", line):
                # Secured network - auth suites follow on later lines
                cur.setdefault("crypto", {"enabled": True, "authentication": []})
            elif line.startswith("* Authentication suites:"):
                crypto = cur.setdefault(
                    "crypto", {"enabled": True, "authentication": []}
                )
                for s in re.findall(r"psk|sae|owe|802\.1x", line.lower()):
                    suite = "eap" if s == "802.1x" else s
                    if suite not in crypto["authentication"]:
                        crypto["authentication"].append(suite)
            elif "channel" not in cur:
                m = re.search(r"channel (\d+)", line)
                if m:
                    cur["channel"] = int(m.group(1))
    flush()
    return aps


def ubus_client_from_device(hass: HomeAssistant, device_id: str) -> UbusClient:
    """Build a client for the radio behind a HA device, reusing the
    credentials stored on the owning openwrt config entry."""
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        raise HomeAssistantError("Radio device not found in registry")
    # config_entry_id on newer cores; config_entries is deprecated there
    # but still the only source on older ones.
    entry_id = getattr(dev, "config_entry_id", None)
    entry_ids = [entry_id] if entry_id else list(getattr(dev, "config_entries", []))
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == OPENWRT_DOMAIN:
            d = entry.data
            host = d.get("host") or d.get("ip") or d.get("url")
            user = d.get("username") or d.get("user") or "root"
            password = d.get("password")
            if not host or password is None:
                continue
            https = d.get("https") or d.get("ssl") or d.get("use_https")
            if "://" not in host:
                host = f"{'https' if https else 'http'}://{host}"
            return UbusClient(hass, host, user, password)
    raise HomeAssistantError(
        "Couldn't read ubus credentials from the OpenWrt config entry - "
        "set the radio up in the OpenWrt integration first"
    )
