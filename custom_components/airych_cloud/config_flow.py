"""Config flow for Airych Cloud — device-authorization (scan-to-pair)."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any
from urllib.parse import quote

import segno
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    instance_id,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    AirychAuth,
    AirychApiError,
    AirychAuthError,
    AirychBackendClient,
    PairDenied,
    PairExpired,
    PairPending,
    ThingsBoardRest,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_NAME,
    CONF_APP_ACCESS_TOKEN,
    CONF_BACKEND_URL,
    CONF_CUSTOMER_ID,
    CONF_ICE_SERVERS,
    CONF_PLUGIN_REFRESH_TOKEN,
    CONF_SELECTED_CAMERA_IDS,
    CONF_SELECTED_HUB_IDS,
    CONF_SNAPSHOT_PREVIEW_INTERVAL,
    CONF_SIGNALING_URL,
    CONF_TB_ACCESS_TOKEN,
    CONF_TB_EXPIRES_AT,
    CONF_TB_URL,
    DEFAULT_BACKEND_URL,
    DEFAULT_PAIR_INTERVAL,
    DEFAULT_PAIR_TIMEOUT,
    DEFAULT_SNAPSHOT_PREVIEW_INTERVAL,
    DEFAULT_TB_URL,
    DOMAIN,
)
from .models import Hub

_LOGGER = logging.getLogger(__name__)

FIELD_BACK_TO_HUBS = "back_to_hubs"
STEP_RESELECT_DEVICES = "reselect_devices"
STEP_SNAPSHOT_SETTINGS = "snapshot_settings"


def _qr_data_uri(content: str) -> str:
    """Render ``content`` as a base64 PNG data URI (segno, pure-python)."""
    buff = io.BytesIO()
    segno.make(content, error="m").save(buff, kind="png", scale=4, border=2)
    b64 = base64.b64encode(buff.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _pairing_qr_content(user_code: str) -> str:
    """Return the Airych app QR payload for HA pairing approval."""
    return f"airych://oauth/device?v=1&scene=ha&user_code={quote(user_code)}"


def _as_list(value: Any) -> list[str]:
    """Normalize selector output to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _selection_from_entry(data: dict[str, Any], key: str) -> list[str] | None:
    """Return a stored selection, preserving the difference between unset/empty."""
    if key not in data:
        return None
    return _as_list(data.get(key))


def _default_selection(
    options: list[dict[str, str]], selected_ids: list[str] | None
) -> list[str]:
    """Return selected ids that still exist, or all options for new flows."""
    if selected_ids is None:
        return [option["value"] for option in options]
    allowed = {option["value"] for option in options}
    return [selected_id for selected_id in selected_ids if selected_id in allowed]


async def _async_fetch_hubs(rest: ThingsBoardRest) -> dict[str, Hub]:
    """Load VioStations and their client-scope attributes from ThingsBoard."""
    hubs: dict[str, Hub] = {}
    for dev in await rest.async_get_customer_hubs():
        hub_id = dev["id"]["id"] if isinstance(dev.get("id"), dict) else dev["id"]
        hub = Hub(hub_id, dev.get("name", hub_id))
        try:
            hub.update_attrs(await rest.async_get_client_attributes(hub_id))
        except AirychApiError as err:
            _LOGGER.warning(
                "Failed to load attributes for hub %s during setup: %s",
                hub_id,
                err,
            )
        hubs[hub_id] = hub
    return hubs


def _hub_selection_schema(
    available_hubs: dict[str, Hub],
    selected_hub_ids: list[str] | None,
) -> vol.Schema:
    """Build the dynamic VioStation selector schema."""
    hub_options = [
        {"value": hub_id, "label": hub.name}
        for hub_id, hub in available_hubs.items()
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_SELECTED_HUB_IDS,
                default=_default_selection(hub_options, selected_hub_ids),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=hub_options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _camera_options_for_selected_hubs(
    available_hubs: dict[str, Hub], selected_hub_ids: list[str]
) -> list[dict[str, str]]:
    """Return VioCam selector options for selected VioStations only."""
    return [
        {
            "value": camera_id,
            "label": f"{hub.name} / {hub.camera(camera_id).name}",
        }
        for hub_id in selected_hub_ids
        if (hub := available_hubs.get(hub_id)) is not None
        for camera_id in hub.camera_ids
    ]


def _camera_selection_schema(
    camera_options: list[dict[str, str]],
    selected_camera_ids: list[str] | None,
) -> vol.Schema:
    """Build the dynamic VioCam selector schema."""
    return vol.Schema(
        {
            vol.Optional(FIELD_BACK_TO_HUBS, default=False): bool,
            vol.Optional(
                CONF_SELECTED_CAMERA_IDS,
                default=_default_selection(camera_options, selected_camera_ids),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=camera_options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _snapshot_settings_schema(options: dict[str, Any]) -> vol.Schema:
    """Build the snapshot options schema."""
    try:
        default_interval = int(
            options.get(
                CONF_SNAPSHOT_PREVIEW_INTERVAL, DEFAULT_SNAPSHOT_PREVIEW_INTERVAL
            )
        )
    except (TypeError, ValueError):
        default_interval = DEFAULT_SNAPSHOT_PREVIEW_INTERVAL
    return vol.Schema(
        {
            vol.Required(
                CONF_SNAPSHOT_PREVIEW_INTERVAL,
                default=max(0, default_interval),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600))
        }
    )


class AirychCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Airych Cloud config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for an existing Airych Cloud entry."""
        return AirychCloudOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._backend_url: str = DEFAULT_BACKEND_URL
        self._backend: AirychBackendClient | None = None
        self._pair_session: str | None = None
        self._qr_uri: str = ""
        self._user_code: str = ""
        self._verification_uri: str = ""
        self._interval: int = DEFAULT_PAIR_INTERVAL
        self._timeout: int = DEFAULT_PAIR_TIMEOUT
        self._poll_task: asyncio.Task | None = None
        self._result: dict[str, Any] | None = None
        self._abort_reason: str | None = None
        self._reauth_entry: ConfigEntry | None = None
        self._available_hubs: dict[str, Hub] | None = None
        self._selected_hub_ids: list[str] | None = None
        self._selected_camera_ids: list[str] | None = None

    # ------------------------------------------------------------- user step
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Kick off pairing against the built-in backend address."""
        try:
            await self._async_start_pairing()
        except AirychApiError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_pair()

    async def _async_start_pairing(self) -> None:
        """Call pair/start and prepare the QR + poll task."""
        session = async_get_clientsession(self.hass)
        self._backend = AirychBackendClient(session, self._backend_url)
        ha_id = await instance_id.async_get(self.hass)
        data = await self._backend.pair_start("Home Assistant", ha_id)

        self._pair_session = data["device_code"]
        self._user_code = data.get("user_code", "")
        self._verification_uri = data.get("verification_uri") or self._backend_url
        self._interval = int(data.get("interval") or DEFAULT_PAIR_INTERVAL)
        self._timeout = int(data.get("expires_in") or DEFAULT_PAIR_TIMEOUT)
        qr_content = (
            _pairing_qr_content(self._user_code)
            if self._user_code
            else data.get("verification_uri_complete")
            or data.get("qr_content")
            or self._verification_uri
        )
        self._qr_uri = await self.hass.async_add_executor_job(_qr_data_uri, qr_content)
        self._poll_task = None
        self._result = None
        self._abort_reason = None

    # ------------------------------------------------------------- pair step
    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the QR and poll until the user approves in the app."""
        if self._poll_task is None:
            self._poll_task = self.hass.async_create_task(self._async_poll())

        if not self._poll_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pairing",
                description_placeholders={
                    "qr_code": self._qr_uri,
                    "user_code": self._user_code,
                    "verification_uri": self._verification_uri,
                },
                progress_task=self._poll_task,
            )

        try:
            self._result = self._poll_task.result()
        except PairExpired:
            self._abort_reason = "pair_expired"
        except PairDenied:
            self._abort_reason = "pair_denied"
        except (AirychApiError, asyncio.CancelledError):
            self._abort_reason = "cannot_connect"

        next_step = "finish" if self._reauth_entry is not None else "select_hubs"
        return self.async_show_progress_done(next_step_id=next_step)

    async def _async_poll(self) -> dict[str, Any]:
        """Poll the backend until the pairing resolves or times out."""
        assert self._backend and self._pair_session
        elapsed = 0
        while elapsed < self._timeout:
            try:
                return await self._backend.pair_poll(self._pair_session)
            except PairPending:
                await asyncio.sleep(self._interval)
                elapsed += self._interval
        raise PairExpired

    # ------------------------------------------------------ device selection
    async def async_step_select_hubs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose which VioStations should be imported."""
        if self._abort_reason:
            return self.async_abort(reason=self._abort_reason)

        assert self._result is not None
        try:
            await self._async_load_available_devices(self._result)
        except AirychAuthError:
            return self.async_abort(reason="cannot_connect")
        except AirychApiError:
            return self.async_abort(reason="cannot_connect")

        assert self._available_hubs is not None
        if not self._available_hubs:
            return self.async_abort(reason="no_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_hub_ids = _as_list(user_input.get(CONF_SELECTED_HUB_IDS))
            if not selected_hub_ids:
                errors["base"] = "no_hubs_selected"
            elif any(hub_id not in self._available_hubs for hub_id in selected_hub_ids):
                errors["base"] = "invalid_device_selection"
            else:
                self._selected_hub_ids = selected_hub_ids
                return await self.async_step_select_cameras()

        return self.async_show_form(
            step_id="select_hubs",
            data_schema=_hub_selection_schema(
                self._available_hubs, self._selected_hub_ids
            ),
            errors=errors,
            description_placeholders={
                "viostation_count": str(len(self._available_hubs)),
            },
        )

    async def async_step_select_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose VioCams under the selected VioStations."""
        if self._abort_reason:
            return self.async_abort(reason=self._abort_reason)

        assert self._available_hubs is not None
        assert self._selected_hub_ids is not None
        camera_options = _camera_options_for_selected_hubs(
            self._available_hubs, self._selected_hub_ids
        )
        if not camera_options:
            self._selected_camera_ids = []
            return await self.async_step_finish()

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(FIELD_BACK_TO_HUBS):
                return await self.async_step_select_hubs()

            selected_camera_ids = _as_list(user_input.get(CONF_SELECTED_CAMERA_IDS))
            allowed_camera_ids = {option["value"] for option in camera_options}
            if any(cam_id not in allowed_camera_ids for cam_id in selected_camera_ids):
                errors["base"] = "invalid_device_selection"
            else:
                self._selected_camera_ids = selected_camera_ids
                return await self.async_step_finish()

        return self.async_show_form(
            step_id="select_cameras",
            data_schema=_camera_selection_schema(
                camera_options, self._selected_camera_ids
            ),
            errors=errors,
            description_placeholders={
                "viostation_count": str(len(self._selected_hub_ids)),
                "camera_count": str(
                    sum(
                        len(self._available_hubs[hub_id].camera_ids)
                        for hub_id in self._selected_hub_ids
                        if hub_id in self._available_hubs
                    )
                ),
            },
        )

    async def _async_load_available_devices(self, result: dict[str, Any]) -> None:
        """Load hub/camera names with the freshly issued TB token."""
        if self._available_hubs is not None:
            return

        session = async_get_clientsession(self.hass)
        backend = AirychBackendClient(session, self._backend_url)
        auth = AirychAuth(
            backend,
            tb_url=result.get("tb_url") or DEFAULT_TB_URL,
            customer_id=result["customer_id"],
            app_access_token=result.get("access_token"),
            access_token=result["tb_access_token"],
            expires_at=float(result.get("tb_expires_at") or 0),
            plugin_refresh_token=result.get("plugin_refresh_token")
            or result["refresh_token"],
        )
        rest = ThingsBoardRest(session, auth)
        self._available_hubs = await _async_fetch_hubs(rest)

    # ----------------------------------------------------------- finish step
    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create (or update on reauth) the config entry."""
        if self._abort_reason:
            return self.async_abort(reason=self._abort_reason)

        assert self._result is not None
        data = self._build_entry_data(self._result)
        account_id = data[CONF_ACCOUNT_ID]

        if self._reauth_entry is not None:
            if self._reauth_entry.unique_id != account_id:
                return self.async_abort(reason="reauth_account_mismatch")
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=data)
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(account_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=data.get(CONF_ACCOUNT_NAME) or "Airych Cloud", data=data
        )

    def _build_entry_data(self, result: dict[str, Any]) -> dict[str, Any]:
        data = {
            CONF_BACKEND_URL: self._backend_url,
            CONF_ACCOUNT_ID: result["account_id"],
            CONF_ACCOUNT_NAME: result.get("account_name", ""),
            CONF_CUSTOMER_ID: result["customer_id"],
            CONF_TB_URL: result.get("tb_url") or DEFAULT_TB_URL,
            CONF_APP_ACCESS_TOKEN: result.get("access_token", ""),
            CONF_TB_ACCESS_TOKEN: result["tb_access_token"],
            CONF_TB_EXPIRES_AT: result.get("tb_expires_at", 0),
            CONF_PLUGIN_REFRESH_TOKEN: result.get("plugin_refresh_token")
            or result["refresh_token"],
            CONF_SIGNALING_URL: result.get("signaling_url", ""),
            CONF_ICE_SERVERS: result.get("ice_servers", []),
        }
        if self._selected_hub_ids is not None:
            data[CONF_SELECTED_HUB_IDS] = self._selected_hub_ids
        elif (
            self._reauth_entry is not None
            and CONF_SELECTED_HUB_IDS in self._reauth_entry.data
        ):
            data[CONF_SELECTED_HUB_IDS] = self._reauth_entry.data[
                CONF_SELECTED_HUB_IDS
            ]

        if self._selected_camera_ids is not None:
            data[CONF_SELECTED_CAMERA_IDS] = self._selected_camera_ids
        elif (
            self._reauth_entry is not None
            and CONF_SELECTED_CAMERA_IDS in self._reauth_entry.data
        ):
            data[CONF_SELECTED_CAMERA_IDS] = self._reauth_entry.data[
                CONF_SELECTED_CAMERA_IDS
            ]
        return data

    # ---------------------------------------------------------------- reauth
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry:
            self._backend_url = self._reauth_entry.data.get(
                CONF_BACKEND_URL, DEFAULT_BACKEND_URL
            )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            account_name = (
                self._reauth_entry.data.get(CONF_ACCOUNT_NAME, "")
                if self._reauth_entry
                else ""
            )
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={"account_name": account_name},
            )
        try:
            await self._async_start_pairing()
        except AirychApiError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_pair()


class AirychCloudOptionsFlow(OptionsFlow):
    """Handle post-pairing Airych Cloud configuration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._available_hubs: dict[str, Hub] | None = None
        self._selected_hub_ids = _selection_from_entry(
            config_entry.data, CONF_SELECTED_HUB_IDS
        )
        self._selected_camera_ids = _selection_from_entry(
            config_entry.data, CONF_SELECTED_CAMERA_IDS
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show available configuration actions."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[STEP_RESELECT_DEVICES, STEP_SNAPSHOT_SETTINGS],
        )

    async def async_step_snapshot_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure snapshot preview throttling."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self._entry.options,
                    CONF_SNAPSHOT_PREVIEW_INTERVAL: user_input[
                        CONF_SNAPSHOT_PREVIEW_INTERVAL
                    ],
                },
            )

        return self.async_show_form(
            step_id=STEP_SNAPSHOT_SETTINGS,
            data_schema=_snapshot_settings_schema(self._entry.options),
        )

    async def async_step_reselect_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the existing-entry device selection flow."""
        return await self.async_step_select_hubs()

    async def async_step_select_hubs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose which VioStations should be imported."""
        try:
            await self._async_load_available_devices()
        except (AirychAuthError, AirychApiError):
            return self.async_abort(reason="cannot_connect")

        assert self._available_hubs is not None
        if not self._available_hubs:
            return self.async_abort(reason="no_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_hub_ids = _as_list(user_input.get(CONF_SELECTED_HUB_IDS))
            if not selected_hub_ids:
                errors["base"] = "no_hubs_selected"
            elif any(hub_id not in self._available_hubs for hub_id in selected_hub_ids):
                errors["base"] = "invalid_device_selection"
            else:
                self._selected_hub_ids = selected_hub_ids
                return await self.async_step_select_cameras()

        return self.async_show_form(
            step_id="select_hubs",
            data_schema=_hub_selection_schema(
                self._available_hubs, self._selected_hub_ids
            ),
            errors=errors,
            description_placeholders={
                "viostation_count": str(len(self._available_hubs)),
            },
        )

    async def async_step_select_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose VioCams under the selected VioStations."""
        assert self._available_hubs is not None
        assert self._selected_hub_ids is not None

        camera_options = _camera_options_for_selected_hubs(
            self._available_hubs, self._selected_hub_ids
        )
        if not camera_options:
            self._selected_camera_ids = []
            return await self.async_step_save()

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(FIELD_BACK_TO_HUBS):
                return await self.async_step_select_hubs()

            selected_camera_ids = _as_list(user_input.get(CONF_SELECTED_CAMERA_IDS))
            allowed_camera_ids = {option["value"] for option in camera_options}
            if any(cam_id not in allowed_camera_ids for cam_id in selected_camera_ids):
                errors["base"] = "invalid_device_selection"
            else:
                self._selected_camera_ids = selected_camera_ids
                return await self.async_step_save()

        return self.async_show_form(
            step_id="select_cameras",
            data_schema=_camera_selection_schema(
                camera_options, self._selected_camera_ids
            ),
            errors=errors,
            description_placeholders={
                "viostation_count": str(len(self._selected_hub_ids)),
                "camera_count": str(
                    sum(
                        len(self._available_hubs[hub_id].camera_ids)
                        for hub_id in self._selected_hub_ids
                        if hub_id in self._available_hubs
                    )
                ),
            },
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist device selections and reload the integration."""
        assert self._selected_hub_ids is not None
        assert self._selected_camera_ids is not None

        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_SELECTED_HUB_IDS: self._selected_hub_ids,
                CONF_SELECTED_CAMERA_IDS: self._selected_camera_ids,
            },
        )
        await self.hass.config_entries.async_reload(self._entry.entry_id)
        self._async_remove_unselected_registry_entries()
        return self.async_create_entry(title="", data=dict(self._entry.options))

    async def _async_load_available_devices(self) -> None:
        """Load hub/camera names using the existing entry credentials."""
        if self._available_hubs is not None:
            return

        session = async_get_clientsession(self.hass)
        backend = AirychBackendClient(
            session, self._entry.data.get(CONF_BACKEND_URL, DEFAULT_BACKEND_URL)
        )

        async def _save_tokens(tokens: dict[str, Any]) -> None:
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    **self._entry.data,
                    CONF_APP_ACCESS_TOKEN: tokens.get("app_access_token")
                    or self._entry.data.get(CONF_APP_ACCESS_TOKEN, ""),
                    CONF_TB_ACCESS_TOKEN: tokens["tb_access_token"],
                    CONF_TB_EXPIRES_AT: tokens["tb_expires_at"],
                    CONF_PLUGIN_REFRESH_TOKEN: tokens["plugin_refresh_token"],
                },
            )

        auth = AirychAuth(
            backend,
            tb_url=self._entry.data.get(CONF_TB_URL, DEFAULT_TB_URL),
            customer_id=self._entry.data[CONF_CUSTOMER_ID],
            app_access_token=self._entry.data.get(CONF_APP_ACCESS_TOKEN),
            access_token=self._entry.data[CONF_TB_ACCESS_TOKEN],
            expires_at=float(self._entry.data.get(CONF_TB_EXPIRES_AT) or 0),
            plugin_refresh_token=self._entry.data[CONF_PLUGIN_REFRESH_TOKEN],
            on_tokens_updated=_save_tokens,
        )
        self._available_hubs = await _async_fetch_hubs(ThingsBoardRest(session, auth))

    @callback
    def _async_remove_unselected_registry_entries(self) -> None:
        """Remove stale HA entities/devices for devices no longer selected."""
        selected_hub_ids = set(self._selected_hub_ids or [])
        selected_camera_ids = set(self._selected_camera_ids or [])
        device_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        devices_to_remove = []

        for device in device_reg.devices.values():
            if self._entry.entry_id not in device.config_entries:
                continue
            should_remove = False
            sort_key = 1
            for domain, identifier in device.identifiers:
                if domain != DOMAIN:
                    continue
                if identifier.startswith("hub:"):
                    hub_id = identifier.split(":", 1)[1]
                    should_remove = hub_id not in selected_hub_ids
                    sort_key = 1
                    break
                if identifier.startswith("cam:"):
                    camera_id = identifier.split(":", 1)[1]
                    should_remove = camera_id not in selected_camera_ids
                    sort_key = 0
                    break
            if should_remove:
                devices_to_remove.append((sort_key, device.id))

        removed_entities = 0
        for _, device_id in sorted(devices_to_remove):
            for entity_entry in er.async_entries_for_device(
                entity_reg, device_id, include_disabled_entities=True
            ):
                if entity_entry.config_entry_id == self._entry.entry_id:
                    entity_reg.async_remove(entity_entry.entity_id)
                    removed_entities += 1
            device_reg.async_remove_device(device_id)

        if devices_to_remove:
            _LOGGER.debug(
                "Removed %s stale Airych device(s) and %s stale entity/entities",
                len(devices_to_remove),
                removed_entities,
            )
