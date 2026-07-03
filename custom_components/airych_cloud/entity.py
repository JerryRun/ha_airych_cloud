"""Shared base entities and device-identifier helpers."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import AirychCoordinator
from .models import Camera, Hub

DEFAULT_HUB_MODEL = "VioStation"
DEFAULT_CAMERA_MODEL = "VioCam"


def hub_identifier(hub_id: str) -> tuple[str, str]:
    return (DOMAIN, f"hub:{hub_id}")


def camera_identifier(camera_id: str) -> tuple[str, str]:
    return (DOMAIN, f"cam:{camera_id}")


class AirychHubEntity(CoordinatorEntity[AirychCoordinator]):
    """Base entity attached to a hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AirychCoordinator, hub_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        self._attr_unique_id = f"{hub_id}_{key}"

    @property
    def hub(self) -> Hub | None:
        return self.coordinator.hubs.get(self._hub_id)

    @property
    def device_info(self) -> DeviceInfo:
        hub = self.hub
        return DeviceInfo(
            identifiers={hub_identifier(self._hub_id)},
            name=hub.name if hub else self._hub_id,
            manufacturer=MANUFACTURER,
            model=(hub.model or DEFAULT_HUB_MODEL) if hub else DEFAULT_HUB_MODEL,
            sw_version=hub.sw_version if hub else None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.hub is not None


class AirychCameraBaseEntity(CoordinatorEntity[AirychCoordinator]):
    """Base entity attached to a camera device (child of a hub)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AirychCoordinator,
        hub_id: str,
        camera_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_{key}"

    @property
    def hub(self) -> Hub | None:
        return self.coordinator.hubs.get(self._hub_id)

    @property
    def camera_model(self) -> Camera | None:
        hub = self.hub
        if hub is not None and self._camera_id in hub.camera_ids:
            return hub.camera(self._camera_id)
        return None

    @property
    def device_info(self) -> DeviceInfo:
        cam = self.camera_model
        return DeviceInfo(
            identifiers={camera_identifier(self._camera_id)},
            name=cam.name if cam else self._camera_id,
            manufacturer=MANUFACTURER,
            model=(cam.model or DEFAULT_CAMERA_MODEL) if cam else DEFAULT_CAMERA_MODEL,
            hw_version=cam.hw_version if cam else None,
            via_device=hub_identifier(self._hub_id),
        )

    @property
    def available(self) -> bool:
        return super().available and self.camera_model is not None
