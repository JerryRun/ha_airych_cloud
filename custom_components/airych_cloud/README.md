# Airych Cloud (Home Assistant)

Cloud-connected Home Assistant integration for Airych devices. It runs in a
customer-installed Home Assistant and talks to the Airych cloud
(ThingsBoard) — see [`DESIGN.md`](./DESIGN.md) for the full specification.

## How it works
- **Pairing**: scan a QR code (device-authorization grant) in the Airych app; the
  App backend returns a ThingsBoard access token plus a long-lived refresh token.
- **Data**: the integration uses the TB token directly — REST for the device list,
  WebSocket for `CLIENT_SCOPE` attribute pushes (online / person / door / window /
  fall / smoke-fire / recording, battery, signal …).
- **Model**: each hub is a HA device; each camera is a HA device under its hub.
- **Preview**: native WebRTC; the integration relays SDP between the HA frontend
  and the hub (GStreamer `webrtcbin`) over a custom signaling WebSocket server,
  advertising your own STUN/TURN servers.
- **Snapshot**: HA camera snapshots ask the App backend to send a TB RPC
  (`getCameraSnapshot`) to the hub. The hub writes the JPEG and `.ts` marker to
  the cloud file service; HA polls the marker and then downloads the image.
  Automatic preview refreshes are locally throttled (default 60 seconds), while
  manual snapshot downloads always request a fresh image.
- **Actions**: HA services expose explicit user intents such as playing
  VioStation/VioCam alarms, camera recording, cloud snapshots, and mobile
  notifications. Snapshot capture is implemented; the other action handlers are
  registered as placeholders until cloud/RPC contracts are finalized.

## Status
Pairing, device selection, entity creation, options-based reselection, snapshots,
and the NBClient-compatible WebRTC signaling path are implemented. Video playback
may still need hub-side GStreamer pipeline tuning against real devices.

## Requirements
- Home Assistant 2024.11+ (native camera WebRTC API).
