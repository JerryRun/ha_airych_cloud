"""Device automation actions for Airych Cloud cameras."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from .const import DOMAIN

ACTION_TYPE_SNAPSHOT = "snapshot"

ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In([ACTION_TYPE_SNAPSHOT]),
    }
)


def _is_camera_device(device: dr.DeviceEntry | None) -> bool:
    return bool(device) and any(
        ident[0] == DOMAIN and ident[1].startswith("cam:")
        for ident in device.identifiers
    )


async def async_get_actions(hass: HomeAssistant, device_id: str) -> list[dict]:
    """Return the actions available for an Airych camera device."""
    device = dr.async_get(hass).async_get(device_id)
    if not _is_camera_device(device):
        return []
    base = {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN}
    return [
        {**base, CONF_TYPE: ACTION_TYPE_SNAPSHOT},
    ]


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict:
    """Return action capabilities."""
    return {}


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Execute the device action by calling the snapshot service."""
    service_data: dict = {"device_id": [config[CONF_DEVICE_ID]]}

    await hass.services.async_call(
        DOMAIN,
        "capture_camera_snapshot",
        service_data,
        blocking=True,
        context=context,
    )


def async_validate_action_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    return ACTION_SCHEMA(config)
