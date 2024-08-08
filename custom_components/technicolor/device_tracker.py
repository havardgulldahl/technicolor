"""Support for Technicolor routers."""

import logging
from typing import Any, Dict

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICES,
    CONF_EXCLUDE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from technicolorgateway.datamodels import (
    DiagnosticsConnection,
    NetworkDevice,
    SystemInfo,
)

from .const import DOMAIN
from .router import TechnicolorRouter

DEFAULT_DEVICE_NAME = "Unknown device"
SOURCE_TYPE_ROUTER = "router"
ATTR_LAST_TIME_REACHABLE = "last_time_reachable"

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_DEVICES, default=[]): vol.All(
                    cv.ensure_list, [cv.string]
                ),
                vol.Optional(CONF_EXCLUDE, default=[]): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up device tracker for Technicolor component."""
    router = hass.data[DOMAIN][entry.entry_id][DOMAIN]

    tracked = set()

    @callback
    def update_router():
        """Update the values of the router."""
        add_entities(router, async_add_entities, tracked)

    router.listeners.append(
        async_dispatcher_connect(hass, router.signal_device_new, update_router)
    )

    update_router()


@callback
def add_entities(
    router: TechnicolorRouter,
    async_add_entities: AddEntitiesCallback,
    tracked: set[str],
):
    """Add new tracker entities from the gateway."""
    _LOGGER.info(f"add_entities tracked ${tracked}")
    new_tracked = []

    for mac, device in router.devices.items():
        if mac in tracked:
            continue

        new_tracked.append(TechnicolorDeviceScanner(router, device))
        tracked.add(mac)
        _LOGGER.info(f"add_entities {mac}")

    if new_tracked:
        async_add_entities(new_tracked, True)


class TechnicolorDeviceScanner(ScannerEntity):
    """Representation of a Technicolor device."""

    def __init__(self, router: TechnicolorRouter, device: NetworkDevice) -> None:
        """Initialize a Technicolor device."""
        self._router = router
        self._device = device
        self._mac = str(device.mac_address)
        self._active = False
        self._attr_name = device.friendly_name or DEFAULT_DEVICE_NAME

    @callback
    def async_update_state(self) -> None:
        """Update the Technicolor device."""
        device = self._router.devices[self._mac]
        self._device["ip"] = device["ip"]
        _LOGGER.info(f"updating state for ${self._mac} with ip ${self._device['ip']}")
        self._active = self._device["ip"] is not None and self._device["ip"] != ""

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return str(self._device.mac_address)

    @property
    def name(self) -> str:
        """Return the name."""
        return self._attr_name

    @property
    def is_connected(self):
        """Return true if the device is connected to the network."""
        return self._active

    @property
    def source_type(self) -> str:
        """Return the source type."""
        return SOURCE_TYPE_ROUTER

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the attributes."""
        return {}

    @property
    def hostname(self) -> str:
        """Return the hostname of device."""
        return self._device.host_name

    @property
    def ip_address(self) -> str:
        """Return the primary ip address of the device."""
        return str(self._device.ipv4)

    @property
    def mac_address(self) -> str:
        """Return the mac address of the device."""
        return str(self._device.mac_address)

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return the device information."""
        return {}

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    @callback
    def async_on_demand_update(self):
        """Update state."""
        _LOGGER.info("in async_on_demand_update")
        self.async_update_state()
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Register state update callback."""
        _LOGGER.info("in async_added_to_hass")
        self.async_update_state()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._router.signal_device_update,
                self.async_on_demand_update,
            )
        )
