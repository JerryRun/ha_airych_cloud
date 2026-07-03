# Airych Cloud for Home Assistant

<p align="center">
  <img src="assets/airych-cloud.png" alt="Airych Cloud" width="160">
</p>

Airych Cloud is a Home Assistant custom integration for Airych VioStation and
VioCam devices. It connects a user-managed Home Assistant instance to the
Airych cloud, ThingsBoard device data, cloud snapshots, and native Home
Assistant WebRTC camera playback.

This repository contains the cloud integration only:

```text
custom_components/airych_cloud
```

The older local/on-device integration (`airych_home`) is intentionally not
included in this HACS repository.

## Features

- App-based QR pairing with Airych OAuth device authorization.
- VioStation hub and VioCam device discovery from the user's cloud account.
- Binary sensors for people, fall, door, window, smoke/fire, recording, and
  online state.
- Native Home Assistant camera entities with WebRTC live view.
- Cloud snapshot support for camera previews and `camera.snapshot`.
- Config entry options to reselect imported VioStations and VioCams.
- Services for Airych automation actions:
  - `airych_cloud.play_hub_alarm`
  - `airych_cloud.play_camera_alarm`
  - `airych_cloud.start_camera_recording`
  - `airych_cloud.stop_camera_recording`
  - `airych_cloud.capture_camera_snapshot`
  - `airych_cloud.send_mobile_notification`

Snapshot capture is implemented. The other action services are registered as
stable Home Assistant automation surfaces and will be wired to cloud/RPC
contracts as they become available.

## Requirements

- Home Assistant 2024.11 or newer.
- HACS installed in Home Assistant.
- An Airych account with at least one bound VioStation.
- The Airych mobile app for QR approval.

The integration uses these cloud endpoints by default:

```text
App backend:  https://iot.airych.xyz:28682
ThingsBoard:  https://iot.airych.xyz:28680
```

## Install With HACS Custom Repository

1. Open Home Assistant.
2. Go to HACS.
3. Open the three-dot menu and choose **Custom repositories**.
4. Add this repository URL:

   ```text
   https://github.com/JerryRun/ha_airych_cloud
   ```

5. Select category **Integration**.
6. Install **Airych Cloud**.
7. Restart Home Assistant.
8. Go to **Settings > Devices & services > Add integration**.
9. Search for **Airych Cloud** and scan the QR code with the Airych app.

## Configuration

During setup, the integration asks you to:

1. Pair Home Assistant with your Airych account by scanning a QR code.
2. Select the VioStations to import.
3. Select the VioCams under those VioStations.

After setup, open the integration options to:

- Reselect imported devices.
- Change the automatic snapshot preview refresh interval.

## Snapshots

Home Assistant requests snapshots through the Airych backend. The backend sends
a ThingsBoard RPC to the hub, the hub uploads the JPEG and timestamp marker to
cloud storage, and Home Assistant downloads the prepared image.

Automatic dashboard preview refreshes are locally throttled. Manual snapshot
downloads and the `airych_cloud.capture_camera_snapshot` service request a fresh
snapshot.

## WebRTC Live View

The Home Assistant browser is the WebRTC offerer. The integration asks the
VioStation hub to start a GStreamer `webrtcbin` pipeline, then relays SDP and
ICE through the Airych signaling server. Media flows through standard WebRTC
into Home Assistant's native camera UI.

## Privacy And Data

This integration stores OAuth and ThingsBoard tokens in the Home Assistant
config entry storage. It sends requests to the Airych cloud and ThingsBoard
servers in order to list devices, subscribe to device attributes, request
snapshots, and start WebRTC sessions.

Camera snapshot images are loaded from the Airych cloud file service after the
hub uploads them. Live video is delivered by WebRTC between the browser and the
hub via the configured signaling/STUN/TURN infrastructure.

Privacy policy:

```text
https://www.airych.xyz/legal/privacy_en.md
```

## Troubleshooting

- If pairing fails, delete the integration entry and add it again.
- If live view does not start, check Home Assistant logs for `airych_cloud` and
  verify that the VioStation is online.
- If snapshots stay stale, check that the hub can upload snapshot files and the
  `.ts` marker to the Airych cloud file service.
- Existing test entries created against local development URLs should be removed
  and re-added so the built-in cloud URLs are used.

## Development

Basic local checks:

```bash
python3 -m compileall -q custom_components/airych_cloud
python3 -m json.tool custom_components/airych_cloud/manifest.json >/tmp/airych_manifest.json
python3 -m json.tool custom_components/airych_cloud/strings.json >/tmp/airych_strings.json
python3 -m json.tool hacs.json >/tmp/airych_hacs.json
```

Before publishing a release:

1. Update `version` in `custom_components/airych_cloud/manifest.json`.
2. Run the local checks above.
3. Push to GitHub and confirm the HACS and Hassfest workflows pass.
4. Create a GitHub release such as `v0.1.0`.

## Support

- Website: https://www.airych.xyz
- Email: support@airych.xyz
- Issues: https://github.com/JerryRun/ha_airych_cloud/issues

## License

MIT License. See [LICENSE](./LICENSE).
