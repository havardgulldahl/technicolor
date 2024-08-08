"""Errors for the Technicolor component."""

from homeassistant.exceptions import HomeAssistantError


class TechnicolorException(HomeAssistantError):
    """Base class for Technicolor exceptions."""


class CannotLoginException(TechnicolorException):
    """Unable to login to the router."""
