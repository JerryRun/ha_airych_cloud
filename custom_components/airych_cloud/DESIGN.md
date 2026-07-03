# Airych Cloud — Home Assistant 云端集成设计规格

> 与本地版 `airych_home`（dbus）并存的**云端版**集成。设备不再内置 HA；
> 客户自行安装 HA，通过本集成接入 Airych 云（ThingsBoard）。
> domain: **`airych_cloud`**

---

## 1. 总体架构

```
设备(hub+camera) ──MQTT/属性/RPC──► ThingsBoard ◄──REST/WSS── HA 插件(airych_cloud)
        │                                                          ▲
        │ WebRTC(媒体, 经TURN)                                      │ 登录/令牌
        └────────── 信令(WS) ──────► 信令服务器 ◄── 信令 ───────────┤
                                                                   │
   App ──登录/扫码授权──► App后端 ──签发TB token/刷新/解绑──────────┘
```

- **认证/令牌生命周期**走 App 后端（瘦 BFF）。
- **数据通路**：插件用 TB token 直连 ThingsBoard（拉设备、订阅属性、发 RPC）。
- **视频**：HA 前端原生 WebRTC，插件做信令中继，媒体经自有 TURN 直连 hub。

多租户：**1 个 App 用户 = 1 个 TB Customer**；一个 Customer 下**可有多个 hub**。

---

## 2. 认证：设备授权码模式（OAuth 2.0 Device Authorization Grant，类涂鸦）

### 2.1 配对流程
1. config flow → `POST {backend}/oauth/device_authorization` → 返回 `device_code, user_code, interval, expires_in`。
2. 插件展示**二维码**（编码 `airych://oauth/device?scene=ha&user_code={user_code}`）+ `user_code` 文本兜底。
3. 用户在 App（已登录）扫码 / 输 user_code 确认绑定。
4. App 调 `POST {backend}/oauth/device/approve`，请求体包含 `user_code` 和 `action=approve`。
5. 插件按 `interval` 轮询 `POST {backend}/oauth/token`：
   - `authorization_pending` → 继续轮询
   - 返回 tokens → 创建 config entry
   - `expired_token` / `access_denied` → 失败

`approved` 返回体：
```json
{
  "status": "approved",
  "account_id": "...",
  "account_name": "张三家",
  "customer_id": "<tb customer id>",
  "tb_url": "https://tb.yourcloud.com",
  "tb_access_token": "<TB JWT 短期>",
  "tb_expires_at": 1781230000,
  "plugin_refresh_token": "<后端签发的长期令牌>"
}
```

### 2.2 令牌刷新（后端中介）
- 插件用 `tb_access_token` 直连 TB。
- 到 ~80% TTL 或遇 TB 401 时：`POST {backend}/ha/token/refresh {plugin_refresh_token}`
  → `{tb_access_token, tb_expires_at, plugin_refresh_token?}`（可轮换）。
- 后端内部负责向 TB 换新 JWT；真实 TB 凭证不落到插件。

### 2.3 多账号 / 重认证 / 删除
- **多账号**：每账号一个 config entry，`unique_id = account_id`，防同账号重复。
- **重认证**：`plugin_refresh_token` 失效 → `async_step_reauth` 重走扫码。
- **删除**：`async_remove_entry` → `POST {backend}/ha/unpair {plugin_refresh_token}` 撤销；本地无条件清理。

### 2.4 App 后端接口契约（由 App 后端实现）
| 接口 | 入参 | 出参 |
|---|---|---|
| `POST /ha/pair/start` | `client_name, ha_install_id?` | `pair_session, qr_content, user_code, interval, expires_in` |
| `POST /ha/pair/poll` | `pair_session` | `pending` / `approved{...}` / `expired` / `denied` |
| `POST /ha/token/refresh` | `plugin_refresh_token` | `tb_access_token, tb_expires_at, plugin_refresh_token?` |
| `POST /ha/unpair` | `plugin_refresh_token` | `{ok:true}` |
| `POST /ha/webrtc/offer` | `tb_access_token?/plugin token, hub_id, camera_id, sdp_offer` | `sdp_answer, ice_servers?` |
| `POST /app/tbdevice/ha/snapshot` | `token` header, `{deviceId,cameraId,resolution}` | `{ts,imageUrl,tsUrl,remoteFilePath}` |

> 说明：信令也可不经后端、由插件直连信令 WS 服务器（见 §5）。`/ha/webrtc/offer` 为
> "经后端代理信令" 的可选方案，二选一。

---

## 3. ThingsBoard 数据模型

### 3.1 设备列表
- 插件用 TB token 调 `GET /api/customer/{customerId}/deviceInfos?page=&pageSize=`
  获取该 Customer 下的 hub 设备（可多个）。
- hub 在线：用 TB 设备活动状态（server-scope 属性 `active`）。

### 3.2 hub 客户端属性（CLIENT_SCOPE，扁平动态 key）
| key | 内容 |
|---|---|
| `hub_info` | `{id,model,label,swver,wlanIp,eth0Ip}` |
| `hub_status` | `{batteryCapacity,batteryLevel,isCharging,isPowerSupply,ethCon,WifiCon,wifi_strength}` |
| `hub_latest_alert` | `{id,type,ts}` |
| `hub_unread_alerts` | number |
| `hub_cameras` | `{ids:[...]}` ← 驱动摄像头动态增删 |
| `camera_{id}_info` | `{camdevid,device_model,ip_address,rtsp_url,mac_address,hardware_version,aov_enabled,screen_flip,enable_tfrecord}` |
| `camera_{id}_friendlyname` | `{camdevid,friendlyname}` |
| `camera_{id}_recording` | `{camera_recording,streaming_sid}` |
| `camera_{id}_status` | `{camdevid,online,battery_status,charging_status,powerplugin_status,wifi_strength,sd_presence}` |
| `camera_{id}_detect` | `{person_falled,has_person,smogfire,door_opened,window_opened}` |

> 订阅策略：REST 先拉一次 CLIENT_SCOPE 全量属性做初始快照，WS 订阅全量增量
> （不写死 key，自动跟随摄像头增删 / `hub_cameras` 变化）。

### 3.3 映射规则
- 智能检测以 `camera_{id}_detect` 结构为准：`has_person`、`person_falled`、
  `door_opened`、`window_opened`、`smogfire` 均为布尔值。
- 录像状态以 **`camera_{id}_recording.camera_recording`** 为准，不从
  `camera_{id}_detect.recording` 读取。
- `smogfire` 取自 `camera_{id}_detect.smogfire`（设备端补充）。
- camera 可用性 `available = status.online`。

---

## 4. 实体模型（hub 与 camera 均为 HA 设备，camera `via_device=hub`）

### Hub 设备（`hub_info`：model/swver/identifiers=(DOMAIN, hub_id)）
| 实体 | 来源 | 类型 |
|---|---|---|
| 在线 | TB `active` | binary_sensor / connectivity |
| 电量 | `hub_status.batteryCapacity` | sensor / battery |
| WiFi 信号 | `hub_status.wifi_strength` | sensor / signal_strength (诊断) |
| 充电中 | `hub_status.isCharging != "Discharging"` | binary_sensor / battery_charging |
| 外接供电 | `hub_status.isPowerSupply` | binary_sensor / power |
| WiFi 连接 | `hub_status.WifiCon` | binary_sensor / connectivity (诊断) |
| 以太网连接 | `hub_status.ethCon` | binary_sensor / connectivity (诊断) |
| 最近告警 | `hub_latest_alert` | sensor（type 为 state，id/ts 为属性） |
| 未读告警数 | `hub_unread_alerts` | sensor / measurement |

### Camera 设备（`friendlyname` 名称，`device_model`/`hardware_version`）
| 实体 | 来源 | 类型 |
|---|---|---|
| **实时预览** | WebRTC | **camera** |
| 在线 | `status.online` | binary_sensor / connectivity |
| 有人 | `detect.has_person` | binary_sensor / presence |
| 跌倒 | `detect.person_falled` | binary_sensor / safety |
| 门 | `detect.door_opened` | binary_sensor / door |
| 窗 | `detect.window_opened` | binary_sensor / window |
| 烟雾火苗 | `detect.smogfire` | binary_sensor / safety |
| 录像中 | `recording.camera_recording` | binary_sensor / running |
| 电量 | `status.battery_status` | sensor / battery |
| WiFi 信号 | `status.wifi_strength` | sensor / signal_strength (诊断) |
| 充电中 | `status.charging_status` | binary_sensor / battery_charging (诊断) |
| 外接供电 | `status.powerplugin_status` | binary_sensor / power (诊断) |
| SD 卡 | `status.sd_presence` | binary_sensor / problem (诊断) |

---

## 5. WebRTC 实时预览

- hub 用 GStreamer `webrtcbin`（**标准 WebRTC**），仅信令传输自定义。
- HA 前端(浏览器)当 **offerer**，hub 当 **answerer**（`sendonly` 视频，H.264）。
- 插件实现 HA 原生 WebRTC（HA ≥ 2024.11）：
  - `camera` 实体 `_attr_frontend_stream_type = StreamType.WEB_RTC`
  - `async_handle_async_webrtc_offer(offer, session_id, send_message)`：经信令把 offer 送 hub，
    取回 answer → `send_message(WebRTCAnswer(...))`。
  - `async_on_webrtc_candidate(session_id, candidate)`：trickle ICE（可选；先非 trickle）。
  - `async_get_webrtc_client_configuration`：下发自有 STUN/TURN（**固定账号密码**）。
- **信令通道**：插件直连自有信令 **WebSocket** 服务器（消息格式见 `signaling.py`）。
- **当前 App 对齐实现**：
  - WebRTC 配置通过 App 后端 `GET /app/tbdevice/webrtcconfig`
    获取，请求头使用 `token: <app_access_token>`。
  - 插件保存 OAuth 返回的 App 后端 `access_token`，刷新时同时更新
    App token 与 `tb_access_token`。
  - 信令协议对齐 NBClient：`HELLO <peerid>` / `SESSION` /
    `{"sdp":...}` / `{"ice":...}`。
  - HA 前端仍作为 offerer，因此下发 hub RPC `openSessionPipeline` 时带
    `createoffer=false` 和 `remote_peerid=<ha_peerid>`。
  - hub 侧 pipeline 当前直接读取摄像头 HTTP fMP4 实时流：
    `http://{camip}:9998/live/1?mux=fmp4&audio=opus`
    然后把 H.264 视频和 Opus 音频重新 pay 到 `webrtcbin`。

---

## 6. 快照

- HA 的卡片预览和 `camera.snapshot` 走 `Camera.async_camera_image()`。
- 这里不复用 WebRTC 媒体通道发 HTTP，也不要求 HA 能访问 hub 内网地址。
- 插件调用 App 后端 `POST /app/tbdevice/ha/snapshot`，请求头使用
  `token: <app_access_token>`，body 为 `{deviceId,cameraId,resolution}`。
- App 后端读取 OTA `/api/version` 里的 `ofs.weedfs.external` 和
  `ofs.filrt.avatar`，拼出云端路径：
  `avatars/camera/{cameraId}/snapshot.jpeg`。
- App 后端通过 ThingsBoard two-way RPC 调 hub：
  method `getCameraSnapshot`，params 包含 `cameraId`、`resolution`、
  `internalUrl`、`remoteFilePath`。
- hub 写入：
  `.../snapshot.jpeg` 和 `.../snapshot.jpeg.ts`，RPC 返回 13 位毫秒时间戳
  `ts`。
- 后端返回 `{ts,imageUrl,tsUrl,remoteFilePath}`。插件轮询 `tsUrl`，当 marker
  中的时间戳 `>= ts` 时，再下载 `imageUrl?v=<marker_ts>` 作为 HA 快照。
- HA 前端的自动预览刷新和手动“下载快照”都会进入
  `/api/camera_proxy/{entity_id}`，但自动预览通常会带 `width/height` 参数。
  插件据此区分：预览刷新使用 `snapshot_preview_interval` 做本地缓存节流
  （默认 60 秒，可在配置入口的“快照设置”调整）；手动下载快照不走节流，
  每次都请求新图。

---

## 7. HA 服务动作

HA 侧暴露的是用户意图，不再暴露旧的聚合服务 `airych_cloud.send_alert`。
具体走云端接口还是 ThingsBoard RPC，等后端/hub 合约确定后逐个接入。

| 服务 | 目标 | 状态 |
|---|---|---|
| `airych_cloud.play_hub_alarm` | VioStation | 已注册，后端/RPC 实现预留 |
| `airych_cloud.play_camera_alarm` | VioCam | 已注册，后端/RPC 实现预留 |
| `airych_cloud.start_camera_recording` | VioCam | 已注册，后端/RPC 实现预留 |
| `airych_cloud.stop_camera_recording` | VioCam | 已注册，后端/RPC 实现预留 |
| `airych_cloud.capture_camera_snapshot` | VioCam | 已实现，调用现有 App 后端快照接口 |
| `airych_cloud.send_mobile_notification` | 用户/App | 已注册，后端实现预留 |

字段约定：

- 告警音：`duration`、`volume`、`tone`。
- 录像开始：`duration`、`reason`。
- 快照：`resolution`、`with_osd`。当前后端快照链路只使用 `resolution`，
  `with_osd` 作为预留字段。
- 手机通知：`title`、`message`、`level`、`camera`、`include_snapshot`。

摄像头 device action 仅保留“抓拍”，内部调用
`airych_cloud.capture_camera_snapshot`。

---

## 7. 文件结构

```
custom_components/airych_cloud/
├── manifest.json        # domain, cloud_push, requirements
├── const.py             # 常量 / 属性 key 解析规则
├── api.py               # 后端客户端 + TB REST/WS 客户端
├── models.py            # Hub / Camera 数据模型 + 属性解析
├── coordinator.py       # 状态管理 / WS 订阅 / 令牌刷新 / 分发
├── config_flow.py       # 扫码配对 + reauth + 多账号
├── __init__.py          # 生命周期 + 服务注册
├── binary_sensor.py / sensor.py / text.py / button.py
├── camera.py            # WebRTC 预览实体
├── signaling.py         # 信令 WS 客户端（待对接消息格式）
├── device_action.py     # camera device action → capture_camera_snapshot
├── services.yaml / strings.json / translations/
```

## 8. 待补充（依赖外部输入）
- [ ] 信令 WS 服务器的 URL 与**消息格式**（offer/answer/candidate 帧结构）。
- [ ] `/ha/pair/*`、`/ha/token/refresh`、`/ha/unpair` 的实际地址与字段微调。
- [ ] TURN/STUN 的地址与固定账号密码下发方式（配对响应内 or 配置项）。
- [ ] 告警音、录像、手机通知动作的云端/RPC 合约确认。
