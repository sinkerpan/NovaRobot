# NovaRobot TR One 连接服务流程

本文是《手机 App 连接机器人方法》在当前宿主机上的落地说明。  
机器人不再依赖“手机开热点”，而是由机器人自己管理网络：优先连已保存 Wi-Fi，失败再开机器人 AP，App 通过 mDNS / UDP / HTTP 自动发现。

---

## 1. 当前宿主机实况

| 项 | 值 |
| --- | --- |
| 主机 | Jetson Orin Nano，hostname `nimo` |
| 网卡 | `wlP1p1s0`（Intel AX200，MAC `58:02:05:e8:b5:a1`） |
| 当前模式 | **STA CONNECTED**，未开 AP（避免打断现有连接） |
| 已连 Wi-Fi | `HUAWEI-XEZMDA_5G` |
| 本机 IP | `192.168.3.12` |
| 机器人 ID | `NovaRobot-B5A1`（MAC 后 4 位） |
| 待机 AP | SSID `NovaRobot-B5A1`，密码 `12345678` |
| mDNS | `novarobot-b5a1.local` → `192.168.3.12` |
| HTTP | `http://192.168.3.12:18080` |
| UDP Discovery | `0.0.0.0:53535` |
| BLE | 需 `bluetoothd -E`（NVIDIA drop-in 会覆盖旧 `99-*.conf`，必须用 `zz-*.conf`） |

代码目录：`/home/novarobot/NovaRobot_wifi_manager`  
进程：`python3 -m NovaRobot_wifi_manager`（已在运行）

局域网里另有一台设备占用了 `Nimo-TR-One.local`（`192.168.3.22`，旧名），所以本机发布的名字是 `novarobot-b5a1.local`，避免撞名。

---

## 2. 总体架构

```text
                 手机 App
                    │
     BLE 发现/配网   │   Wi-Fi 发现
                    │
                    ▼
        ┌──────────────────────────┐
        │     NovaRobot_wifi_manager    │
        │                          │
        │  状态机 Network FSM      │
        │    ├ AP Manager          │
        │    ├ STA Manager         │
        │    ├ BLE Provision       │
        │    │    ├ BlueZ Agent（电源键确认）│
        │    │    └ Status LED（配对快闪）   │
        │    ├ mDNS                │
        │    ├ UDP Discovery       │
        │    └ HTTP 配网 / 状态    │
        └────────────┬─────────────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       家庭WiFi   手机热点    Robot AP
         STA        STA         AP
          │          │          │
          └──────────┼──────────┘
                     ▼
              App 局域网控制
```

软件拆分与文档建议一致：

```text
NovaRobot_wifi_manager
        │
        ├── AP Manager
        ├── STA Manager
        ├── BLE Provisioning
        ├── mDNS
        ├── UDP Discovery
        └── Network State Machine
```

职责分工：

| 通道 | 负责 |
| --- | --- |
| BLE | 发现机器人 + 配网 + 身份 |
| Wi-Fi | 视频、控制、地图、参数、升级 |
| mDNS | App 用名字找机器人，不写死 IP |
| UDP Discovery | 局域网广播秒级发现 |
| HTTP | 状态查询、扫网、提交 SSID/密码 |

---

## 3. 启动后的状态机

```text
BOOT
 │
 ▼
CHECK_CONFIG
 │
 ├── 已配置 WiFi / 当前已在线
 │       ↓
 │    CONNECTING（必要时）
 │       ↓
 │    CONNECTED ────────┐
 │       │              │
 │       │断线           │
 │       ▼              │
 │    RECONNECT          │
 │       │              │
 │       └───────────────┘
 │
 └── 未配置，或 STA 多次失败
         ↓
      AP_MODE
         ↓
      手机连接 NovaRobot-B5A1
         ↓
      BLE / HTTP 配网
         ↓
      保存配置
         ↓
      STA_MODE
```

当前这次启动的真实轨迹：

```text
BOOT → CHECK_CONFIG → CONNECTED（HUAWEI-XEZMDA_5G / 192.168.3.12）
```

优先级：

1. 已保存的家庭 Wi-Fi（例如 `HUAWEI-XEZMDA_5G`）
2. 已保存的手机热点（iPhone / iQOO / Redmi 等）
3. 都连不上 → 开机器人 AP `NovaRobot-B5A1`

安全策略：只要当前 STA 仍然在线，状态机**不会自动切 AP**。  
本机只有一张 AX200 网卡，AP 和 STA 不能同时占用；贸然切 AP 会立刻丢掉现在的 `192.168.3.12`。

---

## 4. 三条用户场景

### 场景 1：第一次使用 / 尚未配网

```text
机器人开机
   ↓
没有已保存 Wi-Fi
   ↓
自动进入 AP_MODE
   ↓
SSID: NovaRobot-B5A1
密码: 12345678
   ↓
手机连上机器人热点
   ↓
App 打开 http://10.42.0.1:18080 或发 UDP 发现
   ↓
输入家庭 Wi-Fi
   ↓
机器人保存并切到 STA
```

### 场景 2：去网球场（没有家庭 Wi-Fi）

```text
开机
   ↓
尝试已保存 Wi-Fi
   ↓
全部失败
   ↓
自动进入 AP_MODE
   ↓
手机连 NovaRobot-B5A1
   ↓
App 局域网控制
```

用户不用重新输入 AP 密码。

### 场景 3：家里 / 已配过网（当前就是这个状态）

```text
开机
   ↓
NetworkManager 已连上 HUAWEI-XEZMDA_5G
   ↓
状态机判定 CONNECTED
   ↓
发布 mDNS + UDP + HTTP
   ↓
App 发现 novarobot-b5a1.local / 192.168.3.12
   ↓
直接控制
```

---

## 5. App 发现机器人

不要依赖固定 IP。机器人重启后 DHCP 可能换地址，App 应走下面两条之一。

### mDNS

```text
novarobot-b5a1.local
服务：_novarobot._tcp 端口 18080
TXT：id=NovaRobot-B5A1  fw=1.0.0  mode=sta
```

本机已验证：

```text
avahi-resolve -n novarobot-b5a1.local
novarobot-b5a1.local    192.168.3.12
```

### UDP Discovery

App 向 `255.255.255.255:53535` 发送：

```text
NOVAROBOT_DISCOVER
```

或：

```json
{"cmd": "discover"}
```

机器人单播回复：

```json
{
  "device": "NovaRobot TR One",
  "id": "NovaRobot-B5A1",
  "ip": "192.168.3.12",
  "battery": null,
  "firmware": "1.0.0",
  "mode": "sta",
  "state": "CONNECTED",
  "ssid": "HUAWEI-XEZMDA_5G",
  "ap_ssid": "NovaRobot-B5A1",
  "mdns": "novarobot-b5a1.local",
  "http": "http://192.168.3.12:18080",
  "udp_port": 53535
}
```

本机已用 `NovaRobot-wifi udp` 验证通过。

---

## 6. 配网接口

手机连上机器人后，可用浏览器或 App 调这些接口。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/` | 配网页 |
| GET | `/status` | 发现/状态 JSON，与 UDP 回复相同 |
| GET | `/api/wifi/scan` | 扫描附近 Wi-Fi |
| POST | `/api/wifi` | 提交 `{"ssid","password"}`，保存并切 STA |
| POST | `/api/mode` | `{"mode":"ap"}` 或 `{"mode":"sta"}` |

BLE GATT（服务以 root 运行；需 `bluetoothd -E`，由 `zz-novarobot-bluetooth.conf` 覆盖 NVIDIA drop-in）：

| UUID | 用途 |
| --- | --- |
| `0000a001-0000-1000-8000-00805f9b34fb` | 配网 Service |
| `0000a002-...` | 读设备信息 / status |
| `0000a003-...` | 写 JSON 命令（见下） |
| `0000a004-...` | 读/Notify 当前状态或扫网结果（含最近 `ble_result`） |

广播名：`NovaRobot-B5A1`

写 `a003` 命令：

```json
{"cmd":"wifi_scan"}                    // 机器人扫附近 Wi-Fi
{"ssid":"家里WiFi","password":"密码"}   // 配网并切 STA
{"cmd":"wifi_config","ssid":"...","password":"..."}  // 同上
{"cmd":"get_mode"}                     // 只查询当前模式，不切换
{"mode":"ap"}                          // App 开热点
{"mode":"sta"}                         // App 连已保存 Wi-Fi
{"cmd":"ping"}                         // 心跳，标记手机已接管
```

扫网进行中读 `a004` 会得到 `{"cmd":"wifi_scan","type":"wifi_scan_busy"}`；完成后 Notify / Read：

```json
{"ok":true,"cmd":"wifi_scan","type":"wifi_scan_result","networks":[{"ssid":"...","signal":80,"security":"WPA2","saved":false}]}
```

载荷截断约 480 字节，按信号强弱保留最前面的热点。App 应轮询 Read，不要只等 1.8 秒。

手机成功读/写 GATT 后 `phone_linked=true`（`/status` 里 `app_controls_mode`），状态机**不再自动**在 AP/STA 间抢切，改由 App 通过 BLE/HTTP 下发 `mode`。

---

## 6.1 App 蓝牙配网页与 wifi_manager 的交互

对应 App：`NimoApp_android_uts/pages/home/home.vue`  
对应协议：`utils/robot-ble-provision.js`  
对应机器人：`handle_ble_command` / `set_mode` / `current_mode_result`

### 原则

- 蓝牙连上之后，App 才能和机器人交互。
- **当前模式**只认机器人回包里的 `mode` / `current_mode`，不能用按钮点了什么就改标题。
- 切模式进行中只显示「正在切换模式，请稍候...」。
- 收到 `type=mode_result` 且 `mode=ap` 后，才改成「当前模式：机器人AP」，并把上一行改成「切换机器人AP成功」。
- `mode=sta` 同理：「当前模式：机器人连网」+「切换机器人连网成功」。

### 第①步：扫描并连接蓝牙

```text
App 扫描 NovaRobot-XXXX
   ↓
createBLEConnection + 发现 A001/A002/A003/A004
   ↓
读 A002（discover_payload，含现场 mode）
   ↓
写 A003  {"cmd":"get_mode"}
   ↓
轮询读 A004，直到 type=mode_result
   ↓
App 用回包 mode 显示「当前模式：机器人AP / 机器人连网」
```

`get_mode` 回包（A004，短 JSON）：

```json
{
  "ok": true,
  "cmd": "mode",
  "type": "mode_result",
  "mode": "sta",
  "current_mode": "sta",
  "ip": "192.168.3.12",
  "ap_ssid": "NovaRobot-B5A1",
  "ssid": "HUAWEI-XEZMDA_5G"
}
```

`mode` 只允许：`ap` / `sta` / `offline`。这是现场读网卡，不是请求值。

### 第①步：切换机器人 AP

```text
用户点「机器人AP」
   ↓
App 保持原「当前模式」，下方显示「正在切换模式，请稍候...」
   ↓
写 A003  {"mode":"ap"}
   ↓
A004 先 Notify  {"type":"mode_busy","target":"ap"}
   ↓
wifi_manager.set_mode("ap") → 关 STA、启 hostapd
   ↓
成功后 A004 Notify / Read：
{
  "ok": true,
  "cmd": "mode",
  "type": "mode_result",
  "mode": "ap",
  "current_mode": "ap",
  "ip": "10.42.0.1",
  "ap_ssid": "NovaRobot-B5A1",
  "target": "ap"
}
   ↓
App 确认 mode===ap 后才：
  当前模式：机器人AP
  切换机器人AP成功
  收起第②③步
```

进行中的 `mode_busy` **没有** 现场 `mode`，只有 `target`。App 必须忽略 busy，不能把 target 当成已经切成功。

切到「机器人连网」写 `{"mode":"sta"}`，成功回包 `mode=sta`，App 显示「切换机器人连网成功」，并重新展开第②③步。

### App 判定成功的条件

必须同时满足：

1. A004 / A002 不是 `*_busy`，也不是扫网结果
2. `ok !== false`
3. `mode` 或 `current_mode` 等于本次目标（`ap` 或 `sta`）

机器人切 AP 可能要 10–30 秒。App 轮询最多约 35 秒；超时仍未看到 `mode=ap` 就提示失败，标题保持原模式。

### 特征分工

| 特征 | App 用法 |
| --- | --- |
| A002 | 始终读现场 `discover_payload`（含真实 `mode`） |
| A003 | 只写完整 JSON：`get_mode` / `mode` / `wifi_scan` / 配网 |
| A004 | 最近一次命令结果：busy → result；切模式用短 JSON，避免 480 字节截断 |

一键修复蓝牙广播：

```bash
sudo bash /home/novarobot/fix-novarobot-ble.sh
```

---

## 6.2 蓝牙配对确认：LED 快闪 + 电源键短按

手机连机器人蓝牙时，BlueZ 会向本机要一次「是否允许配对」。以前这条请求落到 GNOME 桌面弹窗。现在 `NovaRobot_wifi_manager` 注册为默认 BlueZ Agent（`DisplayYesNo`），改由机身硬件确认：

```text
App 发起蓝牙连接 / 配对
   ↓
机器人接管 org.bluez.Agent1
   ↓
系统 LED 快闪（gpiochip1 PCC.01，约 80ms 开 / 80ms 关）
   ↓
短按 J14 PWR BTN（KEY_POWER）：按下灯灭，松开灯亮并确认
   ↓
配对确认，LED 恢复常亮
```

硬件入口（本机已核实）：

| 器件 | 入口 | 说明 |
| --- | --- | --- |
| 系统开关按键 | `/dev/input/event0`，`gpio-keys` `KEY_POWER=116` | J14 第一个 PWR BTN + 第二个 GND。等待确认期间 `EVIOCGRAB`。按下 LED 灭，松开 LED 亮；短按确认配对 |
| 系统 LED | `gpiochip1` `PCC.01`（line 13） | J14 LED+/LED-，信号 `PWR_LED_CTRL`。开机默认常亮；配对等待时快闪 |

超时 30 秒未短按则拒绝配对，LED 恢复。长按电源键**不会**确认连接。`/status` 的 `ble.pairing_pending` 为 true 时表示正在等短按。

相关配置（`config/default.json`）：

```json
"ble_pair_timeout_sec": 30,
"ble_pair_short_press_max_ms": 1500,
"ble_led_name": "auto",
"ble_led_chip": "gpiochip1",
"ble_led_line": 13,
"ble_led_on_ms": 80,
"ble_led_off_ms": 80
```

---

## 7. 端口与进程

| 服务 | 地址 | 当前状态 |
| --- | --- | --- |
| HTTP API / 配网页 | `0.0.0.0:18080` | 已监听 |
| UDP Discovery | `0.0.0.0:53535` | 已监听 |
| mDNS | `novarobot-b5a1.local` / `_novarobot._tcp` | 已发布 |
| 旧热点 `nimo-hotspot.service` | hostapd | 保持关闭，避免抢网卡 |
| BLE GATT | BlueZ hci0 | 需 `bluetoothd -E`；修好后 `ActiveInstances≥1` |
| BLE 配对 Agent | `/org/bluez/novarobot/agent` | LED 快闪 + 电源键短按确认 |

本机 `8080`、`9090` 已被其他程序占用，所以 HTTP 选了 `18080`。

---

## 8. 操作命令

查看状态：

```bash
/home/novarobot/NovaRobot_wifi_manager/scripts/NovaRobot-wifi status
curl http://192.168.3.12:18080/status
```

UDP 发现自测：

```bash
/home/novarobot/NovaRobot_wifi_manager/scripts/NovaRobot-wifi udp
```

手动切模式（会中断当前 Wi-Fi，慎用）：

```bash
/home/novarobot/NovaRobot_wifi_manager/scripts/NovaRobot-wifi mode ap
/home/novarobot/NovaRobot_wifi_manager/scripts/NovaRobot-wifi mode sta
```

提交新 Wi-Fi：

```bash
/home/novarobot/NovaRobot_wifi_manager/scripts/NovaRobot-wifi provision "HUAWEI-XEZMDA_5G" "416302703"
```

禁止自动开 AP（调试保护）：

```bash
touch /home/novarobot/NovaRobot_wifi_manager/data/disable-ap
```

---

## 9. 开机常驻

当前没有免密 sudo，所以服务以用户进程跑着，还没有写入系统 systemd。

用户级常驻（登录后自动拉起）：

```bash
bash /home/novarobot/NovaRobot_wifi_manager/scripts/install.sh
```

开机不登录也要跑，还需要：

```bash
sudo loginctl enable-linger nimo
```

要让 AP / hostapd / BLE GATT 完整可用，需要系统级安装：

```bash
sudo bash /home/novarobot/NovaRobot_wifi_manager/scripts/install.sh
```

系统级 unit：`NovaRobot-wifi-manager.service`  
不要同时启用旧的 `nimo-hotspot.service`，两套会抢同一张网卡。

---

## 9.1 做成 Debian 安装包（.deb）

Jetson / Ubuntu 用 `.deb` 即可，其它机器用 `dpkg -i` 或 `apt install ./xxx.deb`。

在源码目录构建：

```bash
bash /home/novarobot/NovaRobot_wifi_manager/scripts/build-deb.sh
```

产物：`dist/novarobot-wifi-manager_1.0.0_all.deb`（纯 Python，`Architecture: all`）。

安装（会启用 systemd、写入 bluetoothd `-E` drop-in）：

```bash
sudo systemctl disable --now NovaRobot-wifi-manager.service   # 若本机还在跑源码目录那套
sudo apt install ./dist/novarobot-wifi-manager_1.0.0_all.deb
```

包内路径：

| 路径 | 用途 |
| --- | --- |
| `/usr/lib/novarobot-wifi-manager/` | Python 代码 |
| `/etc/novarobot-wifi-manager/default.json` | 配置（dpkg conffile，升级会保留本地修改） |
| `/var/lib/novarobot-wifi-manager/` | wifi.json / identity |
| `/lib/systemd/system/novarobot-wifi-manager.service` | 系统服务 |
| `/usr/bin/NovaRobot-wifi` | 状态/配网 CLI |

换机部署时改 `/etc/novarobot-wifi-manager/default.json` 里的 `iface`（找不到则自动选第一张无线网卡）。`mdns_hostname` 为 `auto` 时按 MAC 后 4 位生成。

卸载：

```bash
sudo apt remove novarobot-wifi-manager
```

配置文件默认保留；连配置一起删用 `apt purge`。

其它格式：同一套文件也可用 `fpm` 打成 `.rpm`（Fedora/openSUSE），但本仓库以 Ubuntu/Jetson 的 `.deb` 为准。

---

## 10. 和旧热点脚本的关系

宿主机原来就有：

- `/home/novarobot/optimize-hotspot-24g.sh`
- `/usr/local/sbin/nimo-hotspot-start.sh`
- `nimo-hotspot.service`（disabled）

那套是“永远开 AP，SSID=`nimo`”。  
现在这套是文档里的状态机：先 STA，失败才 AP，SSID=`NovaRobot-XXXX`。  
旧服务保持关闭即可。
