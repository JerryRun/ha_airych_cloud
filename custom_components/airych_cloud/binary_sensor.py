"""Binary sensors for Airych Cloud hubs and cameras."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NEW_CAMERA
from .coordinator import AirychCoordinator
from .entity import AirychCameraBaseEntity, AirychHubEntity
from .models import Camera, Hub


@dataclass(frozen=True, kw_only=True)
class HubBinaryDesc(BinarySensorEntityDescription):
    value_fn: Callable[[Hub], bool]
    always_available: bool = False


@dataclass(frozen=True, kw_only=True)
class CameraBinaryDesc(BinarySensorEntityDescription):
    value_fn: Callable[[Camera], bool]
    always_available: bool = False


HUB_BINARY_SENSORS: tuple[HubBinaryDesc, ...] = (
    HubBinaryDesc(
        key="online",
        name="Status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda h: h.active,
        always_available=True,
    ),
    HubBinaryDesc(
        key="power_supply",
        name="External power",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda h: h.power_supply,
    ),
    HubBinaryDesc(
        key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda h: h.is_charging,
    ),
    HubBinaryDesc(
        key="wifi",
        name="Wi-Fi connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.wifi_connected,
    ),
    HubBinaryDesc(
        key="ethernet",
        name="Ethernet connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda h: h.eth_connected,
    ),
)

CAMERA_BINARY_SENSORS: tuple[CameraBinaryDesc, ...] = (
    CameraBinaryDesc(
        key="online",
        name="Status",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: c.online,
        always_available=True,
    ),
    CameraBinaryDesc(
        key="person",
        name="Person",
        device_class=BinarySensorDeviceClass.PRESENCE,
        value_fn=lambda c: c.has_person,
    ),
    CameraBinaryDesc(
        key="fall",
        name="Fall",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda c: c.fall,
    ),
    CameraBinaryDesc(
        key="door",
        name="Door",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda c: c.door_open,
    ),
    CameraBinaryDesc(
        key="window",
        name="Window",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda c: c.window_open,
    ),
    CameraBinaryDesc(
        key="smogfire",
        name="Smoke/fire",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda c: c.smogfire,
    ),
    CameraBinaryDesc(
        key="recording",
        name="Recording",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda c: c.recording,
    ),
    CameraBinaryDesc(
        key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.charging,
    ),
    CameraBinaryDesc(
        key="powerplugin",
        name="External power",
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.powerplugin,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors for existing and future hubs/cameras."""
    coordinator: AirychCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = []
    for hub_id, hub in coordinator.hubs.items():
        for desc in HUB_BINARY_SENSORS:
            entities.append(AirychHubBinarySensor(coordinator, hub_id, desc))
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
) -> list[BinarySensorEntity]:
    return [
        AirychCameraBinarySensor(coordinator, hub_id, camera_id, desc)
        for desc in CAMERA_BINARY_SENSORS
    ]


class AirychHubBinarySensor(AirychHubEntity, BinarySensorEntity):
    """Binary sensor on a hub."""

    entity_description: HubBinaryDesc

    def __init__(
        self, coordinator: AirychCoordinator, hub_id: str, desc: HubBinaryDesc
    ) -> None:
        super().__init__(coordinator, hub_id, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return self.entity_description.value_fn(hub) if hub else None

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            # Stay available so connectivity state can report off/online.
            return self.coordinator.last_update_success and self.hub is not None
        return super().available


class AirychCameraBinarySensor(AirychCameraBaseEntity, BinarySensorEntity):
    """Binary sensor on a camera."""

    entity_description: CameraBinaryDesc

    def __init__(
        self,
        coordinator: AirychCoordinator,
        hub_id: str,
        camera_id: str,
        desc: CameraBinaryDesc,
    ) -> None:
        super().__init__(coordinator, hub_id, camera_id, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        cam = self.camera_model
        return self.entity_description.value_fn(cam) if cam else None

    @property
    def available(self) -> bool:
        if self.entity_description.always_available:
            # Stay available so connectivity state can report off/online.
            return self.coordinator.last_update_success and self.hub is not None
        return super().available
