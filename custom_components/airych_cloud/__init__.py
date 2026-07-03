"""The Airych Cloud integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import AirychApiError, AirychAuth, AirychAuthError, AirychBackendClient
from .const import (
    CONF_APP_ACCESS_TOKEN,
    CONF_BACKEND_URL,
    CONF_CUSTOMER_ID,
    CONF_PLUGIN_REFRESH_TOKEN,
    CONF_TB_ACCESS_TOKEN,
    CONF_TB_EXPIRES_AT,
    CONF_TB_URL,
    DEFAULT_TB_URL,
    DOMAIN,
    PLATFORMS,
    SIGNAL_CAMERA_SNAPSHOT,
)
from .coordinator import AirychCoordinator
from .registry_cleanup import async_cleanup_legacy_camera_entities

_LOGGER = logging.getLogger(__name__)

type AirychConfigEntry = ConfigEntry[AirychCoordinator]

SERVICE_PLAY_HUB_ALARM = "play_hub_alarm"
SERVICE_PLAY_CAMERA_ALARM = "play_camera_alarm"
SERVICE_START_CAMERA_RECORDING = "start_camera_recording"
SERVICE_STOP_CAMERA_RECORDING = "stop_camera_recording"
SERVICE_CAPTURE_CAMERA_SNAPSHOT = "capture_camera_snapshot"
SERVICE_SEND_MOBILE_NOTIFICATION = "send_mobile_notification"
LEGACY_SERVICE_SEND_ALERT = "send_alert"

SERVICES = (
    SERVICE_PLAY_HUB_ALARM,
    SERVICE_PLAY_CAMERA_ALARM,
    SERVICE_START_CAMERA_RECORDING,
    SERVICE_STOP_CAMERA_RECORDING,
    SERVICE_CAPTURE_CAMERA_SNAPSHOT,
    SERVICE_SEND_MOBILE_NOTIFICATION,
)

SERVICE_DEVICE_IDS = vol.All(cv.ensure_list, [cv.string])
SERVICE_DURATION = vol.All(vol.Coerce(int), vol.Range(min=1, max=86400))
SERVICE_VOLUME = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))

ALARM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): SERVICE_DEVICE_IDS,
        vol.Optional("duration"): SERVICE_DURATION,
        vol.Optional("volume"): SERVICE_VOLUME,
        vol.Optional("tone"): cv.string,
    }
)
START_RECORDING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): SERVICE_DEVICE_IDS,
        vol.Optional("duration"): SERVICE_DURATION,
        vol.Optional("reason"): cv.string,
    }
)
STOP_RECORDING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): SERVICE_DEVICE_IDS,
    }
)
CAPTURE_SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): SERVICE_DEVICE_IDS,
        vol.Optional("resolution", default="low"): vol.In(["low", "high"]),
        vol.Optional("with_osd", default=False): cv.boolean,
    }
)
SEND_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Optional("title"): cv.string,
        vol.Required("message"): cv.string,
        vol.Optional("level", default="info"): vol.In(["info", "warning", "critical"]),
        vol.Optional("camera"): cv.entity_id,
        vol.Optional("include_snapshot", default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: AirychConfigEntry) -> bool:
    """Set up Airych Cloud from a config entry."""
    session = async_get_clientsession(hass)
    backend = AirychBackendClient(session, entry.data[CONF_BACKEND_URL])

    async def _save_tokens(tokens: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_APP_ACCESS_TOKEN: tokens.get("app_access_token")
                or entry.data.get(CONF_APP_ACCESS_TOKEN, ""),
                CONF_TB_ACCESS_TOKEN: tokens["tb_access_token"],
                CONF_TB_EXPIRES_AT: tokens["tb_expires_at"],
                CONF_PLUGIN_REFRESH_TOKEN: tokens["plugin_refresh_token"],
            },
        )

    auth = AirychAuth(
        backend,
        tb_url=entry.data.get(CONF_TB_URL, DEFAULT_TB_URL),
        customer_id=entry.data[CONF_CUSTOMER_ID],
        app_access_token=entry.data.get(CONF_APP_ACCESS_TOKEN),
        access_token=entry.data[CONF_TB_ACCESS_TOKEN],
        expires_at=float(entry.data.get(CONF_TB_EXPIRES_AT) or 0),
        plugin_refresh_token=entry.data[CONF_PLUGIN_REFRESH_TOKEN],
        on_tokens_updated=_save_tokens,
    )

    coordinator = AirychCoordinator(hass, entry, backend, auth)
    try:
        await coordinator.async_initialize()
    except AirychAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UpdateFailed as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def _cleanup_legacy_entities(*_: Any) -> None:
        removed, updated = async_cleanup_legacy_camera_entities(hass)
        if removed or updated:
            _LOGGER.debug(
                "Cleaned up legacy camera entities: removed=%s updated=%s",
                removed,
                updated,
            )

    _cleanup_legacy_entities()
    entry.async_on_unload(async_call_later(hass, 5, _cleanup_legacy_entities))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AirychConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: AirychCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            for service in (*SERVICES, LEGACY_SERVICE_SEND_ALERT):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: AirychConfigEntry) -> None:
    """Revoke the plugin session server-side when the entry is deleted."""
    session = async_get_clientsession(hass)
    backend = AirychBackendClient(session, entry.data[CONF_BACKEND_URL])
    await backend.unpair(entry.data[CONF_PLUGIN_REFRESH_TOKEN])


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Airych action services."""
    if hass.services.has_service(DOMAIN, LEGACY_SERVICE_SEND_ALERT):
        hass.services.async_remove(DOMAIN, LEGACY_SERVICE_SEND_ALERT)

    async def handle_play_hub_alarm(call: ServiceCall) -> None:
        targets = _hub_targets_from_devices(hass, call.data[CONF_DEVICE_ID])
        _require_targets(targets, "Select at least one Airych VioStation.")
        _LOGGER.info(
            "%s is not wired yet: hubs=%s data=%s",
            call.service,
            [hub_id for _, hub_id in targets],
            _service_log_data(call.data),
        )

    async def handle_play_camera_alarm(call: ServiceCall) -> None:
        targets = _camera_targets_from_devices(hass, call.data[CONF_DEVICE_ID])
        _require_targets(targets, "Select at least one Airych VioCam.")
        _LOGGER.info(
            "%s is not wired yet: cameras=%s data=%s",
            call.service,
            [camera_id for _, _, camera_id in targets],
            _service_log_data(call.data),
        )

    async def handle_start_camera_recording(call: ServiceCall) -> None:
        targets = _camera_targets_from_devices(hass, call.data[CONF_DEVICE_ID])
        _require_targets(targets, "Select at least one Airych VioCam.")
        _LOGGER.info(
            "%s is not wired yet: cameras=%s data=%s",
            call.service,
            [camera_id for _, _, camera_id in targets],
            _service_log_data(call.data),
        )

    async def handle_stop_camera_recording(call: ServiceCall) -> None:
        targets = _camera_targets_from_devices(hass, call.data[CONF_DEVICE_ID])
        _require_targets(targets, "Select at least one Airych VioCam.")
        _LOGGER.info(
            "%s is not wired yet: cameras=%s",
            call.service,
            [camera_id for _, _, camera_id in targets],
        )

    async def handle_capture_camera_snapshot(call: ServiceCall) -> None:
        targets = _camera_targets_from_devices(hass, call.data[CONF_DEVICE_ID])
        _require_targets(targets, "Select at least one Airych VioCam.")
        resolution = call.data["resolution"]
        if call.data["with_osd"]:
            _LOGGER.info(
                "capture_camera_snapshot with_osd is accepted but not wired to the backend yet"
            )
        for coordinator, hub_id, camera_id in targets:
            try:
                snapshot = await coordinator.async_request_camera_snapshot(
                    hub_id, camera_id, resolution
                )
            except AirychAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except AirychApiError as err:
                raise HomeAssistantError(
                    f"Snapshot request failed for {camera_id}: {err}"
                ) from err
            _LOGGER.debug(
                "Snapshot service requested: hub=%s camera=%s resolution=%s response=%s",
                hub_id,
                camera_id,
                resolution,
                snapshot,
            )
            async_dispatcher_send(
                hass,
                SIGNAL_CAMERA_SNAPSHOT,
                hub_id,
                camera_id,
                snapshot,
            )

    async def handle_send_mobile_notification(call: ServiceCall) -> None:
        _LOGGER.info(
            "%s is not wired yet: data=%s",
            call.service,
            _service_log_data(call.data),
        )

    _async_register_service_once(
        hass, SERVICE_PLAY_HUB_ALARM, handle_play_hub_alarm, ALARM_SCHEMA
    )
    _async_register_service_once(
        hass, SERVICE_PLAY_CAMERA_ALARM, handle_play_camera_alarm, ALARM_SCHEMA
    )
    _async_register_service_once(
        hass,
        SERVICE_START_CAMERA_RECORDING,
        handle_start_camera_recording,
        START_RECORDING_SCHEMA,
    )
    _async_register_service_once(
        hass,
        SERVICE_STOP_CAMERA_RECORDING,
        handle_stop_camera_recording,
        STOP_RECORDING_SCHEMA,
    )
    _async_register_service_once(
        hass,
        SERVICE_CAPTURE_CAMERA_SNAPSHOT,
        handle_capture_camera_snapshot,
        CAPTURE_SNAPSHOT_SCHEMA,
    )
    _async_register_service_once(
        hass,
        SERVICE_SEND_MOBILE_NOTIFICATION,
        handle_send_mobile_notification,
        SEND_NOTIFICATION_SCHEMA,
    )


def _async_register_service_once(
    hass: HomeAssistant,
    service: str,
    handler: Any,
    schema: vol.Schema,
) -> None:
    """Register a service if it is not already registered."""
    if hass.services.has_service(DOMAIN, service):
        return
    hass.services.async_register(DOMAIN, service, handler, schema=schema)


def _hub_targets_from_devices(
    hass: HomeAssistant, ha_device_ids: list[str]
) -> list[tuple[AirychCoordinator, str]]:
    """Resolve HA device ids to Airych hub targets."""
    device_reg = dr.async_get(hass)
    targets: dict[tuple[str, str], tuple[AirychCoordinator, str]] = {}
    for ha_device_id in ha_device_ids:
        device = device_reg.async_get(ha_device_id)
        hub_id = _hub_id_from_device(device)
        if hub_id is None:
            _LOGGER.warning("%s is not an Airych VioStation device", ha_device_id)
            continue
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            if hub_id in coordinator.hubs:
                targets[(entry_id, hub_id)] = (coordinator, hub_id)
                break
        else:
            _LOGGER.warning("No loaded Airych entry owns hub %s", hub_id)
    return list(targets.values())


def _camera_targets_from_devices(
    hass: HomeAssistant, ha_device_ids: list[str]
) -> list[tuple[AirychCoordinator, str, str]]:
    """Resolve HA device ids to Airych camera targets."""
    device_reg = dr.async_get(hass)
    targets: dict[tuple[str, str], tuple[AirychCoordinator, str, str]] = {}
    for ha_device_id in ha_device_ids:
        device = device_reg.async_get(ha_device_id)
        camera_id = _camera_id_from_device(device)
        if camera_id is None:
            _LOGGER.warning("%s is not an Airych VioCam device", ha_device_id)
            continue
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            hub = coordinator.find_hub_for_camera(camera_id)
            if hub is not None:
                targets[(entry_id, camera_id)] = (coordinator, hub.id, camera_id)
                break
        else:
            _LOGGER.warning("No loaded Airych entry owns camera %s", camera_id)
    return list(targets.values())


def _hub_id_from_device(device: dr.DeviceEntry | None) -> str | None:
    """Extract the Airych hub id from a HA device entry."""
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith("hub:"):
            return identifier.split("hub:", 1)[1]
    return None


def _camera_id_from_device(device: dr.DeviceEntry | None) -> str | None:
    """Extract the Airych camera id from a HA device entry."""
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith("cam:"):
            return identifier.split("cam:", 1)[1]
    return None


def _require_targets(targets: list[Any], message: str) -> None:
    """Raise a HA service error when no usable target was selected."""
    if not targets:
        raise HomeAssistantError(message)


def _service_log_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return service data in a log-friendly shape."""
    redacted = dict(data)
    for key in ("title", "message", "reason"):
        if key in redacted:
            redacted[key] = "<redacted>"
    return redacted
