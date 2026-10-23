from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from typing import Any, Callable

log = logging.getLogger("novarobot.ble")

PAYLOAD_LIMIT = 480

# 内核 5.15 用扩展广播时不打开 LE Enhanced Connection Complete（bit 9），RTL8822 此时
# 连接建立事件一个都不报，内核丢弃该连接上的 ACL，手机 MTU 请求无人应答后断开。
# 在内核默认 LE 事件掩码 0x0a1ddf 基础上补 bit 9；控制器每次复位（开机 / 重启 bluetoothd / 上下电）后都要重设。
LE_EVENT_MASK = 0x00000000000A1DDF | 0x200


def enable_le_enhanced_conn_event(hci: str = "hci0") -> bool:
    mask = [f"{b:02x}" for b in LE_EVENT_MASK.to_bytes(8, "little")]
    try:
        out = subprocess.run(
            ["hcitool", "-i", hci, "cmd", "0x08", "0x0001", *mask],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception as exc:
        log.warning("set LE event mask failed: %s", exc)
        return False
    # 成功时 Command Complete 最后一个字节是状态 00
    ok = out.returncode == 0 and out.stdout.strip().endswith("00")
    if not ok:
        log.warning("set LE event mask failed: rc=%s out=%s err=%s", out.returncode, out.stdout.strip(), out.stderr.strip())
    return ok


def _is_scan_payload(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    typ = str(data.get("type") or "")
    cmd = str(data.get("cmd") or "")
    return cmd in {"wifi_scan", "scan"} or typ.startswith("wifi_scan")


def _is_mode_payload(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    typ = str(data.get("type") or "")
    cmd = str(data.get("cmd") or "")
    return cmd == "mode" or typ.startswith("mode_")


def _fit_mode(data: dict[str, Any]) -> bytes:
    typ = str(data.get("type") or "mode_result")
    current = str(data.get("current_mode") or data.get("mode") or "")
    # 切换中：只表示还在切。target 不能出现在成功包里。
    if typ == "mode_busy":
        payload: dict[str, Any] = {
            "ok": bool(data.get("ok", True)),
            "cmd": "mode",
            "type": "mode_busy",
            "target": str(data.get("target") or current or ""),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return raw
    # 开热点前最后一条蓝牙包：让手机去连 AP。连上 Wi-Fi 才是成功，不再等 mode_result。
    if typ == "ap_join":
        payload = {
            "ok": True,
            "cmd": "mode",
            "type": "ap_join",
            "target": "ap",
            "ap_ssid": data.get("ap_ssid") or "",
            "password": data.get("password") or "",
            "ip": data.get("ip") or "",
            "confirm": "wifi",
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = {
        "ok": bool(data.get("ok", True)),
        "cmd": "mode",
        "type": "mode_result",
        "mode": current,
        "current_mode": current,
        "ap_ssid": data.get("ap_ssid") or "",
        "ip": data.get("ip") or "",
    }
    # 连网成功才带 ssid；热点成功包不带空 ssid，也不带 target
    if current == "sta":
        payload["ssid"] = data.get("ssid") or ""
    if data.get("error"):
        payload["error"] = str(data["error"])[:80]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit_json(data: dict[str, Any], limit: int = PAYLOAD_LIMIT) -> bytes:
    """Keep scan JSON under ATT-friendly size; drop weakest APs first."""
    networks = [n for n in (data.get("networks") or []) if isinstance(n, dict) and n.get("ssid")]
    saved = [s for s in (data.get("saved_wifi") or []) if s][:12]
    for style in ("full", "mid", "min"):
        n = len(networks)
        while n >= 0:
            rows: list[dict[str, Any]] = []
            for item in networks[:n]:
                ssid = str(item.get("ssid") or "")
                if style == "full":
                    rows.append(
                        {
                            "ssid": ssid,
                            "signal": int(item.get("signal") or 0),
                            "security": str(item.get("security") or ""),
                            "saved": bool(item.get("saved")),
                        }
                    )
                elif style == "mid":
                    rows.append({"ssid": ssid, "signal": int(item.get("signal") or 0)})
                else:
                    rows.append({"ssid": ssid})
            payload: dict[str, Any] = {
                "ok": data.get("ok", True),
                "cmd": data.get("cmd") or "wifi_scan",
                "type": data.get("type") or "wifi_scan_result",
                "networks": rows,
            }
            if data.get("error"):
                payload["error"] = str(data["error"])[:80]
            if saved and style != "min":
                payload["saved_wifi"] = saved
            if n < len(networks):
                payload["truncated"] = True
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) <= limit:
                return raw
            if n == 0:
                break
            n -= 1
    return json.dumps(
        {
            "ok": data.get("ok", True),
            "cmd": "wifi_scan",
            "type": data.get("type") or "wifi_scan_result",
            "error": str(data.get("error") or "payload too large")[:80],
            "networks": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")[:limit]

INFO_KEYS = ("id", "firmware", "mode", "state", "ssid", "ip", "ap_ssid", "ap_clients", "ap_client_connected")
RESULT_KEYS = ("ok", "cmd", "type", "state", "mode", "ssid", "ip", "error")


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit_info(info: dict[str, Any], last: dict[str, Any] | None = None, limit: int = PAYLOAD_LIMIT) -> bytes:
    """A002 / A004 状态包：只留 App 用得到的字段，超长时先删 saved_wifi，保证是完整 JSON。"""
    base: dict[str, Any] = {k: info.get(k) for k in INFO_KEYS if k in info}
    if info.get("error"):
        base["error"] = str(info["error"])[:80]
    extra: dict[str, Any] = {}
    if last:
        extra = {"ok": last.get("ok"), "ble_result": last}
    saved = [str(s) for s in (info.get("saved_wifi") or []) if s][:12]
    for n in range(len(saved), -1, -1):
        payload = dict(base, **extra)
        if saved:
            payload["saved_wifi"] = saved[:n]
        raw = _dumps(payload)
        if len(raw) <= limit:
            return raw
    if last:
        slim = {k: last.get(k) for k in RESULT_KEYS if k in last}
        if "error" in slim:
            slim["error"] = str(slim["error"])[:80]
        raw = _dumps(dict(base, ok=last.get("ok"), ble_result=slim))
        if len(raw) <= limit:
            return raw
    return _dumps(base)


SERVICE_UUID = "0000a001-0000-1000-8000-00805f9b34fb"
CHAR_INFO_UUID = "0000a002-0000-1000-8000-00805f9b34fb"
CHAR_WIFI_UUID = "0000a003-0000-1000-8000-00805f9b34fb"
CHAR_STATUS_UUID = "0000a004-0000-1000-8000-00805f9b34fb"

BLUEZ = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
LE_ADV = "org.bluez.LEAdvertisement1"
GATT_MGR = "org.bluez.GattManager1"
LE_ADV_MGR = "org.bluez.LEAdvertisingManager1"
ADAPTER_IFACE = "org.bluez.Adapter1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"


class BleProvision:
    """
    BLE 配网外设：
      a002 read            -> 设备信息 / status（最近命令结果在 ble_result）
      a003 write           -> JSON 命令
           {"cmd":"wifi_scan"}
           {"ssid":"...","password":"..."} 或 {"cmd":"wifi_config",ssid,password}
           {"cmd":"get_mode"}
           {"mode":"ap"} / {"mode":"sta"}
           {"cmd":"ping"}
      a004 read + notify   -> 扫网结果（紧凑 JSON）或当前状态
    写命令在后台线程执行；扫网/配网未完成时 type 为 *_busy。
    手机读写过 GATT 后 phone_linked=True；最近一次读写起 app_control_lease_sec 内状态机不自动抢模式。
    """

    def __init__(
        self,
        local_name: str,
        info_fn: Callable[[], dict[str, Any]],
        command_fn: Callable[[dict[str, Any]], dict[str, Any]],
        pair_timeout_sec: int = 30,
        short_press_max_ms: int = 1500,
        led_name: str = "auto",
        led_on_ms: int = 80,
        led_off_ms: int = 80,
        led_chip: str = "gpiochip1",
        led_line: int = 13,
    ) -> None:
        self.local_name = local_name
        self.info_fn = info_fn
        self.command_fn = command_fn
        self.pair_timeout_sec = pair_timeout_sec
        self.short_press_max_ms = short_press_max_ms
        self.led_name_cfg = led_name
        self.led_on_ms = led_on_ms
        self.led_off_ms = led_off_ms
        self.led_chip = led_chip
        self.led_line = led_line
        self.available = False
        self.phone_linked = False
        self.last_activity = 0.0
        self.error: str | None = None
        self.pairing_pending = False
        self.pairing_device: str | None = None
        self.led_name: str | None = None
        self.power_button_path: str | None = None
        self._thread: threading.Thread | None = None
        self._loop = None
        self._last_result: dict[str, Any] = {}
        self._status_char = None
        self._confirmer = None

    def start(self) -> None:
        try:
            from .bluetooth_host import ensure_bluetooth_experimental, stop_adapter_discovery

            ensure_bluetooth_experimental()
            stop_adapter_discovery()
        except Exception as exc:
            log.warning("bluetooth host prepare failed: %s", exc)
        try:
            import dbus  # noqa: F401
            from gi.repository import GLib  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            log.warning("BLE unavailable: %s", exc)
            return
        self._thread = threading.Thread(target=self._run, name="novarobot-ble", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        confirmer = self._confirmer
        self._confirmer = None
        if confirmer is not None:
            try:
                confirmer.stop()
            except Exception:
                pass
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:
                pass

    def _mark_linked(self) -> None:
        self.last_activity = time.monotonic()
        if not self.phone_linked:
            self.phone_linked = True
            log.info("BLE phone linked — App controls AP/STA mode")

    def app_active(self, lease_sec: int) -> bool:
        """App 最近 lease_sec 秒内读写过 GATT：期间状态机不自动切 AP。"""
        return self.phone_linked and (time.monotonic() - self.last_activity) < lease_sec

    def _notify_status(self) -> None:
        ch = self._status_char
        if ch is None:
            return
        try:
            from gi.repository import GLib

            # WriteValue 就在 BLE 主循环线程上，直接发。工作线程必须等这一包进空中再返回。
            if self._thread is not None and threading.current_thread() is self._thread:
                ch.send_notify()
                return
            done = threading.Event()

            def _send() -> bool:
                try:
                    ch.send_notify()
                finally:
                    done.set()
                return False

            GLib.idle_add(_send)
            done.wait(timeout=0.5)
        except Exception as exc:
            log.warning("BLE schedule notify failed: %s", exc)

    def _run(self) -> None:
        try:
            import dbus
            import dbus.exceptions
            import dbus.mainloop.glib
            import dbus.service
            from gi.repository import GLib
        except Exception as exc:
            self.error = str(exc)
            log.warning("BLE import failed: %s", exc)
            return

        class InvalidArgsException(dbus.exceptions.DBusException):
            _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"

        class InvalidOffsetException(dbus.exceptions.DBusException):
            _dbus_error_name = "org.bluez.Error.InvalidOffset"

        log.info("BLE thread starting")
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        self_outer = self
        app_path = "/org/bluez/novarobot/app"
        adv_path = "/org/bluez/novarobot/advertisement"

        try:
            adapter_obj = bus.get_object(BLUEZ, ADAPTER_PATH)
            adapter_props = dbus.Interface(adapter_obj, DBUS_PROP)
            adapter_iface = dbus.Interface(adapter_obj, ADAPTER_IFACE)
        except Exception as exc:
            self.error = f"adapter setup failed: {exc}"
            log.warning(self.error)
            return

        def prepare_adapter() -> None:
            try:
                adapter_iface.StopDiscovery()
            except Exception:
                pass
            adapter_props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
            adapter_props.Set(ADAPTER_IFACE, "Discoverable", dbus.Boolean(True))
            adapter_props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))
            adapter_props.Set(ADAPTER_IFACE, "Alias", self_outer.local_name)
            # 缩短可发现超时干扰；0=一直可发现
            try:
                adapter_props.Set(ADAPTER_IFACE, "DiscoverableTimeout", dbus.UInt32(0))
            except Exception:
                pass
            if enable_le_enhanced_conn_event():
                log.info("LE event mask set: 0x%016x (Enhanced Connection Complete on)", LE_EVENT_MASK)

        def maintain_adapter() -> None:
            """心跳只修补掉线，不再反复 Set Discoverable：广播期间会 Failed to set mode Busy。"""
            try:
                if not bool(adapter_props.Get(ADAPTER_IFACE, "Powered")):
                    adapter_props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
                alias = str(adapter_props.Get(ADAPTER_IFACE, "Alias") or "")
                if alias != self_outer.local_name:
                    adapter_props.Set(ADAPTER_IFACE, "Alias", self_outer.local_name)
            except Exception as exc:
                log.warning("BLE maintain adapter: %s", exc)
                self_outer.available = False
            try:
                active = int(adapter_props.Get(LE_ADV_MGR, "ActiveInstances"))
                if active < 1 and self_outer.available:
                    log.warning("BLE advertisement instance lost, will re-register")
                    self_outer.available = False
            except Exception:
                pass

        prepare_adapter()

        def _on_pair_state(pending: bool, addr: str | None) -> None:
            self_outer.pairing_pending = pending
            self_outer.pairing_device = addr

        try:
            from .ble_agent import PairingConfirmer
            from .power_button import PowerButton
            from .status_led import StatusLed

            led = StatusLed(
                self_outer.led_name_cfg,
                self_outer.led_on_ms,
                self_outer.led_off_ms,
                chip=self_outer.led_chip,
                line=self_outer.led_line,
            )
            button = PowerButton(short_press_max_ms=self_outer.short_press_max_ms)
            self_outer.led_name = led.name
            self_outer.power_button_path = button.path
            confirmer = PairingConfirmer(
                led,
                button,
                timeout_sec=self_outer.pair_timeout_sec,
                on_state=_on_pair_state,
            )
            confirmer.attach(bus, GLib)
            self_outer._confirmer = confirmer
            log.info(
                "BLE confirm hardware led=%s button=%s timeout=%ss",
                led.name,
                button.path,
                self_outer.pair_timeout_sec,
            )
        except Exception as exc:
            log.warning("BLE pairing agent/hardware attach failed: %s", exc)

        class Advertisement(dbus.service.Object):
            def __init__(self, bus_obj: dbus.Bus) -> None:
                dbus.service.Object.__init__(self, bus_obj, adv_path)

            def get_properties(self) -> dict[str, Any]:
                return {
                    LE_ADV: {
                        "Type": "peripheral",
                        "LocalName": dbus.String(self_outer.local_name),
                        "ServiceUUIDs": dbus.Array([SERVICE_UUID], signature="s"),
                        # 31 字节广播包里优先放名字；TxPower 会把 LocalName 挤进 scan response，App 扫到空名
                        "IncludeTxPower": dbus.Boolean(False),
                    }
                }

            @dbus.service.method(DBUS_PROP, in_signature="ss", out_signature="v")
            def Get(self, interface: str, prop: str) -> Any:
                props = self.get_properties().get(interface)
                if props is None or prop not in props:
                    raise InvalidArgsException()
                return props[prop]

            @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
            def GetAll(self, interface: str) -> dict[str, Any]:
                if interface != LE_ADV:
                    raise InvalidArgsException()
                return self.get_properties()[LE_ADV]

            @dbus.service.method(LE_ADV, in_signature="", out_signature="")
            def Release(self) -> None:
                log.info("BLE advertisement released by BlueZ")

        class Application(dbus.service.Object):
            def __init__(self, bus_obj: dbus.Bus) -> None:
                self.managed: list[Any] = []
                dbus.service.Object.__init__(self, bus_obj, app_path)

            def add(self, obj: Any) -> None:
                self.managed.append(obj)

            @dbus.service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
            def GetManagedObjects(self) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for obj in self.managed:
                    result[obj.get_path()] = obj.get_properties()
                return result

        class Service(dbus.service.Object):
            def __init__(self, bus_obj: dbus.Bus, index: int, uuid: str, primary: bool) -> None:
                self.path = f"{app_path}/service{index}"
                self.uuid = uuid
                self.primary = primary
                self.chars: list[Characteristic] = []
                dbus.service.Object.__init__(self, bus_obj, self.path)

            def get_path(self) -> Any:
                return dbus.ObjectPath(self.path)

            def get_properties(self) -> dict[str, Any]:
                return {
                    GATT_SERVICE: {
                        "UUID": self.uuid,
                        "Primary": dbus.Boolean(self.primary),
                        "Characteristics": dbus.Array([c.get_path() for c in self.chars], signature="o"),
                    }
                }

        class Characteristic(dbus.service.Object):
            def __init__(
                self,
                bus_obj: dbus.Bus,
                index: int,
                uuid: str,
                flags: list[str],
                service: Service,
            ) -> None:
                self.path = f"{service.path}/char{index}"
                self.uuid = uuid
                self.flags = flags
                self.service = service
                self.notifying = False
                self.pending_notify = False
                self.read_snapshot = b""
                dbus.service.Object.__init__(self, bus_obj, self.path)

            def get_path(self) -> Any:
                return dbus.ObjectPath(self.path)

            def get_properties(self) -> dict[str, Any]:
                return {
                    GATT_CHRC: {
                        "Service": self.service.get_path(),
                        "UUID": self.uuid,
                        "Flags": dbus.Array(self.flags, signature="s"),
                    }
                }

            @dbus.service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
            def GetAll(self, interface: str) -> dict[str, Any]:
                if interface != GATT_CHRC:
                    raise InvalidArgsException()
                return self.get_properties()[GATT_CHRC]

            @dbus.service.signal(DBUS_PROP, signature="sa{sv}as")
            def PropertiesChanged(self, interface: str, changed: dict[str, Any], invalidated: list[str]) -> None:
                pass

            @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
            def StartNotify(self) -> None:
                self.notifying = True
                log.info("BLE StartNotify %s", self.uuid)
                # App 常先写命令再订阅，订阅前跳过的那一包在这里补发（回复 StartNotify 之后再发）
                if self.pending_notify:
                    self.pending_notify = False
                    log.info("BLE flush pending notify after subscribe")
                    GLib.idle_add(self.send_notify)

            @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
            def StopNotify(self) -> None:
                self.notifying = False

            def send_notify(self) -> bool:
                if not self.notifying:
                    self.pending_notify = True
                    log.warning("BLE notify skipped, A004 not subscribed (will flush on subscribe)")
                    return False
                try:
                    raw = self._payload_bytes()
                    value = dbus.Array([dbus.Byte(b) for b in raw], signature="y")
                    self.PropertiesChanged(GATT_CHRC, {"Value": value}, [])
                    log.info("BLE A004 notify %s", raw.decode("utf-8", errors="replace"))
                except Exception as exc:
                    log.warning("BLE notify failed: %s", exc)
                    return False
                return False

            def _payload_bytes(self) -> bytes:
                # A002 始终回当前真实模式；A004 回最近命令结果（扫网/切模式）
                uuid = str(self.uuid or "").lower()
                if uuid == CHAR_INFO_UUID:
                    try:
                        info = self_outer.info_fn() or {}
                    except Exception as exc:
                        info = {"error": str(exc)}
                    if not isinstance(info, dict):
                        info = {"error": "bad info"}
                    return _fit_info(info)
                last = self_outer._last_result if isinstance(self_outer._last_result, dict) else {}
                if _is_scan_payload(last):
                    return _fit_json(last, PAYLOAD_LIMIT)
                if _is_mode_payload(last):
                    return _fit_mode(last)
                try:
                    info = self_outer.info_fn() or {}
                except Exception as exc:
                    info = {"error": str(exc)}
                if not isinstance(info, dict):
                    info = {"error": "bad info"}
                return _fit_info(info, last or None)

            @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
            def ReadValue(self, options: dict[str, Any]) -> dbus.Array:
                self_outer._mark_linked()
                # 超过 MTU 的长读由 BlueZ 拆成多次带 offset 的 ReadValue，且 BlueZ 不会替我们切片；
                # offset=0 时生成快照，后续分段都从同一份快照取，保证 App 拼出完整一致的 JSON
                offset = int(options.get("offset", 0))
                if offset == 0 or not self.read_snapshot:
                    self.read_snapshot = self._payload_bytes()
                if offset > len(self.read_snapshot):
                    raise InvalidOffsetException()
                return dbus.Array([dbus.Byte(b) for b in self.read_snapshot[offset:]], signature="y")

            @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
            def WriteValue(self, value: Any, options: dict[str, Any]) -> None:
                raw = bytes(int(b) for b in value).decode("utf-8", errors="ignore").strip()
                log.info("BLE command write: %s", raw)
                self_outer._mark_linked()
                try:
                    obj = json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise dbus.exceptions.DBusException(f"org.bluez.Error.Failed: bad json ({exc})")
                if not isinstance(obj, dict):
                    raise dbus.exceptions.DBusException("org.bluez.Error.Failed: json object required")

                cmd = str(obj.get("cmd") or "").strip().lower()
                ssid = str(obj.get("ssid") or "").strip()
                mode = str(obj.get("mode") or "").strip().lower()
                if cmd in {"wifi_scan", "scan"}:
                    self_outer._last_result = {"ok": True, "cmd": "wifi_scan", "type": "wifi_scan_busy"}
                    self_outer._notify_status()
                elif cmd in {"get_mode", "mode_query"}:
                    self_outer._last_result = {"ok": True, "cmd": "mode", "type": "mode_busy"}
                    self_outer._notify_status()
                elif mode in {"ap", "sta"} or cmd == "mode":
                    self_outer._last_result = {
                        "ok": True,
                        "cmd": "mode",
                        "type": "mode_busy",
                        "target": mode if mode in {"ap", "sta"} else "",
                    }
                    self_outer._notify_status()
                elif cmd in {"wifi_config", "provision"} or (ssid and cmd not in {"ping", "mode"}):
                    self_outer._last_result = {
                        "ok": True,
                        "cmd": "wifi_config",
                        "type": "wifi_config_busy",
                        "ssid": ssid,
                    }
                    self_outer._notify_status()

                def _worker() -> None:
                    try:
                        result = self_outer.command_fn(obj)
                        self_outer._last_result = result if isinstance(result, dict) else {"ok": True, "result": result}
                        logged = dict(self_outer._last_result) if isinstance(self_outer._last_result, dict) else self_outer._last_result
                        if isinstance(logged, dict) and "password" in logged:
                            logged = dict(logged)
                            logged["password"] = "***"
                        log.info("BLE command result: %s", logged)
                    except Exception as exc:
                        self_outer._last_result = {"ok": False, "error": str(exc)}
                        log.exception("BLE command failed")
                    # 开 AP 后蓝牙数据通道不可用，成功改由手机连上热点判定，不再补发 mode_result。
                    if isinstance(self_outer._last_result, dict) and self_outer._last_result.get("confirm") == "wifi":
                        log.info("AP switch finished; skip BLE mode_result, success is joining AP Wi-Fi")
                        return
                    self_outer._notify_status()

                threading.Thread(target=_worker, daemon=True, name="novarobot-ble-cmd").start()

        Advertisement(bus)
        application = Application(bus)
        service = Service(bus, 0, SERVICE_UUID, True)
        # 全部要求加密链路：未配对手机首次访问会触发系统配对，由电源键确认；已配对手机重连自动加密
        char_info = Characteristic(bus, 0, CHAR_INFO_UUID, ["encrypt-read"], service)
        char_wifi = Characteristic(bus, 1, CHAR_WIFI_UUID, ["encrypt-write", "write-without-response"], service)
        char_status = Characteristic(bus, 2, CHAR_STATUS_UUID, ["encrypt-read", "encrypt-notify"], service)
        self_outer._status_char = char_status
        service.chars = [char_info, char_wifi, char_status]
        application.add(service)
        application.add(char_info)
        application.add(char_wifi)
        application.add(char_status)

        gatt_mgr = dbus.Interface(adapter_obj, GATT_MGR)
        adv_mgr = dbus.Interface(adapter_obj, LE_ADV_MGR)

        def cleanup() -> None:
            try:
                gatt_mgr.UnregisterApplication(dbus.ObjectPath(app_path))
            except Exception:
                pass
            try:
                adv_mgr.UnregisterAdvertisement(dbus.ObjectPath(adv_path))
            except Exception:
                pass

        def do_register() -> bool:
            cleanup()
            prepare_adapter()
            try:
                # 异步回调依赖正在跑的 GLib 主循环
                gatt_mgr.RegisterApplication(
                    dbus.ObjectPath(app_path),
                    {},
                    reply_handler=lambda: log.info("GATT application registered"),
                    error_handler=lambda e: log.warning("GATT register error: %s", e),
                )
                adv_mgr.RegisterAdvertisement(
                    dbus.ObjectPath(adv_path),
                    {},
                    reply_handler=lambda: _on_adv_ok(),
                    error_handler=lambda e: _on_adv_err(e),
                )
            except Exception as exc:
                self_outer.error = f"register failed: {exc}"
                self_outer.available = False
                log.warning(self_outer.error)
                GLib.timeout_add_seconds(5, do_register)
            return False

        def _on_adv_ok() -> None:
            self_outer.available = True
            self_outer.error = None
            log.info("BLE advertising as %s service=%s", self_outer.local_name, SERVICE_UUID)

        def _on_adv_err(exc: Any) -> None:
            self_outer.available = False
            self_outer.error = f"register failed: {exc}"
            log.warning(self_outer.error)
            GLib.timeout_add_seconds(5, do_register)

        def heartbeat() -> bool:
            maintain_adapter()
            # 适配器可能被外部复位（重启 bluetoothd、上下电），掩码随之恢复成内核默认值
            enable_le_enhanced_conn_event()
            try:
                confirmer = self_outer._confirmer
                if confirmer is not None:
                    confirmer.ensure_default()
            except Exception:
                pass
            # 若广播丢了则重注册
            if not self_outer.available:
                do_register()
            return True

        self._loop = GLib.MainLoop()
        GLib.timeout_add(300, do_register)  # 主循环起来后再注册
        GLib.timeout_add_seconds(15, heartbeat)
        self._loop.run()
