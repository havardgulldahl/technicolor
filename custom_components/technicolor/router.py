import asyncio
import logging
from collections import namedtuple
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Callable

import technicolorgateway
from homeassistant.components.device_tracker import (
    CONF_CONSIDER_HOME,
    DEFAULT_CONSIDER_HOME,
)
from homeassistant.components.device_tracker import DOMAIN as TRACKER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from technicolorgateway import TechnicolorGateway
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from technicolorgateway.datamodels import (
    DiagnosticsConnection,
    NetworkDevice,
    SystemInfo,
)

from .const import (
    CONF_CONSIDER_HOME,
    CONF_REQUIRE_IP,
    CONF_TRACK_UNKNOWN,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_NAME,
    DEFAULT_TRACK_UNKNOWN,
    DOMAIN,
    MODE_GUEST,
    MODE_AP,
    MODE_ROUTER,
    MODE_WLAN,
    MODE_ETHERNET,
)

CONF_REQ_RELOAD = [CONF_REQUIRE_IP]

# from .device_tracker import TechnicolorDeviceScanner
TechnicolorDeviceScanner = Any

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)
SENSORS_TYPE_COUNT = "sensors_count"


@dataclass
class ConnectedDevice:
    mac: str
    name: str
    active: bool = False
    last_seen: datetime = dt_util.utcnow() - timedelta(days=365)
    device_model: str = None
    device_type: str = None
    type: str = None
    link_rate: str = None
    signal: str = None
    ip: str = None
    ssid: str = None
    conn_ap_mac: str = None
    allow_or_block: str = None

    @staticmethod
    def from_network_device(device: NetworkDevice) -> "ConnectedDevice":

        dt = None
        if device.is_satellite:
            dt = MODE_AP
        elif device.is_guest:
            dt = MODE_GUEST
        elif device.is_ethernet:
            dt = MODE_ETHERNET
        else:
            dt = MODE_WLAN

        try:
            rate = float(device.speed)
        except (TypeError, ValueError):
            rate = None

        return ConnectedDevice(
            mac=format_mac(str(device.mac_address)),
            name=device.friendly_name,
            active=True,
            last_seen=dt_util.utcnow(),
            # device_model=device.device_model,
            device_type=dt,
            type=device.device_type,
            link_rate=rate,
            # signal=device.signal,
            ip=device.ipv4,
            ssid=device.ssid,
            # conn_ap_mac=device.conn_ap_mac,
            # allow_or_block=device.allow_or_block,
        )


class TechnicolorRouter:
    """Representation of a Technicolor router."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize a Technicolor router."""
        self.hass = hass
        self._entry = entry
        self._host = entry.data[CONF_HOST]
        self._user = entry.data[CONF_USERNAME]
        self._pass = entry.data[CONF_PASSWORD]

        self._api: TechnicolorGateway = None
        self.api_lock = asyncio.Lock()  # for async calls to sync api

        self._info = None
        self.model = ""
        self.mode = MODE_ROUTER
        self.device_name = ""
        self.firmware_version = ""
        self.hardware_version = ""
        self.serial_number = ""

        self.listeners = []
        self.track_devices = True

        self._devices: dict[str, Any] = {}
        self._connected_devices: int = 0
        self._connect_error: bool = False
        consider_home_int = entry.options.get(
            CONF_CONSIDER_HOME, DEFAULT_CONSIDER_HOME.total_seconds()
        )
        self._consider_home = timedelta(seconds=consider_home_int)

    async def setup(self) -> None:
        self._api: TechnicolorGateway = TechnicolorGateway(
            self._host, "80", self._user, self._pass
        )

        try:
            await self.hass.async_add_executor_job(self._api.authenticate)
        except Exception as e:
            _LOGGER.exception("Failed to connect to Technicolor", e)
            raise ConfigEntryNotReady from e

        # get static system info (device name, model, firmware, ...)
        self._info = await self.hass.async_add_executor_job(
            self._api.get_system_info_modal
        )
        if self._info is None:
            return False

        self.device_name = self._info.product_vendor or DEFAULT_NAME
        self.model = self._info.product_name or None
        self.firmware_version = self._info.firmware_version or None
        self.hardware_version = self._info.hardware_version or None
        self.serial_number = self._info.serial_number or None
        self.mode = MODE_ROUTER
        self.uptime = self._info.uptime

        # Load tracked entities from registry
        entity_reg = er.async_get(self.hass)
        track_entries = er.async_entries_for_config_entry(
            entity_reg, self._entry.entry_id
        )
        for entry in track_entries:
            if entry.domain != TRACKER_DOMAIN:
                continue
            device_mac = format_mac(entry.unique_id)

            _LOGGER.warn(f"{entry.entity_id} {entry} {device_mac}")
            # migrate entity unique ID if wrong formatted
            if device_mac != entry.unique_id:
                existing_entity_id = entity_reg.async_get_entity_id(
                    TRACKER_DOMAIN, DOMAIN, device_mac
                )
                if existing_entity_id:
                    # entity with uniqueid properly formatted already
                    # exists in the registry, we delete this duplicate
                    entity_reg.async_remove(entry.entity_id)
                    continue

                entity_reg.async_update_entity(
                    entry.entity_id, new_unique_id=device_mac
                )

            self._devices[device_mac] = asdict(
                ConnectedDevice(mac=device_mac, name=entry.name)
            )

        # Update devices
        await self.update_all(None)

        self.async_on_close(
            async_track_time_interval(self.hass, self.update_all, SCAN_INTERVAL)
        )
        return True

    async def update_all(self, now=None) -> None:
        """Update all Technicolor platforms."""
        _LOGGER.info("update_device_trackers")
        new_device = None
        now = dt_util.utcnow()
        devices = await self.hass.async_add_executor_job(
            self._api.get_network_device_details
        )

        for device in devices:
            cd = asdict(ConnectedDevice.from_network_device(device))
            if self.devices.get(cd["mac"]) is None:
                new_device = True
                _LOGGER.info("new: %s", cd["mac"])
                last_time = now
            else:
                new_device = False
                last_time: datetime = self.devices[cd["mac"]]["last_seen"]

            cd["active"] = now - last_time < self._consider_home
            cd["last_seen"] = now
            self._devices[cd["mac"]] = cd

        async_dispatcher_send(self.hass, self.signal_device_update)

        if new_device:
            async_dispatcher_send(self.hass, self.signal_device_new)

    async def close(self) -> None:
        """Close the connection."""
        if self._api is not None:
            async with self.api_lock:
                await self.hass.async_add_executor_job(self._api.logout)

    @callback
    def async_on_close(self, func: CALLBACK_TYPE) -> None:
        """Add a function to call when router is closed."""
        # self._on_close.append(func)

    def update_options(self, new_options: MappingProxyType[str, Any]) -> bool:
        """Update router options."""
        req_reload = False
        for name, new_opt in new_options.items():
            if name in CONF_REQ_RELOAD:
                old_opt = self._options.get(name)
                if old_opt is None or old_opt != new_opt:
                    req_reload = True
                    break

        self._options.update(new_options)
        return req_reload

    async def async_get_traffic_meter(self) -> dict[str, Any] | None:
        """Get the traffic meter data of the router."""
        raise NotImplementedError
        async with self.api_lock:
            return await self.hass.async_add_executor_job(self.api.get_traffic_meter)

    async def async_get_speed_test(self) -> dict[str, Any] | None:
        """Perform a speed test and get the results from the router."""
        raise NotImplementedError
        async with self.api_lock:
            return await self.hass.async_add_executor_job(
                self.api.get_new_speed_test_result
            )

    async def async_get_link_status(self) -> dict[str, Any] | None:
        """Check the ethernet link status of the router."""
        async with self.api_lock:
            return await self.hass.async_add_executor_job(
                self._api.get_diagnostics_connection_modal
            )

    async def async_allow_block_device(self, mac: str, allow_block: str) -> None:
        """Allow or block a device connected to the router."""
        raise NotImplementedError
        async with self.api_lock:
            await self.hass.async_add_executor_job(
                self.api.allow_block_device, mac, allow_block
            )

    async def async_get_utilization(self) -> dict[str, Any] | None:
        """Get the system information about utilization of the router."""
        async with self.api_lock:
            return await self.hass.async_add_executor_job(
                self._api.get_system_info_modal
            )

    async def async_reboot(self) -> None:
        """Reboot the router."""
        raise NotImplementedError
        async with self.api_lock:
            await self.hass.async_add_executor_job(self.api.reboot)

    @property
    def signal_device_update(self) -> str:
        """Event specific per Technicolor entry to signal updates in devices."""
        return f"{DOMAIN}-device-update"

    @property
    def signal_device_new(self) -> str:
        """Event specific per Technicolor entry to signal new device."""
        return f"{DOMAIN}-device-new"

    @property
    def unique_id(self) -> str:
        """Return router unique id."""
        return self._entry.unique_id or self._entry.entry_id

    @property
    def devices(self) -> dict[str, TechnicolorDeviceScanner]:
        """Return devices."""
        return self._devices
