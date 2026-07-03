"""Camera entities with native WebRTC live preview.

HA frontend (browser) is the offerer; the hub (GStreamer ``webrtcbin``) is the
answerer. This entity relays the SDP offer to the hub through the custom
signaling server and returns the answer. STUN/TURN servers (fixed credentials)
are advertised to the browser via the WebRTC client configuration.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from typing import Any
from urllib.parse import unquote

import aiohttp
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import AirychApiError
from .const import (
    CONF_ICE_SERVERS,
    CONF_SNAPSHOT_PREVIEW_INTERVAL,
    CONF_SIGNALING_URL,
    DEFAULT_SNAPSHOT_PREVIEW_INTERVAL,
    DOMAIN,
    SIGNAL_CAMERA_SNAPSHOT,
    SIGNAL_NEW_CAMERA,
)
from .coordinator import AirychCoordinator
from .entity import AirychCameraBaseEntity
from .signaling import SignalingClient, SignalingError, SignalingSession

_LOGGER = logging.getLogger(__name__)

try:  # webrtc_models is a Home Assistant dependency (2024.11+)
    from webrtc_models import RTCIceCandidateInit, RTCIceServer
except ImportError:  # pragma: no cover - defensive
    RTCIceCandidateInit = None  # type: ignore[assignment, misc]
    RTCIceServer = None  # type: ignore[assignment, misc]

RTC_PEER_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
SNAPSHOT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)
SNAPSHOT_READY_TIMEOUT = 8.0
SNAPSHOT_READY_INTERVAL = 0.5
SNAPSHOT_MAX_BYTES = 5 * 1024 * 1024
PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfab0d0000000049454e44ae426082"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities for existing and future cameras."""
    coordinator: AirychCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        AirychCamera(coordinator, hub_id, camera_id)
        for hub_id, hub in coordinator.hubs.items()
        for camera_id in coordinator.camera_ids_for_hub(hub)
    ]
    async_add_entities(entities)

    @callback
    def _add_camera(hub_id: str, camera_id: str) -> None:
        async_add_entities([AirychCamera(coordinator, hub_id, camera_id)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_CAMERA, _add_camera)
    )


class AirychCamera(AirychCameraBaseEntity, Camera):
    """Live WebRTC preview for an Airych camera."""

    _attr_name = None  # the camera is the primary feature of the device
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, coordinator: AirychCoordinator, hub_id: str, camera_id: str
    ) -> None:
        AirychCameraBaseEntity.__init__(self, coordinator, hub_id, camera_id, "camera")
        Camera.__init__(self)
        self.content_type = "image/png"
        self._webrtc_sessions: dict[str, SignalingSession] = {}
        self._pending_ice_candidates: dict[str, list[dict[str, Any]]] = {}
        self._ice_ready_sessions: set[str] = set()
        self._hub_pipeline_ids: dict[str, int] = {}
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_cache: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Register listeners after the entity is added."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CAMERA_SNAPSHOT,
                self._handle_snapshot_response,
            )
        )

    @callback
    def _handle_snapshot_response(
        self, hub_id: str, camera_id: str, snapshot: dict[str, Any]
    ) -> None:
        """Refresh the camera still preview from an externally requested snapshot."""
        if hub_id != self._hub_id or camera_id != self._camera_id:
            return
        self.hass.async_create_task(self._async_refresh_snapshot_from_response(snapshot))

    def _signaling(self, signaling_url: str) -> SignalingClient:
        return SignalingClient(
            async_get_clientsession(self.hass),
            signaling_url,
        )

    @property
    def is_streaming(self) -> bool:
        """Report a usable VioCam as active so HA enables snapshot download."""
        return self.camera_model is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the latest snapshot marker timestamp for visible state changes."""
        if self._snapshot_cache is None:
            return {}
        return {"last_snapshot_ts": self._snapshot_cache.get("ts")}

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Relay the browser SDP offer to the hub and return its answer."""
        await self._async_close_session(session_id)
        self._pending_ice_candidates[session_id] = []
        self._ice_ready_sessions.discard(session_id)
        signaling_session: SignalingSession | None = None
        try:
            h264_payload = _choose_h264_payload_type(offer_sdp)
            opus_payload = _choose_codec_payload_type(offer_sdp, "opus", 111)
            _LOGGER.debug(
                "WebRTC offer received: hub=%s camera=%s session=%s offer_len=%s h264_pt=%s opus_pt=%s",
                self._hub_id,
                self._camera_id,
                session_id,
                len(offer_sdp),
                h264_payload,
                opus_payload,
            )
            config = await self.coordinator.async_get_webrtc_config(self._hub_id)
            signaling_url = (
                config.get("signalServer")
                or self.coordinator.entry.data.get(CONF_SIGNALING_URL, "")
            )
            peer_id = self._make_peer_id(session_id)
            remote_ice_queue: list[dict[str, Any]] = []
            answer_sent = False

            def _send_remote_ice(ice: dict[str, Any]) -> None:
                if RTCIceCandidateInit is None:
                    return
                candidate_text = ice.get("candidate")
                if not candidate_text:
                    return
                send_message(
                    WebRTCCandidate(
                        RTCIceCandidateInit(
                            candidate_text,
                            sdp_mid=ice.get("sdpMid"),
                            sdp_m_line_index=ice.get("sdpMLineIndex"),
                        )
                    )
                )

            async def _forward_remote_ice(ice: dict[str, Any]) -> None:
                if not answer_sent:
                    remote_ice_queue.append(ice)
                    _LOGGER.debug(
                        "Queued remote ICE before answer: hub=%s camera=%s session=%s candidate=%s",
                        self._hub_id,
                        self._camera_id,
                        session_id,
                        _summarize_ice(ice),
                    )
                    return
                _LOGGER.debug(
                    "Forwarding remote ICE: hub=%s camera=%s session=%s candidate=%s",
                    self._hub_id,
                    self._camera_id,
                    session_id,
                    _summarize_ice(ice),
                )
                _send_remote_ice(ice)

            signaling_session = await self._signaling(signaling_url).async_open(
                peer_id, _forward_remote_ice
            )
            self._webrtc_sessions[session_id] = signaling_session
            pending_ice = self._pending_ice_candidates.get(session_id, [])
            _LOGGER.debug(
                "WebRTC signaling registered: hub=%s camera=%s session=%s peer=%s url=%s pending_ice=%s",
                self._hub_id,
                self._camera_id,
                session_id,
                peer_id,
                signaling_session.connected_url,
                len(pending_ice),
            )
            if pplid := await self._async_open_hub_webrtc_session(
                peer_id,
                config,
                signaling_session.connected_url,
                h264_payload,
                opus_payload,
            ):
                self._hub_pipeline_ids[session_id] = pplid
            _LOGGER.debug(
                "Waiting for hub WebRTC answer: hub=%s camera=%s session=%s peer=%s",
                self._hub_id,
                self._camera_id,
                session_id,
                peer_id,
            )
            await signaling_session.async_wait_peer_ready()
            await signaling_session.async_send_offer(offer_sdp)
            self._ice_ready_sessions.add(session_id)
            pending_ice = self._pending_ice_candidates.pop(session_id, [])
            if pending_ice:
                _LOGGER.debug(
                    "Flushing queued ICE after offer: hub=%s camera=%s session=%s count=%s",
                    self._hub_id,
                    self._camera_id,
                    session_id,
                    len(pending_ice),
                )
            for ice in pending_ice:
                await signaling_session.async_send_candidate(ice)
            answer = await signaling_session.async_wait_answer()
        except SignalingError as err:
            _LOGGER.error("WebRTC offer failed for %s: %s", self._camera_id, err)
            send_message(WebRTCError("webrtc_offer_failed", str(err)))
            await self._async_close_session(session_id)
            return
        except Exception as err:  # noqa: BLE001 - keep HA UI error clean
            _LOGGER.exception("WebRTC setup failed for %s", self._camera_id)
            send_message(WebRTCError("webrtc_offer_failed", str(err)))
            await self._async_close_session(session_id)
            return
        _LOGGER.debug(
            "WebRTC answer received: hub=%s camera=%s session=%s answer_len=%s",
            self._hub_id,
            self._camera_id,
            session_id,
            len(answer),
        )
        _LOGGER.debug(
            "WebRTC answer summary: hub=%s camera=%s session=%s %s",
            self._hub_id,
            self._camera_id,
            session_id,
            _summarize_sdp(answer),
        )
        send_message(WebRTCAnswer(answer))
        answer_sent = True
        if remote_ice_queue:
            _LOGGER.debug(
                "Flushing remote ICE after answer: hub=%s camera=%s session=%s count=%s",
                self._hub_id,
                self._camera_id,
                session_id,
                len(remote_ice_queue),
            )
        for ice in remote_ice_queue:
            _send_remote_ice(ice)

    async def async_on_webrtc_candidate(self, session_id: str, candidate) -> None:
        """Trickle ICE from the browser to the hub.

        HA passes ``RTCIceCandidateInit``. The hub expects NBClient's
        ``{"ice":{"candidate":"...","sdpMLineIndex":0}}`` envelope.
        """
        session = self._webrtc_sessions.get(session_id)
        if session is None or session_id not in self._ice_ready_sessions:
            pending = self._pending_ice_candidates.get(session_id)
            if pending is None:
                _LOGGER.debug("Ignoring ICE for closed WebRTC session %s", session_id)
                return
            pending.append(_candidate_to_airych_ice(candidate))
            _LOGGER.debug(
                "Queued early ICE for WebRTC session %s (pending=%s candidate=%s)",
                session_id,
                len(pending),
                _summarize_ice(pending[-1]),
            )
            return
        ice = _candidate_to_airych_ice(candidate)
        _LOGGER.debug(
            "Forwarding browser ICE for WebRTC session %s candidate=%s",
            session_id,
            _summarize_ice(ice),
        )
        await session.async_send_candidate(ice)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Tear down a WebRTC signaling session."""
        _LOGGER.debug("Closing WebRTC session %s", session_id)
        self.hass.async_create_task(self._async_close_session(session_id))

    @callback
    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        """Advertise our own STUN/TURN servers to the browser."""
        config = super()._async_get_webrtc_client_configuration()
        if RTCIceServer is not None:
            raw_servers = list(self.coordinator.entry.data.get(CONF_ICE_SERVERS, []))
            raw_servers.extend(
                _ice_servers_from_webrtc_config(
                    self.coordinator.webrtc_config_for_hub(self._hub_id)
                )
            )
            for srv in raw_servers:
                config.configuration.ice_servers.append(
                    RTCIceServer(
                        urls=srv.get("urls"),
                        username=srv.get("username"),
                        credential=srv.get("credential"),
                    )
                )
            _LOGGER.debug(
                "WebRTC client config built: hub=%s camera=%s ice_servers=%s",
                self._hub_id,
                self._camera_id,
                len(config.configuration.ice_servers),
            )
        return config

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image for HA camera previews and camera.snapshot."""
        is_preview_refresh = width is not None or height is not None
        if is_preview_refresh and (
            cached := self._fresh_preview_snapshot_cache()
        ) is not None:
            self.content_type = cached["content_type"]
            return cached["body"]

        async with self._snapshot_lock:
            if is_preview_refresh and (
                cached := self._fresh_preview_snapshot_cache()
            ) is not None:
                self.content_type = cached["content_type"]
                return cached["body"]

            image = await self._async_fetch_fresh_camera_image()
            if image is not None:
                return image

        if is_preview_refresh and self._snapshot_cache is not None:
            self.content_type = self._snapshot_cache["content_type"]
            return self._snapshot_cache["body"]

        self.content_type = "image/png"
        return PLACEHOLDER_PNG

    def _fresh_preview_snapshot_cache(self) -> dict[str, Any] | None:
        """Return cached preview image while inside the configured preview TTL."""
        cache = self._snapshot_cache
        if cache is None:
            return None
        ttl = _snapshot_preview_interval(self.coordinator.entry.options)
        if ttl <= 0:
            return None
        age = time.monotonic() - cache["fetched_at"]
        if age > ttl:
            return None
        _LOGGER.debug(
            "Snapshot preview cache hit: hub=%s camera=%s age=%.1fs ttl=%ss ts=%s",
            self._hub_id,
            self._camera_id,
            age,
            ttl,
            cache.get("ts"),
        )
        return cache

    async def _async_refresh_snapshot_from_response(
        self, snapshot: dict[str, Any]
    ) -> None:
        """Update the preview cache from a snapshot response requested elsewhere."""
        async with self._snapshot_lock:
            image = await self._async_fetch_fresh_camera_image(snapshot)
        if image is not None:
            self.async_write_ha_state()

    async def _async_fetch_fresh_camera_image(
        self, snapshot: dict[str, Any] | None = None
    ) -> bytes | None:
        """Request a fresh backend/hub snapshot and update the local cache."""
        try:
            if snapshot is None:
                snapshot = await self.coordinator.async_request_camera_snapshot(
                    self._hub_id, self._camera_id
                )
            requested_ts = _snapshot_ts(snapshot.get("ts"))
            image_url = snapshot.get("imageUrl")
            ts_url = snapshot.get("tsUrl")
            if requested_ts is None or not image_url or not ts_url:
                raise ValueError(f"snapshot response missing fields: {snapshot}")

            ready_ts = await _async_wait_snapshot_ready(
                async_get_clientsession(self.hass), ts_url, requested_ts
            )
            image = await _async_fetch_snapshot_image(
                async_get_clientsession(self.hass),
                _url_with_cache_buster(image_url, ready_ts),
            )
            self.content_type = image["content_type"]
            self._snapshot_cache = {
                "body": image["body"],
                "content_type": image["content_type"],
                "fetched_at": time.monotonic(),
                "ts": ready_ts,
            }
            _LOGGER.debug(
                "Snapshot fetched: hub=%s camera=%s ts=%s content_type=%s bytes=%s",
                self._hub_id,
                self._camera_id,
                ready_ts,
                self.content_type,
                len(image["body"]),
            )
            return image["body"]
        except (
            AirychApiError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ) as err:
            _LOGGER.warning(
                "Snapshot request failed: hub=%s camera=%s err=%s",
                self._hub_id,
                self._camera_id,
                err,
            )
        return None

    async def _async_close_session(self, session_id: str) -> None:
        self._pending_ice_candidates.pop(session_id, None)
        self._ice_ready_sessions.discard(session_id)
        if pplid := self._hub_pipeline_ids.pop(session_id, None):
            try:
                _LOGGER.debug(
                    "Closing hub WebRTC pipeline: hub=%s camera=%s session=%s pplid=%s",
                    self._hub_id,
                    self._camera_id,
                    session_id,
                    pplid,
                )
                await self.coordinator.rest.async_rpc(
                    self._hub_id,
                    "openSessionPipeline",
                    {"method": "close_pipeline", "pplid": pplid},
                )
            except Exception as err:  # noqa: BLE001 - best-effort cleanup
                _LOGGER.debug(
                    "Failed to close hub WebRTC pipeline: hub=%s camera=%s session=%s pplid=%s err=%s",
                    self._hub_id,
                    self._camera_id,
                    session_id,
                    pplid,
                    err,
                )
        if session := self._webrtc_sessions.pop(session_id, None):
            _LOGGER.debug("Closing signaling socket for WebRTC session %s", session_id)
            await session.async_close()

    def _make_peer_id(self, session_id: str) -> str:
        base = f"ha_{int(time.time() * 1000)}_{self._camera_id}_{session_id}"
        return RTC_PEER_SAFE.sub("_", base)[:120]

    async def _async_open_hub_webrtc_session(
        self,
        peer_id: str,
        config: dict[str, Any],
        signal_server: str,
        h264_payload: int,
        opus_payload: int,
    ) -> int | None:
        hub = self.coordinator.hubs[self._hub_id]
        camera = hub.camera(self._camera_id)
        camera_ip = camera.ip_address
        if not camera_ip:
            raise SignalingError(f"camera {self._camera_id} has no ip_address")
        live_host = _validated_camera_live_host(camera_ip)

        params: dict[str, Any] = {
            "method": "open_pipeline",
            "webrtc": "session",
            "remote_peerid": peer_id,
            "createoffer": False,
            "enckey": self._camera_id,
            "ppl_description": _build_hub_live_webrtc_pipeline(
                live_host, h264_payload, opus_payload
            ),
        }
        if signal_server:
            params["signal_server"] = signal_server
        if stun_server := config.get("stunServer"):
            params["stun_server"] = stun_server
        if turn_server := config.get("turnServer"):
            params["turn_server"] = turn_server
        if turn_servers := config.get("turnServers"):
            params["turn_server_arrobj"] = turn_servers

        _LOGGER.debug(
            "Sending openSessionPipeline RPC: hub=%s camera=%s peer=%s signal=%s camera_ip=%s h264_pt=%s opus_pt=%s",
            self._hub_id,
            self._camera_id,
            peer_id,
            signal_server,
            live_host,
            h264_payload,
            opus_payload,
        )
        result = await self.coordinator.rest.async_rpc(
            self._hub_id, "openSessionPipeline", params
        )
        _LOGGER.debug(
            "openSessionPipeline RPC returned: hub=%s camera=%s result=%s",
            self._hub_id,
            self._camera_id,
            result,
        )
        if isinstance(result, dict):
            try:
                return int(result["pplid"])
            except (KeyError, TypeError, ValueError):
                return None
        return None


def _candidate_to_airych_ice(candidate) -> dict[str, Any]:
    """Convert HA/webrtc_models ICE candidate objects to the hub JSON shape."""
    if hasattr(candidate, "to_dict"):
        data = candidate.to_dict()
    elif isinstance(candidate, dict):
        data = candidate
    else:
        data = {
            "candidate": getattr(candidate, "candidate", ""),
            "sdpMid": getattr(candidate, "sdp_mid", None),
            "sdpMLineIndex": getattr(candidate, "sdp_m_line_index", None),
        }
    ice = {
        "candidate": data.get("candidate", ""),
        "sdpMLineIndex": data.get("sdpMLineIndex")
        if data.get("sdpMLineIndex") is not None
        else data.get("sdp_m_line_index", 0),
    }
    if data.get("sdpMid") or data.get("sdp_mid"):
        ice["sdpMid"] = data.get("sdpMid") or data.get("sdp_mid")
    return ice


def _summarize_ice(ice: dict[str, Any]) -> str:
    candidate = ice.get("candidate") or ""
    parts = candidate.split()
    cand_type = "unknown"
    address = ""
    port = ""
    if "typ" in parts:
        idx = parts.index("typ")
        if idx + 1 < len(parts):
            cand_type = parts[idx + 1]
    if len(parts) >= 6:
        address = parts[4]
        port = parts[5]
    return (
        f"type={cand_type} mid={ice.get('sdpMid')} "
        f"mline={ice.get('sdpMLineIndex')} addr={address}:{port}"
    )


def _summarize_sdp(sdp: str) -> str:
    media: list[str] = []
    directions: list[str] = []
    codecs: list[str] = []
    candidates = 0
    for line in sdp.splitlines():
        if line.startswith("m="):
            media.append(line)
        elif line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
            directions.append(line[2:])
        elif line.startswith("a=rtpmap:"):
            codecs.append(line[9:])
        elif line.startswith("a=candidate:"):
            candidates += 1
    return (
        f"media={media} directions={directions} "
        f"codecs={codecs} candidates={candidates}"
    )


def _choose_h264_payload_type(offer_sdp: str) -> int:
    """Pick the browser's H.264 payload type so GStreamer does not answer VP8."""
    h264_payloads: list[int] = []
    fmtp: dict[int, str] = {}
    for line in offer_sdp.splitlines():
        if line.startswith("a=rtpmap:"):
            payload, _, codec = line[9:].partition(" ")
            try:
                payload_type = int(payload)
            except ValueError:
                continue
            if codec.lower().startswith("h264/"):
                h264_payloads.append(payload_type)
        elif line.startswith("a=fmtp:"):
            payload, _, params = line[7:].partition(" ")
            try:
                fmtp[int(payload)] = params.lower()
            except ValueError:
                continue

    if not h264_payloads:
        _LOGGER.debug("Browser offer has no H.264 payload type; falling back to 96")
        return 96

    def _score(payload_type: int) -> int:
        params = fmtp.get(payload_type, "")
        score = 0
        if "packetization-mode=1" in params:
            score += 100
        if "profile-level-id=42" in params:
            score += 20
        if "level-asymmetry-allowed=1" in params:
            score += 5
        return score

    payload_type = max(h264_payloads, key=_score)
    _LOGGER.debug(
        "Selected browser H.264 payload type %s from %s",
        payload_type,
        h264_payloads,
    )
    return payload_type


def _choose_codec_payload_type(
    offer_sdp: str, codec_name: str, fallback: int
) -> int:
    """Pick the browser payload type for a non-video codec."""
    prefix = f"{codec_name.lower()}/"
    for line in offer_sdp.splitlines():
        if not line.startswith("a=rtpmap:"):
            continue
        payload, _, codec = line[9:].partition(" ")
        if codec.lower().startswith(prefix):
            try:
                return int(payload)
            except ValueError:
                continue
    _LOGGER.debug(
        "Browser offer has no %s payload type; falling back to %s",
        codec_name,
        fallback,
    )
    return fallback


def _build_hub_live_webrtc_pipeline(
    camera_host: str, h264_payload: int, opus_payload: int
) -> str:
    """Build the hub-side GStreamer pipeline that publishes live media to WebRTC."""
    live_url = f"http://{camera_host}:9998/live/1?mux=fmp4&audio=opus"
    return (
        "webrtcbin name=webrtcbin "
        f'souphttpsrc is-live=true location="{live_url}" ! '
        "qtdemux name=demux "
        "demux.video_0 ! queue ! h264parse ! identity name=identity ! "
        f"rtph264pay config-interval=-1 pt={h264_payload} ! "
        "application/x-rtp,media=video,encoding-name=H264,"
        f"payload={h264_payload},clock-rate=90000 ! "
        "webrtcbin. "
        "demux.audio_0 ! queue ! opusparse ! "
        f"rtpopuspay pt={opus_payload} ! "
        "application/x-rtp,media=audio,encoding-name=OPUS,"
        f"payload={opus_payload},clock-rate=48000 ! webrtcbin."
    )


def _snapshot_content_type(
    declared_content_type: str | None, image: bytes
) -> str | None:
    """Return a HA camera content type for known snapshot image payloads."""
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if declared_content_type and declared_content_type.startswith("image/"):
        return declared_content_type
    return None


async def _async_wait_snapshot_ready(
    session: aiohttp.ClientSession, ts_url: str, requested_ts: int
) -> int:
    """Poll the cloud marker until it proves the requested snapshot is uploaded."""
    deadline = time.monotonic() + SNAPSHOT_READY_TIMEOUT
    last_ts: int | None = None
    last_status: int | None = None
    while True:
        marker_url = _url_with_cache_buster(ts_url, int(time.time() * 1000))
        try:
            async with session.get(
                marker_url,
                headers={"Cache-Control": "no-cache"},
                timeout=SNAPSHOT_HTTP_TIMEOUT,
            ) as resp:
                body = await resp.text()
                last_status = resp.status
                if resp.status == 200:
                    last_ts = _snapshot_ts(body)
                    if last_ts is not None and last_ts >= requested_ts:
                        return last_ts
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if time.monotonic() >= deadline:
                raise

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "snapshot marker not ready: "
                f"requested_ts={requested_ts} last_ts={last_ts} status={last_status}"
            )
        await asyncio.sleep(min(SNAPSHOT_READY_INTERVAL, remaining))


async def _async_fetch_snapshot_image(
    session: aiohttp.ClientSession, image_url: str
) -> dict[str, Any]:
    """Download and validate the prepared cloud snapshot image."""
    async with session.get(
        image_url,
        headers={"Cache-Control": "no-cache"},
        timeout=SNAPSHOT_HTTP_TIMEOUT,
    ) as resp:
        body = await _async_read_limited(resp)
        if resp.status != 200:
            raise ValueError(
                f"snapshot image HTTP {resp.status}: "
                f"{body[:200].decode(errors='replace')}"
            )
        content_type = _snapshot_content_type(resp.content_type, body)
        if not content_type:
            raise ValueError(
                f"snapshot image has invalid content-type {resp.content_type}: "
                f"{body[:200].decode(errors='replace')}"
            )
        return {"body": body, "content_type": content_type}


def _snapshot_ts(value: Any) -> int | None:
    """Parse the 13-digit snapshot timestamp returned by RPC / marker file."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d{10,}", value)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
    return None


async def _async_read_limited(resp: aiohttp.ClientResponse) -> bytes:
    """Read a response body while enforcing the snapshot size cap."""
    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > SNAPSHOT_MAX_BYTES:
            raise ValueError(
                f"snapshot image exceeds {SNAPSHOT_MAX_BYTES // (1024 * 1024)}MB"
            )
    return bytes(body)


def _validated_camera_live_host(camera_ip: str) -> str:
    """Validate camera host before embedding it into a hub-side pipeline."""
    try:
        parsed = ipaddress.ip_address(camera_ip.strip())
    except ValueError as err:
        raise SignalingError(f"camera ip_address is invalid: {camera_ip!r}") from err
    if parsed.version == 6:
        return f"[{parsed.compressed}]"
    return parsed.compressed


def _url_with_cache_buster(url: str, version: int) -> str:
    """Append a version query parameter without disturbing existing queries."""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version}"


def _snapshot_preview_interval(options: dict[str, Any]) -> int:
    """Return the configured automatic preview refresh minimum interval."""
    try:
        value = int(
            options.get(
                CONF_SNAPSHOT_PREVIEW_INTERVAL, DEFAULT_SNAPSHOT_PREVIEW_INTERVAL
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_SNAPSHOT_PREVIEW_INTERVAL
    return max(0, value)


def _ice_servers_from_webrtc_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert backend webrtcconfig into browser RTCIceServer dictionaries."""
    servers: list[dict[str, Any]] = []
    if stun_server := config.get("stunServer"):
        if srv := _ice_server_from_airych_url(stun_server):
            servers.append(srv)
    for item in config.get("turnServers") or []:
        if isinstance(item, dict) and (turn := item.get("turn_server")):
            if srv := _ice_server_from_airych_url(turn):
                servers.append(srv)
    if turn_server := config.get("turnServer"):
        if srv := _ice_server_from_airych_url(turn_server):
            servers.append(srv)
    return servers


def _ice_server_from_airych_url(url: str) -> dict[str, Any] | None:
    """Convert ``turn://user:pass@host`` into WebRTC's RTCIceServer shape."""
    if "://" not in url:
        return {"urls": url}
    scheme, rest = url.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("stun", "stuns", "turn", "turns"):
        return None
    userinfo = ""
    hostport = rest
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
    server: dict[str, Any] = {"urls": f"{scheme}:{hostport}"}
    if scheme.startswith("turn") and userinfo:
        username, _, credential = userinfo.partition(":")
        if username:
            server["username"] = unquote(username)
        if credential:
            server["credential"] = unquote(credential)
    return server
