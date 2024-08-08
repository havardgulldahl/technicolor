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
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from technicolorgateway import TechnicolorGateway
from technicolorgateway.datamodels import (
    DiagnosticsConnection,
    NetworkDevice,
    SystemInfo,
)

from .const import (
    CONF_CONSIDER_HOME,
    CONF_DNSMASQ,
    CONF_INTERFACE,
    CONF_REQUIRE_IP,
    CONF_TRACK_UNKNOWN,
    DEFAULT_CONSIDER_HOME,
    DEFAULT_INTERFACE,
    DEFAULT_NAME,
    DEFAULT_TRACK_UNKNOWN,
    DOMAIN,
    KEY_COORDINATOR,
    KEY_METHOD,
    KEY_SENSORS,
    MODE_AP,
    MODE_ROUTER,
)

CONF_REQ_RELOAD = [CONF_DNSMASQ, CONF_INTERFACE, CONF_REQUIRE_IP]

# from .device_tracker import TechnicolorDeviceScanner
TechnicolorDeviceScanner = Any

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)
SENSORS_TYPE_COUNT = "sensors_count"

ConnectedDevice = namedtuple("WrtDevice", ["ip", "name", "connected_to"])


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

        self._connected_devices = 0
        self._devices: dict[str, TechnicolorDeviceScanner] = {}
        self._connected_devices: int = 0
        self._connect_error: bool = False

    async def setup(self) -> None:
        self._api = TechnicolorGateway(self._host, "80", self._user, self._pass)

        try:
            await self.hass.async_add_executor_job(self._api.authenticate)
        except Exception as e:
            _LOGGER.exception("Failed to connect to Technicolor", e)
            raise ConfigEntryNotReady from e

        _LOGGER.warn(f"{self._api}")
        _LOGGER.warn(f"{self._api._br}")

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

            self._devices[device_mac] = TechnicolorDeviceScanner(
                device_mac, entry.original_name
            )

        # Update devices
        await self.update_all(None)

        self.async_on_close(
            async_track_time_interval(self.hass, self.update_all, SCAN_INTERVAL)
        )

    async def update_all(self, now) -> None:
        """Update all Technicolor platforms."""
        _LOGGER.info("update_all")
        await self.update_device_trackers()

    async def update_device_trackers(self) -> None:
        _LOGGER.info("update_device_trackers")
        new_device = None
        # devices = await self.hass.async_add_executor_job(self._api.get_device_modal)
        devices = await self.hass.async_add_executor_job(
            self._api.get_network_device_details
        )

        for device in devices:
            _LOGGER.warn(f"update_device_trackers device {device}")
            device_mac = str(device.mac_address)
            _LOGGER.info(f"device: {device_mac}")
            if self.devices.get(device_mac) is None:
                new_device = True
                _LOGGER.info("new")

            self.devices[device_mac] = device

        async_dispatcher_send(self.hass, self.signal_device_update)

        if new_device:
            async_dispatcher_send(self.hass, self.signal_device_new)

    async def close(self) -> None:
        """Close the connection."""
        if self._api is not None:
            await self._api.async_disconnect()

        for func in self._on_close:
            func()
        self._on_close.clear()

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

    @property
    def signal_device_update(self) -> str:
        """Event specific per Technicolor entry to signal updates in devices."""
        return f"{DOMAIN}-device-update"

    @property
    def signal_device_new(self) -> str:
        """Event specific per Technicolor entry to signal new device."""
        return f"{DOMAIN}-device-new"

    @property
    def host(self) -> str:
        """Return router hostname."""
        return self._api.host

    @property
    def unique_id(self) -> str:
        """Return router unique id."""
        return self._entry.unique_id or self._entry.entry_id

    @property
    def devices(self) -> dict[str, TechnicolorDeviceScanner]:
        """Return devices."""
        return self._devices

    @property
    def sensors_coordinator(self) -> dict[str, Any]:
        """Return sensors coordinators."""
        return self._sensors_coordinator
