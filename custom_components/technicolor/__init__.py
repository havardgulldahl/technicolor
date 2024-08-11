"""The technicolor integration."""

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_PORT, CONF_SSL, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, Event
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    KEY_COORDINATOR,
    KEY_COORDINATOR_FIRMWARE,
    KEY_COORDINATOR_LINK,
    KEY_COORDINATOR_SPEED,
    KEY_COORDINATOR_TRAFFIC,
    KEY_COORDINATOR_UTIL,
    KEY_ROUTER,
    PLATFORMS,
)
from .errors import CannotLoginException
from .router import TechnicolorRouter

PLATFORMS = ["device_tracker"]
SCAN_INTERVAL = timedelta(seconds=60)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the technicolor integration."""
    conf = config.get(DOMAIN)
    if conf is None:
        return True

    # save the options from config yaml
    options = {}
    hass.data[DOMAIN] = {"yaml_options": options}

    # check if already configured
    domains_list = hass.config_entries.async_domains()
    if DOMAIN in domains_list:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Technicolor platform."""

    # import options from yaml if empty
    yaml_options = hass.data.get(DOMAIN, {}).pop("yaml_options", {})
    yaml_options["interval_seconds"] = SCAN_INTERVAL.seconds
    if not entry.options and yaml_options:
        hass.config_entries.async_update_entry(entry, options=yaml_options)

    router = TechnicolorRouter(hass, entry)
    if router is None:
        raise CannotLoginException("Unable to login to the router")
    await router.setup()

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, "device_tracker")
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DOMAIN: router,
    }

    async def async_close_connection(event: Event) -> None:
        """Close Technicolor connection on HA Stop."""
        await router.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_close_connection)
    )

    async def async_update_devices() -> bool:
        """Fetch data from the router."""
        if router.track_devices:
            _LOGGER.debug("Updating devices from .async_update_devices")
            return await router.update_all()
        return False

    async def async_update_traffic_meter() -> dict[str, Any] | None:
        """Fetch data from the router."""
        return await router.async_get_traffic_meter()

    async def async_update_speed_test() -> dict[str, Any] | None:
        """Fetch data from the router."""
        return await router.async_get_speed_test()

    async def async_check_firmware() -> dict[str, Any] | None:
        """Check for new firmware of the router."""
        return await router.async_check_new_firmware()

    async def async_update_utilization() -> dict[str, Any] | None:
        """Fetch data from the router."""
        return await router.async_get_utilization()

    async def async_check_link_status() -> dict[str, Any] | None:
        """Fetch data from the router."""
        return await router.async_get_link_status()

    # Create update coordinators
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{router.device_name} Devices",
        update_method=async_update_devices,
        update_interval=SCAN_INTERVAL,
    )
    coordinator_traffic_meter = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{router.device_name} Traffic meter",
        update_method=async_update_traffic_meter,
        update_interval=SCAN_INTERVAL,
    )
    coordinator_utilization = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{router.device_name} Utilization",
        update_method=async_update_utilization,
        update_interval=SCAN_INTERVAL,
    )
    coordinator_link = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{router.device_name} Ethernet Link Status",
        update_method=async_check_link_status,
        update_interval=SCAN_INTERVAL,
    )

    if router.track_devices:
        await coordinator.async_config_entry_first_refresh()
    await coordinator_utilization.async_config_entry_first_refresh()
    await coordinator_link.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        KEY_ROUTER: router,
        KEY_COORDINATOR: coordinator,
        KEY_COORDINATOR_TRAFFIC: coordinator_traffic_meter,
        KEY_COORDINATOR_UTIL: coordinator_utilization,
        KEY_COORDINATOR_LINK: coordinator_link,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    router = hass.data[DOMAIN][entry.entry_id][KEY_ROUTER]

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    if not router.track_devices:
        router_id = None
        # Remove devices that are no longer tracked
        device_registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for device_entry in devices:
            if device_entry.via_device_id is None:
                router_id = device_entry.id
                continue  # do not remove the router itself
            device_registry.async_update_device(
                device_entry.id, remove_config_entry_id=entry.entry_id
            )
        # Remove entities that are no longer tracked
        entity_registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        for entity_entry in entries:
            if entity_entry.device_id is not router_id:
                entity_registry.async_remove(entity_entry.entity_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a device from a config entry."""
    router = hass.data[DOMAIN][config_entry.entry_id][KEY_ROUTER]

    device_mac = None
    for connection in device_entry.connections:
        if connection[0] == dr.CONNECTION_NETWORK_MAC:
            device_mac = connection[1]
            break

    if device_mac is None:
        return False

    if device_mac not in router.devices:
        return True

    return not router.devices[device_mac]["active"]
