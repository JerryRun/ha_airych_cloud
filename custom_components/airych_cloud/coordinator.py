"""Coordinator: owns cloud state and pushes updates to entities."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AirychAuth,
    AirychBackendClient,
    AirychApiError,
    AirychAuthError,
    ThingsBoardRest,
    ThingsBoardWs,
)
from .const import (
    CONF_SELECTED_CAMERA_IDS,
    CONF_SELECTED_HUB_IDS,
    DOMAIN,
    SIGNAL_NEW_CAMERA,
)
from .models import Hub

_LOGGER = logging.getLogger(__name__)

ACTIVE_POLL_INTERVAL = timedelta(seconds=60)


class AirychCoordinator(DataUpdateCoordinator[dict[str, Hub]]):
    """Holds hubs/cameras, subscribes to TB attribute pushes, polls activity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        backend: AirychBackendClient,
        auth: AirychAuth,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=ACTIVE_POLL_INTERVAL,
        )
        self.entry = entry
        self.backend = backend
        self.auth = auth
        session = async_get_clientsession(hass)
        self.rest = ThingsBoardRest(session, auth)
        self.ws = ThingsBoardWs(session, auth, self._on_ws_update)
        self.hubs: dict[str, Hub] = {}
        self.webrtc_configs: dict[str, dict[str, Any]] = {}
        self._selected_hub_ids = set(entry.data.get(CONF_SELECTED_HUB_IDS, []))
        self._selected_camera_ids = set(entry.data.get(CONF_SELECTED_CAMERA_IDS, []))
        self._restrict_hubs = CONF_SELECTED_HUB_IDS in entry.data
        self._restrict_cameras = CONF_SELECTED_CAMERA_IDS in entry.data

    # ------------------------------------------------------------------ setup
    async def async_initialize(self) -> None:
        """Initial load: device list + attribute snapshot, then start the WS."""
        try:
            hub_devices = await self.rest.async_get_customer_hubs()
        except AirychAuthError as err:
            raise err
        except AirychApiError as err:
            raise UpdateFailed(f"failed to load devices: {err}") from err

        for dev in hub_devices:
            hub_id = dev["id"]["id"] if isinstance(dev.get("id"), dict) else dev["id"]
            if self._restrict_hubs and hub_id not in self._selected_hub_ids:
                continue
            hub = Hub(hub_id, dev.get("name", hub_id))
            try:
                hub.update_attrs(await self.rest.async_get_client_attributes(hub_id))
                hub.active = await self.rest.async_get_device_active(hub_id)
            except AirychApiError as err:
                _LOGGER.warning("Failed initial attribute load for hub %s: %s", hub_id, err)
            self.hubs[hub_id] = hub

        self.async_set_updated_data(self.hubs)
        self.ws.set_devices(list(self.hubs))
        await self._async_prime_webrtc_configs()
        await self.ws.async_start()

    async def async_shutdown(self) -> None:
        await self.ws.async_stop()
        await super().async_shutdown()

    # ------------------------------------------------------- periodic refresh
    async def _async_update_data(self) -> dict[str, Hub]:
        """Poll the server-scope ``active`` flag (not pushed over the attr WS)."""
        for hub in self.hubs.values():
            try:
                hub.active = await self.rest.async_get_device_active(hub.id)
            except AirychAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except AirychApiError as err:
                _LOGGER.debug("active poll failed for %s: %s", hub.id, err)
        return self.hubs

    # --------------------------------------------------------------- ws push
    @callback
    def _on_ws_update(self, device_id: str, delta: dict[str, Any]) -> None:
        hub = self.hubs.get(device_id)
        if hub is None:
            return
        before = set(self.camera_ids_for_hub(hub))
        hub.update_attrs(delta)
        after = set(self.camera_ids_for_hub(hub))

        for new_cam in after - before:
            _LOGGER.debug("New camera discovered: hub=%s camera=%s", device_id, new_cam)
            async_dispatcher_send(self.hass, SIGNAL_NEW_CAMERA, device_id, new_cam)

        self.async_set_updated_data(self.hubs)

    # --------------------------------------------------------------- helpers
    def camera_ids_for_hub(self, hub: Hub) -> list[str]:
        """Return camera ids enabled for this config entry."""
        if not self._restrict_cameras:
            return hub.camera_ids
        return [
            camera_id
            for camera_id in hub.camera_ids
            if camera_id in self._selected_camera_ids
        ]

    def find_hub_for_camera(self, camera_id: str) -> Hub | None:
        for hub in self.hubs.values():
            if camera_id in self.camera_ids_for_hub(hub):
                return hub
        return None

    async def _async_prime_webrtc_configs(self) -> None:
        """Best-effort warmup so HA can advertise ICE servers before playback."""
        for hub_id in self.hubs:
            try:
                await self.async_get_webrtc_config(hub_id)
            except AirychApiError as err:
                _LOGGER.debug("Failed to warm WebRTC config for hub %s: %s", hub_id, err)
            except AirychAuthError:
                raise

    async def async_get_webrtc_config(self, hub_id: str) -> dict[str, Any]:
        """Return cached signal/STUN/TURN config for a hub, loading if needed."""
        if hub_id in self.webrtc_configs:
            return self.webrtc_configs[hub_id]
        hub = self.hubs[hub_id]
        app_token = await self.auth.async_get_app_token()
        try:
            config = await self.backend.webrtc_config(app_token, hub.tb_name)
        except AirychApiError:
            # Existing entries may still hold the old OAuth opaque token. Force
            # a refresh so the backend can return a fresh App JWT, then retry.
            await self.auth.async_refresh()
            app_token = await self.auth.async_get_app_token()
            config = await self.backend.webrtc_config(app_token, hub.tb_name)
        self.webrtc_configs[hub_id] = config
        return config

    def webrtc_config_for_hub(self, hub_id: str) -> dict[str, Any]:
        """Return cached WebRTC config for sync HA camera capability hooks."""
        return self.webrtc_configs.get(hub_id, {})

    async def async_request_camera_snapshot(
        self, hub_id: str, camera_id: str, resolution: str = "low"
    ) -> dict[str, Any]:
        """Request a fresh cloud snapshot through the App backend/TB RPC path."""
        app_token = await self.auth.async_get_app_token()
        try:
            return await self.backend.request_camera_snapshot(
                app_token, hub_id, camera_id, resolution
            )
        except AirychApiError:
            await self.auth.async_refresh()
            app_token = await self.auth.async_get_app_token()
            return await self.backend.request_camera_snapshot(
                app_token, hub_id, camera_id, resolution
            )
