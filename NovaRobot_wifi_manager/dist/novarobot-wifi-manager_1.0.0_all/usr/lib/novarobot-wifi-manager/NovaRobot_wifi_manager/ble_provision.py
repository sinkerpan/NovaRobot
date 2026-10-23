from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

log = logging.getLogger("novarobot.ble")

PAYLOAD_LIMIT = 480


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
    current = str(data.get("current_mode") or data.get("mode") or "")
    payload: dict[str, Any] = {
        "ok": bool(data.get("ok", True)),
        "cmd": "mode",
        "type": str(data.get("type") or "mode_result"),
        "mode": current,
        "current_mode": current,
        "ip": data.get("ip") or "",
        "ap_ssid": data.get("ap_ssid") or "",
        "ssid": data.get("ssid") or "",
    }
    if data.get("target"):
        payload["target"] = str(data["target"])
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
    手机成功写过任意命令后 phone_linked=True，状态机不再自动抢模式。
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
        if not self.phone_linked:
            self.phone_linked = True
            log.info("BLE phone linked — App controls AP/STA mode")

    def _notify_status(self) -> None:
        ch = self._status_char
        if ch is None:
            return
        try:
            from gi.repository import GLib

            GLib.idle_add(ch.send_notify)
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
                        "IncludeTxPower": dbus.Boolean(True),
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

            @dbus.service.method(GATT_CHRC, in_signature="", out_signature="")
            def StopNotify(self) -> None:
                self.notifying = False

            def send_notify(self) -> bool:
                if not self.notifying:
                    return False
                try:
                    value = dbus.Array([dbus.Byte(b) for b in self._payload_bytes()], signature="y")
                    self.PropertiesChanged(GATT_CHRC, {"Value": value}, [])
                except Exception as exc:
                    log.warning("BLE notify failed: %s", exc)
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
                    raw = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    return raw[:PAYLOAD_LIMIT]
                last = self_outer._last_result if isinstance(self_outer._last_result, dict) else {}
                if _is_scan_payload(last):
                    return _fit_json(last, PAYLOAD_LIMIT)
                if _is_mode_payload(last):
                    return _fit_mode(last)
                payload: dict[str, Any] = {}
                if last:
                    payload["ok"] = last.get("ok")
                    payload["ble_result"] = last
                try:
                    info = self_outer.info_fn() or {}
                except Exception as exc:
                    info = {"error": str(exc)}
                if isinstance(info, dict):
                    payload.update(info)
                if last:
                    payload["ble_result"] = last
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                return raw[:PAYLOAD_LIMIT]

            @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
            def ReadValue(self, options: dict[str, Any]) -> dbus.Array:
                self_outer._mark_linked()
                return dbus.Array([dbus.Byte(b) for b in self._payload_bytes()], signature="y")

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
                        log.info("BLE command result: %s", self_outer._last_result)
                    except Exception as exc:
                        self_outer._last_result = {"ok": False, "error": str(exc)}
                        log.exception("BLE command failed")
                    self_outer._notify_status()

                threading.Thread(target=_worker, daemon=True, name="novarobot-ble-cmd").start()

        Advertisement(bus)
        application = Application(bus)
        service = Service(bus, 0, SERVICE_UUID, True)
        char_info = Characteristic(bus, 0, CHAR_INFO_UUID, ["read"], service)
        char_wifi = Characteristic(bus, 1, CHAR_WIFI_UUID, ["write", "write-without-response"], service)
        char_status = Characteristic(bus, 2, CHAR_STATUS_UUID, ["read", "notify"], service)
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
            try:
                prepare_adapter()
            except Exception as exc:
                log.warning("BLE heartbeat adapter: %s", exc)
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
