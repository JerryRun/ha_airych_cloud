"""Sensors for Airych Cloud hubs and cameras."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NEW_CAMERA
from .coordinator import AirychCoordinator
from .entity import AirychCameraBaseEntity, AirychHubEntity
from .models import Camera, Hub
from .registry_cleanup import async_cleanup_legacy_camera_entities


@dataclass(frozen=True, kw_only=True)
class HubSensorDesc(SensorEntityDescription):
    value_fn: Callable[[Hub], Any]


@dataclass(frozen=True, kw_only=True)
class CameraSensorDesc(SensorEntityDescription):
    value_fn: Callable[[Camera], Any]


HUB_SENSORS: tuple[HubSensorDesc, ...] = (
    HubSensorDesc(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda h: h.battery,
    ),
    HubSensorDesc(
        key="wifi_strength",
        name="Wi-Fi signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.wifi_strength,
    ),
    HubSensorDesc(
        key="latest_alert",
        name="Latest alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.latest_alert.get("type"),
    ),
    HubSensorDesc(
        key="unread_alerts",
        name="Unread alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda h: h.unread_alerts,
    ),
)

CAMERA_SENSORS: tuple[CameraSensorDesc, ...] = (
    CameraSensorDesc(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.battery,
    ),
    CameraSensorDesc(
        key="wifi_strength",
        name="Wi-Fi signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.wifi_strength,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for existing and future hubs/cameras."""
    coordinator: AirychCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_cleanup_legacy_camera_entities(hass)

    entities: list[SensorEntity] = []
    for hub_id, hub in coordinator.hubs.items():
        for desc in HUB_SENSORS:
            entities.append(AirychHubSensor(coordinator, hub_id, desc))
        for camera_id in coordinator.camera_ids_for_hub(hub):
            entities.extend(_camera_entities(coordinator, hub_id, camera_id))
    async_add_entities(entities)

    @callback
    def _add_camera(hub_id: str, camera_id: str) -> None:
        async_add_entities(_camera_entities(coordinator, hub_id, camera_id))

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_CAMERA, _add_camera)
    )


def _camera_entities(
    coordinator: AirychCoordinator, hub_id: str, camera_id: str
) -> list[SensorEntity]:
    return [
        AirychCameraSensor(coordinator, hub_id, camera_id, desc)
        for desc in CAMERA_SENSORS
    ]


class AirychHubSensor(AirychHubEntity, SensorEntity):
    """Sensor on a hub."""

    entity_description: HubSensorDesc

    def __init__(
        self, coordinator: AirychCoordinator, hub_id: str, desc: HubSensorDesc
    ) -> None:
        super().__init__(coordinator, hub_id, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        hub = self.hub
        return self.entity_description.value_fn(hub) if hub else None


class AirychCameraSensor(AirychCameraBaseEntity, SensorEntity):
    """Sensor on a camera."""

    entity_description: CameraSensorDesc

    def __init__(
        self,
        coordinator: AirychCoordinator,
        hub_id: str,
        camera_id: str,
        desc: CameraSensorDesc,
    ) -> None:
        super().__init__(coordinator, hub_id, camera_id, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        cam = self.camera_model
        return self.entity_description.value_fn(cam) if cam else None
