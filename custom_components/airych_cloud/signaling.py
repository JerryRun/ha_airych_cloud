"""NBClient-compatible WebRTC signaling over the Airych signaling server."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

REGISTER_TIMEOUT = 10
REGISTER_CONNECT_ATTEMPTS = 3
PEER_TIMEOUT = 8
ANSWER_TIMEOUT = 25


class SignalingError(Exception):
    """Signaling exchange failed."""


def _finish_future(future: asyncio.Future, value: Any = None) -> None:
    if not future.done():
        future.set_result(value)


def _fail_future(future: asyncio.Future, err: Exception) -> None:
    if not future.done():
        future.set_exception(err)


class SignalingSession:
    """One HA browser <-> hub signaling session.

    The protocol mirrors NBClient's native ``webrtcsignal.c`` transport:
    register as ``HELLO <local_peerid>``, wait for ``HELLO``, then relay JSON
    ``{"sdp": ...}`` / ``{"ice": ...}`` frames. The hub connects to this peer
    after an ``openSessionPipeline`` RPC.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        signaling_url: str,
        peer_id: str,
        on_remote_ice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._url = signaling_url
        self._peer_id = peer_id
        self._on_remote_ice = on_remote_ice
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._connected_url = signaling_url
        self._reset_waiters()
        self._closed = False

    @property
    def peer_id(self) -> str:
        return self._peer_id

    @property
    def connected_url(self) -> str:
        """Return the concrete signaling URL that accepted registration."""
        return self._connected_url

    async def async_connect(self) -> None:
        """Connect to the signaling server and register our HA peer id."""
        if not self._url:
            raise SignalingError("signaling_url is not configured")
        candidates = await self._async_connection_candidates()
        last_err: Exception | None = None
        for attempt in range(1, REGISTER_CONNECT_ATTEMPTS + 1):
            for url, headers in candidates:
                try:
                    await self._async_connect_url(url, headers)
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    last_err = err
                    await self.async_close()
                    _LOGGER.debug(
                        "Signaling registration attempt %s/%s failed for %s: %s",
                        attempt,
                        REGISTER_CONNECT_ATTEMPTS,
                        url,
                        err,
                    )
            if attempt < REGISTER_CONNECT_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
        if last_err is not None:
            await self.async_close()
            raise SignalingError(f"signaling registration failed: {last_err}") from last_err

    async def _async_connect_url(
        self, url: str, headers: dict[str, str] | None
    ) -> None:
        self._closed = False
        self._reset_waiters()
        self._ws = await self._session.ws_connect(
            url,
            heartbeat=20,
            headers=headers,
        )
        self._reader = asyncio.create_task(self._read_loop())
        await self._send_text(f"HELLO {self._peer_id}")
        await asyncio.wait_for(self._registered, timeout=REGISTER_TIMEOUT)
        self._connected_url = url

    async def _async_connection_candidates(
        self,
    ) -> list[tuple[str, dict[str, str] | None]]:
        """Return the original URL plus explicit IP URLs for multi-A records."""
        candidates: list[tuple[str, dict[str, str] | None]] = []
        seen_urls: set[tuple[str, str]] = set()
        split = urlsplit(self._url)
        host = split.hostname
        if (
            not host
            or split.scheme not in ("ws", "wss")
            or _is_ip_address(host)
        ):
            return [(self._url, None)]

        port = split.port or (443 if split.scheme == "wss" else 80)
        schemes = [split.scheme]
        # The App backend may return wss:// for the FRP signaling port even when
        # the server itself is plain WebSocket. Keep the secure URL first, then
        # fall back to ws:// for this non-standard port.
        if split.scheme == "wss":
            schemes.append("ws")
        for scheme in schemes:
            url = urlunsplit(
                (scheme, split.netloc, split.path, split.query, split.fragment)
            )
            seen_urls.add((url, ""))
            candidates.append((url, None))

        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as err:
            _LOGGER.debug("Failed to resolve signaling host %s: %s", host, err)
            return candidates

        ip_schemes = list(reversed(schemes)) if split.scheme == "wss" else schemes
        for scheme in ip_schemes:
            seen_ips: set[str] = set()
            for info in infos:
                ip = info[4][0]
                if ip in seen_ips:
                    continue
                seen_ips.add(ip)
                ip_host = f"[{ip}]" if ":" in ip else ip
                url = urlunsplit(
                    (
                        scheme,
                        f"{ip_host}:{port}",
                        split.path,
                        split.query,
                        split.fragment,
                    )
                )
                key = (url, "")
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                candidates.append((url, None))
        return candidates

    def _reset_waiters(self) -> None:
        loop = asyncio.get_running_loop()
        self._registered = loop.create_future()
        self._peer_ready = loop.create_future()
        self._answer = loop.create_future()

    async def async_wait_peer_ready(self) -> None:
        """Wait briefly until the hub has connected to this HA peer."""
        try:
            await asyncio.wait_for(self._peer_ready, timeout=PEER_TIMEOUT)
        except asyncio.TimeoutError:
            # Some signaling servers do not notify the callee explicitly. Sending
            # the offer after the RPC has returned is still the best next step.
            _LOGGER.debug("No peer-ready signal for %s; sending offer anyway", self._peer_id)

    async def async_send_offer_and_wait_answer(self, offer_sdp: str) -> str:
        """Send the browser offer and wait for the hub answer."""
        await self.async_send_offer(offer_sdp)
        return await self.async_wait_answer()

    async def async_send_offer(self, offer_sdp: str) -> None:
        """Send the browser SDP offer to the hub."""
        _LOGGER.debug(
            "Signaling send offer: peer=%s %s", self._peer_id, _summarize_sdp(offer_sdp)
        )
        await self._send_json({"sdp": {"type": "offer", "sdp": offer_sdp}})

    async def async_wait_answer(self) -> str:
        """Wait for the hub SDP answer."""
        try:
            return await asyncio.wait_for(self._answer, timeout=ANSWER_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise SignalingError("timed out waiting for SDP answer") from err

    async def async_send_candidate(self, candidate: dict[str, Any]) -> None:
        """Forward one browser ICE candidate to the hub."""
        _LOGGER.debug(
            "Signaling send ICE: peer=%s candidate=%s",
            self._peer_id,
            _summarize_ice(candidate),
        )
        await self._send_json({"ice": candidate})

    async def async_close(self) -> None:
        """Close the signaling socket and reader task."""
        self._closed = True
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _send_text(self, text: str) -> None:
        if self._ws is None or self._ws.closed:
            raise SignalingError("signaling socket is not connected")
        await self._ws.send_str(text)

    async def _send_json(self, data: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise SignalingError("signaling socket is not connected")
        await self._ws.send_str(json.dumps(data, separators=(",", ":")))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                await self._handle_text(msg.data)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - surface errors to waiters
            self._fail_all(SignalingError(f"signaling read failed: {err}"))
        finally:
            if not self._closed:
                self._fail_all(SignalingError("signaling socket closed"))

    async def _handle_text(self, text: str) -> None:
        if text == "HELLO":
            _LOGGER.debug("Signaling registered: peer=%s", self._peer_id)
            _finish_future(self._registered, True)
            return
        if text in ("SESSION_OK", "OFFER_REQUEST") or text.startswith("SESSION "):
            _LOGGER.debug("Signaling peer ready: peer=%s message=%s", self._peer_id, text)
            _finish_future(self._peer_ready, True)
            return
        if text.startswith("ERROR"):
            self._fail_all(SignalingError(text))
            return

        try:
            data = json.loads(text)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON signaling message: %s", text)
            return

        if not isinstance(data, dict):
            return
        if (sdp := data.get("sdp")) and isinstance(sdp, dict):
            if sdp.get("type") == "answer":
                _LOGGER.debug(
                    "Signaling received answer: peer=%s %s",
                    self._peer_id,
                    _summarize_sdp(sdp.get("sdp", "")),
                )
                _finish_future(self._answer, sdp.get("sdp", ""))
            elif sdp.get("type") == "offer":
                _finish_future(self._peer_ready, True)
                _LOGGER.warning("Hub sent an SDP offer, but HA is configured as offerer")
            return
        if (ice := data.get("ice")) and isinstance(ice, dict):
            _LOGGER.debug(
                "Signaling received ICE: peer=%s candidate=%s",
                self._peer_id,
                _summarize_ice(ice),
            )
            if self._on_remote_ice:
                await self._on_remote_ice(ice)
            return
        if data.get("type") == "error":
            self._fail_all(SignalingError(data.get("message", "signaling error")))

    def _fail_all(self, err: Exception) -> None:
        _fail_future(self._registered, err)
        _fail_future(self._peer_ready, err)
        _fail_future(self._answer, err)


class SignalingClient:
    """Factory for NBClient-compatible signaling sessions."""

    def __init__(self, session: aiohttp.ClientSession, signaling_url: str) -> None:
        self._session = session
        self._url = signaling_url

    async def async_open(
        self,
        peer_id: str,
        on_remote_ice: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SignalingSession:
        signaling = SignalingSession(
            self._session, self._url, peer_id, on_remote_ice
        )
        await signaling.async_connect()
        return signaling


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


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
