"""Entity registry cleanup helpers for Airych Cloud."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LEGACY_CAMERA_ENTITY_SUFFIXES = ("_unread_alarms", "_sleeping")
_CAMERA_PERSON_SUFFIX = "_person"
_PERSON_DEVICE_CLASS = "presence"


def async_cleanup_legacy_camera_entities(hass: HomeAssistant) -> tuple[int, int]:
    """Clean up legacy camera registry entries.

    Returns ``(removed_count, updated_count)``.
    """
    registry = er.async_get(hass)
    removed = 0
    updated = 0
    for entity in list(registry.entities.values()):
        unique_id = entity.unique_id or ""
        if entity.platform != DOMAIN:
            continue
        if unique_id.endswith(_LEGACY_CAMERA_ENTITY_SUFFIXES):
            registry.async_remove(entity.entity_id)
            removed += 1
            continue
        if (
            entity.domain == "binary_sensor"
            and unique_id.endswith(_CAMERA_PERSON_SUFFIX)
            and entity.original_device_class != _PERSON_DEVICE_CLASS
        ):
            registry.async_update_entity(
                entity.entity_id,
                original_device_class=_PERSON_DEVICE_CLASS,
            )
            updated += 1
    return removed, updated
