"""Snapshot button for each Airych Cloud camera."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_CAMERA_SNAPSHOT, SIGNAL_NEW_CAMERA
from .coordinator import AirychCoordinator
from .entity import AirychCameraBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up snapshot buttons for existing and future cameras."""
    coordinator: AirychCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        SnapshotButton(coordinator, hub_id, camera_id)
        for hub_id, hub in coordinator.hubs.items()
        for camera_id in coordinator.camera_ids_for_hub(hub)
    ]
    async_add_entities(entities)

    @callback
    def _add_camera(hub_id: str, camera_id: str) -> None:
        async_add_entities([SnapshotButton(coordinator, hub_id, camera_id)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_CAMERA, _add_camera)
    )


class SnapshotButton(AirychCameraBaseEntity, ButtonEntity):
    """Trigger a cloud snapshot for the camera."""

    _attr_name = "Snapshot"
    _attr_icon = "mdi:camera"

    def __init__(
        self, coordinator: AirychCoordinator, hub_id: str, camera_id: str
    ) -> None:
        super().__init__(coordinator, hub_id, camera_id, "snapshot")

    async def async_press(self) -> None:
        _LOGGER.info(
            "Snapshot button pressed: hub=%s camera=%s",
            self._hub_id,
            self._camera_id,
        )
        snapshot = await self.coordinator.async_request_camera_snapshot(
            self._hub_id, self._camera_id
        )
        _LOGGER.debug(
            "Snapshot button requested: hub=%s camera=%s response=%s",
            self._hub_id,
            self._camera_id,
            snapshot,
        )
        async_dispatcher_send(
            self.hass,
            SIGNAL_CAMERA_SNAPSHOT,
            self._hub_id,
            self._camera_id,
            snapshot,
        )
