"""Cloud API clients for Airych Cloud.

Three concerns live here:

* :class:`AirychBackendClient` — talks to the *App backend* (pairing, token
  refresh, unpair, optional WebRTC signaling proxy).
* :class:`AirychAuth` — holds the current ThingsBoard access token and refreshes
  it through the backend before it expires (backend-mediated refresh).
* :class:`ThingsBoardRest` / :class:`ThingsBoardWs` — talk to ThingsBoard
  directly using the access token (device list, attributes, RPC, live updates).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import (
    EP_CAMERA_SNAPSHOT,
    EP_PAIR_POLL,
    EP_PAIR_START,
    EP_TOKEN_REFRESH,
    EP_UNPAIR,
    EP_WEBRTC_CONFIG,
    EP_WEBRTC_OFFER,
    OAUTH_CLIENT_ID,
    OAUTH_DEVICE_GRANT,
    OAUTH_SCOPE,
    TB_RPC_TIMEOUT_MS,
    TB_SCOPE_CLIENT,
    TB_SCOPE_SERVER,
    TOKEN_REFRESH_MARGIN,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class AirychApiError(Exception):
    """Generic API error."""


class AirychAuthError(AirychApiError):
    """Authentication failed / token could not be refreshed (needs reauth)."""


class PairPending(AirychApiError):
    """Pairing not yet approved."""


class PairExpired(AirychApiError):
    """Pairing session expired."""


class PairDenied(AirychApiError):
    """Pairing was rejected in the app."""


# ---------------------------------------------------------------------------
# App backend
# ---------------------------------------------------------------------------
class AirychBackendClient:
    """Client for the Airych App backend."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")

    async def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            async with self._session.post(
                url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status >= 500:
                    raise AirychApiError(f"{path} -> HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AirychApiError(f"{path} request failed: {err}") from err
        if not isinstance(data, dict):
            raise AirychApiError(f"{path} returned non-object body")
        return data

    async def _post_form(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            async with self._session.post(
                url, data=payload, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status >= 500:
                    raise AirychApiError(f"{path} -> HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AirychApiError(f"{path} request failed: {err}") from err
        if not isinstance(data, dict):
            raise AirychApiError(f"{path} returned non-object body")
        return data

    async def _get_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                "GET",
                url,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 500:
                    raise AirychApiError(f"{path} -> HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AirychApiError(f"{path} request failed: {err}") from err
        if not isinstance(data, dict):
            raise AirychApiError(f"{path} returned non-object body")
        return data

    async def pair_start(self, client_name: str, ha_install_id: str) -> dict[str, Any]:
        """Begin an OAuth device-authorization session."""
        return await self._post_form(
            EP_PAIR_START,
            {"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE},
        )

    async def pair_poll(self, device_code: str) -> dict[str, Any]:
        """Poll an OAuth device-code grant.

        Returns the ``approved`` payload, or raises :class:`PairPending` /
        :class:`PairExpired` / :class:`PairDenied`.
        """
        data = await self._post_form(
            EP_PAIR_POLL,
            {
                "grant_type": OAUTH_DEVICE_GRANT,
                "device_code": device_code,
                "client_id": OAUTH_CLIENT_ID,
            },
        )
        if not data.get("error"):
            data["status"] = "approved"
            if data.get("refresh_token") and not data.get("plugin_refresh_token"):
                data["plugin_refresh_token"] = data["refresh_token"]
            return data
        error = data.get("error")
        if error == "authorization_pending":
            raise PairPending
        if error == "access_denied":
            raise PairDenied
        if error == "expired_token":
            raise PairExpired
        raise AirychApiError(data.get("error_description") or error)

    async def token_refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange the long-lived OAuth refresh token for fresh tokens."""
        data = await self._post_form(
            EP_TOKEN_REFRESH,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
        )
        if data.get("error"):
            raise AirychAuthError(data.get("error_description") or data["error"])
        if not data.get("tb_access_token"):
            raise AirychAuthError("refresh did not return a token")
        if data.get("refresh_token") and not data.get("plugin_refresh_token"):
            data["plugin_refresh_token"] = data["refresh_token"]
        if not data.get("plugin_refresh_token"):
            data["plugin_refresh_token"] = refresh_token
        return data

    async def unpair(self, refresh_token: str) -> None:
        """Revoke the OAuth refresh token server-side (best effort)."""
        try:
            await self._post_form(
                EP_UNPAIR,
                {
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": OAUTH_CLIENT_ID,
                },
            )
        except AirychApiError as err:
            _LOGGER.warning("Unpair call failed (ignored): %s", err)

    async def webrtc_offer(
        self, token: str, hub_id: str, camera_id: str, sdp_offer: str
    ) -> dict[str, Any]:
        """Optional: relay a WebRTC offer through the backend (see signaling.py)."""
        return await self._post_json(
            EP_WEBRTC_OFFER,
            {
                "token": token,
                "hub_id": hub_id,
                "camera_id": camera_id,
                "sdp_offer": sdp_offer,
            },
        )

    async def webrtc_config(
        self, app_access_token: str, device_name: str
    ) -> dict[str, Any]:
        """Load signal/STUN/TURN configuration from the App backend."""
        data = await self._get_json(
            EP_WEBRTC_CONFIG,
            {
                "appLocation": {"country": "US"},
                "deviceName": device_name,
            },
            headers={"token": app_access_token},
        )
        if data.get("code") not in (0, None):
            raise AirychApiError(data.get("msg") or "webrtcconfig failed")
        config = data.get("data")
        if not isinstance(config, dict):
            raise AirychApiError("webrtcconfig returned no data")
        return config

    async def request_camera_snapshot(
        self,
        app_access_token: str,
        device_id: str,
        camera_id: str,
        resolution: str = "low",
    ) -> dict[str, Any]:
        """Ask the backend to RPC the hub and prepare a cloud snapshot."""
        data = await self._post_json(
            EP_CAMERA_SNAPSHOT,
            {
                "deviceId": device_id,
                "cameraId": camera_id,
                "resolution": resolution,
            },
            headers={"token": app_access_token},
        )
        if data.get("code") not in (0, None):
            raise AirychApiError(data.get("msg") or "snapshot request failed")
        snapshot = data.get("data")
        if not isinstance(snapshot, dict):
            raise AirychApiError("snapshot request returned no data")
        return snapshot


# ---------------------------------------------------------------------------
# Auth / token lifecycle (backend-mediated refresh)
# ---------------------------------------------------------------------------
class AirychAuth:
    """Owns the TB access token and refreshes it through the backend."""

    def __init__(
        self,
        backend: AirychBackendClient,
        *,
        tb_url: str,
        customer_id: str,
        app_access_token: str | None = None,
        access_token: str,
        expires_at: float,
        plugin_refresh_token: str,
        on_tokens_updated: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.backend = backend
        self.tb_url = tb_url.rstrip("/")
        self.customer_id = customer_id
        self._app_access_token = app_access_token or ""
        self._access_token = access_token
        self._expires_at = expires_at
        self._plugin_refresh_token = plugin_refresh_token
        self._on_tokens_updated = on_tokens_updated
        self._lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def app_access_token(self) -> str:
        return self._app_access_token

    def _needs_refresh(self) -> bool:
        if not self._expires_at:
            return False
        # Refresh once we are within the margin of expiry.
        remaining = self._expires_at - time.time()
        # We don't know the original TTL here, so use an absolute 5 min floor
        # combined with the configured margin against a nominal 1h lifetime.
        return remaining <= max(300, TOKEN_REFRESH_MARGIN * 3600)

    async def async_get_token(self) -> str:
        """Return a valid access token, refreshing proactively if needed."""
        if self._needs_refresh():
            await self.async_refresh()
        return self._access_token

    async def async_get_app_token(self) -> str:
        """Return a valid App backend token, refreshing if it is missing/stale."""
        if not self._app_access_token or self._needs_refresh():
            await self.async_refresh()
        if not self._app_access_token:
            raise AirychAuthError("refresh did not return an app token")
        return self._app_access_token

    async def async_refresh(self) -> str:
        """Force a token refresh via the backend."""
        async with self._lock:
            data = await self.backend.token_refresh(self._plugin_refresh_token)
            self._app_access_token = data.get("access_token") or self._app_access_token
            self._access_token = data["tb_access_token"]
            self._expires_at = float(data.get("tb_expires_at") or 0)
            if data.get("plugin_refresh_token"):
                self._plugin_refresh_token = data["plugin_refresh_token"]
            if self._on_tokens_updated:
                await self._on_tokens_updated(
                    {
                        "tb_access_token": self._access_token,
                        "tb_expires_at": self._expires_at,
                        "plugin_refresh_token": self._plugin_refresh_token,
                        "app_access_token": self._app_access_token,
                    }
                )
            _LOGGER.debug("TB token refreshed (expires_at=%s)", self._expires_at)
            return self._access_token


# ---------------------------------------------------------------------------
# ThingsBoard REST
# ---------------------------------------------------------------------------
class ThingsBoardRest:
    """Minimal ThingsBoard REST client scoped to a customer's devices."""

    def __init__(self, session: aiohttp.ClientSession, auth: AirychAuth) -> None:
        self._session = session
        self._auth = auth

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, retry: bool = True
    ) -> Any:
        token = await self._auth.async_get_token()
        url = f"{self._auth.tb_url}{path}"
        headers = {"X-Authorization": f"Bearer {token}"}
        try:
            async with self._session.request(
                method, url, headers=headers, json=json, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403) and retry:
                    # Token may have just expired; refresh once and retry.
                    await self._auth.async_refresh()
                    return await self._request(method, path, json=json, retry=False)
                if resp.status >= 400:
                    body = await resp.text()
                    raise AirychApiError(f"TB {method} {path} -> {resp.status}: {body}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AirychApiError(f"TB {method} {path} failed: {err}") from err

    async def async_get_customer_hubs(self) -> list[dict[str, Any]]:
        """Return all devices assigned to the customer (each is a hub)."""
        hubs: list[dict[str, Any]] = []
        page = 0
        while True:
            path = (
                f"/api/customer/{self._auth.customer_id}/deviceInfos"
                f"?pageSize=100&page={page}&sortProperty=createdTime&sortOrder=DESC"
            )
            data = await self._request("GET", path)
            hubs.extend(data.get("data", []))
            if not data.get("hasNext"):
                break
            page += 1
        return hubs

    async def async_get_client_attributes(self, device_id: str) -> dict[str, Any]:
        """Return all CLIENT_SCOPE attributes for a device as {key: value}."""
        path = (
            f"/api/plugins/telemetry/DEVICE/{device_id}"
            f"/values/attributes/{TB_SCOPE_CLIENT}"
        )
        raw = await self._request("GET", path)
        # TB returns [{"key": k, "value": v, "lastUpdateTs": ...}, ...]
        return {item["key"]: item["value"] for item in raw or []}

    async def async_get_device_active(self, device_id: str) -> bool:
        """Return the TB device activity state (server-scope ``active``)."""
        path = (
            f"/api/plugins/telemetry/DEVICE/{device_id}"
            f"/values/attributes/{TB_SCOPE_SERVER}?keys=active"
        )
        raw = await self._request("GET", path)
        for item in raw or []:
            if item.get("key") == "active":
                return bool(item.get("value"))
        return False

    async def async_rpc(
        self, device_id: str, method: str, params: dict[str, Any]
    ) -> Any:
        """Send a two-way server-side RPC to a hub."""
        path = f"/api/rpc/twoway/{device_id}"
        body = {"method": method, "params": params, "timeout": TB_RPC_TIMEOUT_MS}
        return await self._request("POST", path, json=body)


# ---------------------------------------------------------------------------
# ThingsBoard WebSocket (attribute subscription)
# ---------------------------------------------------------------------------
class ThingsBoardWs:
    """Subscribes to CLIENT_SCOPE attribute updates for a set of hub devices.

    Pushes ``(device_id, {key: value})`` deltas to ``on_update``.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: AirychAuth,
        on_update: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._session = session
        self._auth = auth
        self._on_update = on_update
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._cmd_id = 0
        self._sub_to_device: dict[int, str] = {}
        self._device_ids: list[str] = []
        self._closing = False

    def set_devices(self, device_ids: list[str]) -> None:
        self._device_ids = list(device_ids)

    async def async_start(self) -> None:
        self._closing = False
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def async_stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _run(self) -> None:
        delay = 5
        while not self._closing:
            try:
                await self._connect_and_listen()
                delay = 5
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001 - keep the reconnect loop alive
                _LOGGER.warning("TB websocket error, reconnecting in %ss: %s", delay, err)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _connect_and_listen(self) -> None:
        token = await self._auth.async_get_token()
        ws_base = self._auth.tb_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        url = f"{ws_base}/api/ws/plugins/telemetry?token={token}"
        async with self._session.ws_connect(url, heartbeat=30) as ws:
            self._ws = ws
            self._subscribe_all()
            await self._send_subscriptions(ws)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        self._ws = None

    def _subscribe_all(self) -> None:
        self._sub_to_device.clear()

    async def _send_subscriptions(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        cmds = []
        for device_id in self._device_ids:
            self._cmd_id += 1
            self._sub_to_device[self._cmd_id] = device_id
            cmds.append(
                {
                    "entityType": "DEVICE",
                    "entityId": device_id,
                    "scope": TB_SCOPE_CLIENT,
                    "cmdId": self._cmd_id,
                    # No "keys" -> subscribe to all CLIENT_SCOPE attributes so new
                    # camera_{id}_* keys are picked up automatically.
                }
            )
        await ws.send_json({"attrSubCmds": cmds, "tsSubCmds": [], "historyCmds": []})

    def _handle_message(self, data: dict[str, Any]) -> None:
        sub_id = data.get("subscriptionId")
        device_id = self._sub_to_device.get(sub_id)
        payload = data.get("data") or {}
        if not device_id or not payload:
            return
        # payload is {key: [[ts, value], ...]}; take the latest value per key.
        delta: dict[str, Any] = {}
        for key, points in payload.items():
            if points:
                delta[key] = points[-1][1]
        if delta:
            self._on_update(device_id, delta)
