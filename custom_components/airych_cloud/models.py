"""Typed views over ThingsBoard hub/camera attributes.

``Hub`` holds the raw CLIENT_SCOPE attribute map (plus the server-scope
``active`` flag) and exposes parsed sub-objects. ``Camera`` is a thin live view
over its owning hub's attributes, so entities can keep a stable reference while
the underlying state is updated in place by the coordinator.
"""
from __future__ import annotations

import json
from typing import Any

from .const import (
    ATTR_HUB_CAMERAS,
    ATTR_HUB_INFO,
    ATTR_HUB_LATEST_ALERT,
    ATTR_HUB_STATUS,
    ATTR_HUB_UNREAD_ALERTS,
    CAM_KEY_PREFIX,
    CAM_SUFFIX_DETECT,
    CAM_SUFFIX_FRIENDLYNAME,
    CAM_SUFFIX_INFO,
    CAM_SUFFIX_RECORDING,
    CAM_SUFFIX_STATUS,
)


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a TB attribute value to a dict (it may arrive as a JSON string)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class Camera:
    """Live view over a single camera's attributes within a hub."""

    def __init__(self, hub: "Hub", camera_id: str) -> None:
        self.hub = hub
        self.id = camera_id

    def _block(self, suffix: str) -> dict[str, Any]:
        return _as_dict(self.hub.attrs.get(f"{CAM_KEY_PREFIX}{self.id}_{suffix}"))

    # --- identity -----------------------------------------------------------
    @property
    def info(self) -> dict[str, Any]:
        return self._block(CAM_SUFFIX_INFO)

    @property
    def name(self) -> str:
        friendly = self._block(CAM_SUFFIX_FRIENDLYNAME).get("friendlyname")
        return friendly or self.id

    @property
    def model(self) -> str | None:
        return self.info.get("device_model")

    @property
    def hw_version(self) -> str | None:
        return self.info.get("hardware_version")

    @property
    def rtsp_url(self) -> str | None:
        return self.info.get("rtsp_url")

    @property
    def ip_address(self) -> str | None:
        return self.info.get("ip_address")

    # --- status -------------------------------------------------------------
    @property
    def status(self) -> dict[str, Any]:
        return self._block(CAM_SUFFIX_STATUS)

    @property
    def online(self) -> bool:
        return bool(self.status.get("online", False))

    @property
    def battery(self) -> int | None:
        return self.status.get("battery_status")

    @property
    def wifi_strength(self) -> int | None:
        return self.status.get("wifi_strength")

    @property
    def charging(self) -> bool:
        return bool(self.status.get("charging_status", False))

    @property
    def powerplugin(self) -> bool:
        return bool(self.status.get("powerplugin_status", False))

    @property
    def sd_present(self) -> bool:
        return bool(self.status.get("sd_presence", 0))

    # --- detection ----------------------------------------------------------
    @property
    def detect(self) -> dict[str, Any]:
        return self._block(CAM_SUFFIX_DETECT)

    @property
    def has_person(self) -> bool:
        return self.detect.get("has_person") is True

    @property
    def fall(self) -> bool:
        return self.detect.get("person_falled") is True

    @property
    def door_open(self) -> bool:
        return self.detect.get("door_opened") is True

    @property
    def window_open(self) -> bool:
        return self.detect.get("window_opened") is True

    @property
    def smogfire(self) -> bool:
        return self.detect.get("smogfire") is True

    # --- recording ----------------------------------------------------------
    @property
    def recording(self) -> bool:
        return bool(self._block(CAM_SUFFIX_RECORDING).get("camera_recording", False))

    @property
    def streaming_sid(self) -> str | None:
        return self._block(CAM_SUFFIX_RECORDING).get("streaming_sid")


class Hub:
    """A ThingsBoard hub device and the cameras hanging off it."""

    def __init__(self, device_id: str, tb_name: str) -> None:
        self.id = device_id
        self.tb_name = tb_name
        self.attrs: dict[str, Any] = {}
        self.active: bool = False

    def update_attrs(self, delta: dict[str, Any]) -> None:
        """Merge an attribute delta (full snapshot or WS update)."""
        self.attrs.update(delta)

    # --- identity -----------------------------------------------------------
    @property
    def info(self) -> dict[str, Any]:
        return _as_dict(self.attrs.get(ATTR_HUB_INFO))

    @property
    def name(self) -> str:
        info = self.info
        return info.get("label") or info.get("id") or self.tb_name or self.id

    @property
    def model(self) -> str | None:
        return self.info.get("model")

    @property
    def sw_version(self) -> str | None:
        return self.info.get("swver")

    # --- status -------------------------------------------------------------
    @property
    def status(self) -> dict[str, Any]:
        return _as_dict(self.attrs.get(ATTR_HUB_STATUS))

    @property
    def battery(self) -> int | None:
        return self.status.get("batteryCapacity")

    @property
    def wifi_strength(self) -> int | None:
        return self.status.get("wifi_strength")

    @property
    def is_charging(self) -> bool:
        return str(self.status.get("isCharging", "")).lower() == "charging"

    @property
    def power_supply(self) -> bool:
        return bool(self.status.get("isPowerSupply", False))

    @property
    def wifi_connected(self) -> bool:
        return bool(self.status.get("WifiCon", False))

    @property
    def eth_connected(self) -> bool:
        return bool(self.status.get("ethCon", False))

    @property
    def latest_alert(self) -> dict[str, Any]:
        return _as_dict(self.attrs.get(ATTR_HUB_LATEST_ALERT))

    @property
    def unread_alerts(self) -> int | None:
        return self.attrs.get(ATTR_HUB_UNREAD_ALERTS)

    # --- cameras ------------------------------------------------------------
    @property
    def camera_ids(self) -> list[str]:
        return list(_as_dict(self.attrs.get(ATTR_HUB_CAMERAS)).get("ids", []) or [])

    def camera(self, camera_id: str) -> Camera:
        return Camera(self, camera_id)
