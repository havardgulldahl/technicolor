"""Technicolor component constants."""

from datetime import timedelta
from homeassistant.const import Platform


DOMAIN = "technicolor"

CONF_REQUIRE_IP = "require_ip"
CONF_SSH_KEY = "ssh_key"
CONF_TRACK_UNKNOWN = "track_unknown"
CONF_CONSIDER_HOME = "consider_home"

DEFAULT_TRACK_UNKNOWN = False
DEFAULT_CONSIDER_HOME = timedelta(seconds=180)
DEFAULT_NAME = "Technicolor router"

KEY_ROUTER = "router"
KEY_COORDINATOR = "coordinator"
KEY_COORDINATOR_TRAFFIC = "coordinator_traffic"
KEY_COORDINATOR_SPEED = "coordinator_speed"
KEY_COORDINATOR_FIRMWARE = "coordinator_firmware"
KEY_COORDINATOR_UTIL = "coordinator_utilization"
KEY_COORDINATOR_LINK = "coordinator_link"

MODE_AP = "ap"
MODE_ROUTER = "router"
MODE_GUEST = "guest"
MODE_WLAN = "wlan"
MODE_ETHERNET = "ethernet"

PLATFORMS = [
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
# Icons
DEVICE_ICONS = {
    MODE_AP: "mdi:access-point-network",
    MODE_ROUTER: "mdi:router-network-wireless",
    MODE_ETHERNET: "mdi:router-network",
    MODE_GUEST: "mdi:account-network-outline",  # Guest
    MODE_WLAN: "mdi:router-wireless",
}
