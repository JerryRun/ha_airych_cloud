"""Constants for the Airych Cloud integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "airych_cloud"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
]

# ---------------------------------------------------------------------------
# Config entry: data / options keys
# ---------------------------------------------------------------------------
# Stored in entry.data after a successful pairing.
CONF_BACKEND_URL = "backend_url"            # App backend base URL
CONF_ACCOUNT_ID = "account_id"
CONF_ACCOUNT_NAME = "account_name"
CONF_CUSTOMER_ID = "customer_id"            # ThingsBoard customer id
CONF_TB_URL = "tb_url"                       # ThingsBoard base URL
CONF_APP_ACCESS_TOKEN = "app_access_token"   # App backend OAuth access token
CONF_TB_ACCESS_TOKEN = "tb_access_token"
CONF_TB_EXPIRES_AT = "tb_expires_at"
CONF_PLUGIN_REFRESH_TOKEN = "plugin_refresh_token"
CONF_SIGNALING_URL = "signaling_url"        # custom WebRTC signaling WS server
CONF_ICE_SERVERS = "ice_servers"            # STUN/TURN list (fixed credentials)
CONF_SELECTED_HUB_IDS = "selected_hub_ids"
CONF_SELECTED_CAMERA_IDS = "selected_camera_ids"
CONF_SNAPSHOT_PREVIEW_INTERVAL = "snapshot_preview_interval"

# HA frontend may refresh camera still previews frequently. This interval only
# throttles automatic preview refreshes; explicit snapshot downloads stay fresh.
DEFAULT_SNAPSHOT_PREVIEW_INTERVAL = 60

# Default cloud endpoint base URLs.
DEFAULT_BACKEND_URL = "https://iot.airych.xyz:28682"
DEFAULT_TB_URL = "https://iot.airych.xyz:28680"

# ---------------------------------------------------------------------------
# OAuth endpoints (relative to backend_url)
# ---------------------------------------------------------------------------
OAUTH_CLIENT_ID = "airych-home-assistant"
OAUTH_SCOPE = (
    "openid offline_access airych:ha "
    "airych:device:read airych:device:rpc airych:webrtc"
)
OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

EP_PAIR_START = "/oauth/device_authorization"
EP_PAIR_POLL = "/oauth/token"
EP_TOKEN_REFRESH = "/oauth/token"
EP_UNPAIR = "/oauth/revoke"
EP_WEBRTC_OFFER = "/ha/webrtc/offer"
EP_WEBRTC_CONFIG = "/app/tbdevice/webrtcconfig"
EP_CAMERA_SNAPSHOT = "/app/tbdevice/ha/snapshot"

# Pairing poll defaults (overridden by backend response when present).
DEFAULT_PAIR_INTERVAL = 3       # seconds between polls
DEFAULT_PAIR_TIMEOUT = 300      # seconds before the pairing session expires

# Refresh the TB token once we are within this fraction of its TTL.
TOKEN_REFRESH_MARGIN = 0.2      # refresh at ~80% of lifetime

# ---------------------------------------------------------------------------
# ThingsBoard
# ---------------------------------------------------------------------------
TB_SCOPE_CLIENT = "CLIENT_SCOPE"
TB_SCOPE_SERVER = "SERVER_SCOPE"
TB_RPC_TIMEOUT_MS = 10000

# ---------------------------------------------------------------------------
# Hub client-scope attribute keys
# ---------------------------------------------------------------------------
ATTR_HUB_INFO = "hub_info"
ATTR_HUB_STATUS = "hub_status"
ATTR_HUB_LATEST_ALERT = "hub_latest_alert"
ATTR_HUB_UNREAD_ALERTS = "hub_unread_alerts"
ATTR_HUB_CAMERAS = "hub_cameras"

# Per-camera attribute key suffixes: f"camera_{cam_id}_{suffix}"
CAM_KEY_PREFIX = "camera_"
CAM_SUFFIX_INFO = "info"
CAM_SUFFIX_FRIENDLYNAME = "friendlyname"
CAM_SUFFIX_RECORDING = "recording"
CAM_SUFFIX_STATUS = "status"
CAM_SUFFIX_DETECT = "detect"

# ---------------------------------------------------------------------------
# Dispatcher signals
# ---------------------------------------------------------------------------
# Fired with a Hub when a new hub is discovered.
SIGNAL_NEW_HUB = f"{DOMAIN}_new_hub"
# Fired with (hub_id, camera_id) when a new camera is discovered under a hub.
SIGNAL_NEW_CAMERA = f"{DOMAIN}_new_camera"
# Fired with (hub_id, camera_id, snapshot_response) after a fresh snapshot was requested.
SIGNAL_CAMERA_SNAPSHOT = f"{DOMAIN}_camera_snapshot"

MANUFACTURER = "Airych"
