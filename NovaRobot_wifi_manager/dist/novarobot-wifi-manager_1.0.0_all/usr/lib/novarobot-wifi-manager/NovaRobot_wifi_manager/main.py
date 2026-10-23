from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Any

from .ap_manager import ApManager
from .ble_provision import SERVICE_UUID, BleProvision
from .config_store import ConfigStore, load_default
from .http_api import HttpApi
from .identity import ap_ssid, device_id, mdns_hostname, read_mac, resolve_wifi_iface
from .mdns import MdnsPublisher
from . import network
from .sta_manager import StaManager
from .state_machine import NetworkStateMachine
from .udp_discovery import UdpDiscovery

log = logging.getLogger("novarobot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class NovaRobotWifiManager:
    def __init__(self) -> None:
        self.cfg = load_default()
        self.store = ConfigStore(self.cfg)
        self.iface = resolve_wifi_iface(str(self.cfg.get("iface") or ""))
        self.cfg["iface"] = self.iface
        self.dev_id = device_id(self.iface)
        self.ssid = ap_ssid(self.iface)
        self.mac = read_mac(self.iface)
        host = str(self.cfg.get("mdns_hostname") or "auto").strip()
        if host in {"", "auto"}:
            host = mdns_hostname(self.iface)
        self.mdns_host = host
        self.sta = StaManager(self.iface)
        self.ap = ApManager(self.cfg, self.ssid)
        self.mdns = MdnsPublisher(self.mdns_host, self.cfg["service_type"], int(self.cfg["http_port"]))
        self.fsm = NetworkStateMachine(self.cfg, self.sta, self.ap, self.store, on_change=self._on_state)
        self.udp = UdpDiscovery(int(self.cfg["udp_port"]), self.discover_payload)
        self.http = HttpApi(
            int(self.cfg["http_port"]),
            status_fn=self.discover_payload,
            provision_fn=self.provision_wifi,
            scan_fn=lambda: network.scan_wifi(self.iface),
            mode_fn=self.set_mode,
        )
        self.ble = BleProvision(
            self.ssid,
            self.discover_payload,
            self.handle_ble_command,
            pair_timeout_sec=int(self.cfg.get("ble_pair_timeout_sec", 30)),
            short_press_max_ms=int(self.cfg.get("ble_pair_short_press_max_ms", 1500)),
            led_name=str(self.cfg.get("ble_led_name", "auto")),
            led_on_ms=int(self.cfg.get("ble_led_on_ms", 80)),
            led_off_ms=int(self.cfg.get("ble_led_off_ms", 80)),
            led_chip=str(self.cfg.get("ble_led_chip", "gpiochip1")),
            led_line=int(self.cfg.get("ble_led_line", 13)),
        )
        self.fsm.app_control_fn = lambda: self.ble.phone_linked
        self._persist_identity()
        self._import_existing_profiles()

    def _persist_identity(self) -> None:
        self.store.save_identity(
            {
                "device": self.cfg["device_name"],
                "id": self.dev_id,
                "ap_ssid": self.ssid,
                "mac": self.mac,
                "mdns": f"{self.mdns_host}.local",
                "firmware": self.cfg["firmware"],
            }
        )

    def _import_existing_profiles(self) -> None:
        data = self.store.load_wifi()
        existing = {p.get("ssid") for p in data.get("profiles", [])}
        current = self.sta.current().get("ssid")
        for profile in self.sta.saved_profiles():
            if profile["ssid"] in existing:
                continue
            data.setdefault("profiles", []).append(
                {
                    "ssid": profile["ssid"],
                    "priority": profile["priority"],
                    "source": "imported",
                    "kind": profile["kind"],
                }
            )
            existing.add(profile["ssid"])
        if current and not data.get("preferred_ssid"):
            data["preferred_ssid"] = current
        self.store.save_wifi(data)

    def current_ip(self) -> str | None:
        return network.iface_ipv4(self.iface) or network.first_ipv4()

    def discover_payload(self) -> dict[str, Any]:
        sta = self.sta.current()
        mode = "ap" if self.ap.is_active() else "sta" if sta["connected"] else "offline"
        ip = self.current_ip()
        return {
            "device": self.cfg["device_name"],
            "id": self.dev_id,
            "model": self.cfg["device_name"],
            "ip": ip,
            "battery": None,
            "firmware": self.cfg["firmware"],
            "mode": mode,
            "state": self.fsm.state,
            "ssid": self.ssid if mode == "ap" else sta.get("ssid"),
            "ap_ssid": self.ssid,
            "mdns": f"{self.mdns_host}.local",
            "http": f"http://{ip}:{self.cfg['http_port']}" if ip else None,
            "udp_port": int(self.cfg["udp_port"]),
            "ble": {
                "name": self.ssid,
                "service": SERVICE_UUID,
                "available": self.ble.available,
                "phone_linked": self.ble.phone_linked,
                "error": self.ble.error,
                "pairing_pending": self.ble.pairing_pending,
                "pairing_device": self.ble.pairing_device,
                "led": self.ble.led_name,
                "power_button": self.ble.power_button_path,
                "chars": {
                    "info": "0000a002-0000-1000-8000-00805f9b34fb",
                    "command": "0000a003-0000-1000-8000-00805f9b34fb",
                    "status": "0000a004-0000-1000-8000-00805f9b34fb",
                },
            },
            "mac": self.mac,
            "saved_wifi": [p.get("ssid") for p in self.sta.saved_profiles() if p.get("ssid")],
            "app_controls_mode": self.ble.phone_linked,
        }

    def handle_ble_command(self, obj: dict[str, Any]) -> dict[str, Any]:
        """App via BLE a003: wifi_scan / wifi_config / mode / ping."""
        cmd = str(obj.get("cmd") or "").strip().lower()
        mode = str(obj.get("mode") or "").strip().lower()
        ssid = str(obj.get("ssid") or "").strip()
        password = str(obj.get("password") or "")

        if cmd in {"wifi_scan", "scan"}:
            return self.ble_wifi_scan()

        if cmd == "ping" or obj.get("ping"):
            return {"ok": True, "pong": True, "cmd": "ping"}

        if cmd in {"get_mode", "mode_query"}:
            return self.current_mode_result()

        if mode in {"ap", "sta"}:
            return self.set_mode(mode)

        if cmd == "mode" and mode in {"ap", "sta"}:
            return self.set_mode(mode)

        if cmd in {"wifi_config", "provision"} or ssid:
            if not ssid:
                return {"ok": False, "cmd": "wifi_config", "error": "ssid required"}
            result = dict(self.provision_wifi(ssid, password))
            result.setdefault("cmd", "wifi_config")
            return result

        return {"ok": False, "error": "expected {cmd:wifi_scan}|{cmd:get_mode}|{mode:ap|sta}|{ssid,password}"}

    def ble_wifi_scan(self) -> dict[str, Any]:
        """Scan nearby APs for the phone. Same source as HTTP GET /api/wifi/scan."""
        log.info("BLE wifi_scan start iface=%s", self.iface)
        try:
            nets = network.scan_wifi(self.iface)
        except Exception as exc:
            log.exception("BLE wifi_scan failed")
            return {
                "ok": False,
                "cmd": "wifi_scan",
                "type": "wifi_scan_result",
                "error": str(exc),
                "networks": [],
            }
        saved = [p.get("ssid") for p in self.sta.saved_profiles() if p.get("ssid")]
        saved_set = set(saved)
        compact: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in nets:
            name = str(item.get("ssid") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            signal = int(item.get("signal") or 0)
            compact.append(
                {
                    "ssid": name,
                    "signal": signal,
                    "rssi": signal,
                    "security": str(item.get("security") or ""),
                    "saved": name in saved_set or bool(item.get("saved")),
                    "in_use": bool(item.get("in_use")),
                }
            )
        for name in saved:
            if name in seen:
                continue
            compact.append(
                {
                    "ssid": name,
                    "signal": 0,
                    "rssi": 0,
                    "security": "已保存",
                    "saved": True,
                    "in_use": False,
                }
            )
            seen.add(name)
        log.info("BLE wifi_scan done count=%s", len(compact))
        return {
            "ok": True,
            "cmd": "wifi_scan",
            "type": "wifi_scan_result",
            "networks": compact,
            "saved_wifi": saved,
        }

    def current_mode_result(self) -> dict[str, Any]:
        """App 读当前模式：A004 / HTTP 共用，mode 必须是现场值。"""
        snap = self.discover_payload()
        current = str(snap.get("mode") or "offline")
        return {
            "ok": True,
            "cmd": "mode",
            "type": "mode_result",
            "mode": current,
            "current_mode": current,
            "ip": snap.get("ip") or "",
            "ap_ssid": self.ssid,
            "ssid": "" if current == "ap" else str(snap.get("ssid") or ""),
        }

    def set_mode(self, mode: str) -> dict[str, Any]:
        mode = mode.strip().lower()
        log.info("set_mode=%s (app/http/ble)", mode)
        if mode == "ap":
            self.fsm.clear_requested_mode()
            ok = self.ap.start()
            if ok:
                self.fsm.note_external("AP_MODE", "app/ble AP")
            else:
                self.fsm.note_external("RECONNECT", "AP start failed")
            self._refresh_mdns()
            result = self.current_mode_result()
            result["ok"] = bool(ok and result.get("mode") == "ap")
            result["target"] = "ap"
            if not result["ok"]:
                result["error"] = "AP start failed"
            return result
        if mode == "sta":
            self.fsm.clear_requested_mode()
            if self.ap.is_active():
                self.ap.stop()
            preferred = self.store.load_wifi().get("preferred_ssid")
            ok = self.sta.connect_best(preferred, timeout_sec=int(self.cfg["sta_timeout_sec"]))
            if ok:
                self.fsm.note_external("CONNECTED", f"app/ble STA {self.sta.current().get('ssid')}")
            else:
                self.fsm.note_external("RECONNECT", "STA connect failed")
            self._refresh_mdns()
            result = self.current_mode_result()
            result["ok"] = bool(ok and result.get("mode") == "sta")
            result["target"] = "sta"
            if not result["ok"]:
                result["error"] = "STA connect failed"
            return result
        return {"ok": False, "cmd": "mode", "type": "mode_result", "error": "mode must be ap or sta"}

    def provision_wifi(self, ssid: str, password: str) -> dict[str, Any]:
        log.info("provision wifi ssid=%s password=%s", ssid, "yes" if password else "reuse-saved")
        if password:
            self.store.add_profile(ssid, password, priority=30)
        else:
            self.store.add_profile(ssid, None, priority=30)
        if self.ap.is_active():
            self.ap.stop()
        ok = self.sta.provision(ssid, password, timeout_sec=int(self.cfg["sta_timeout_sec"]))
        if ok:
            self.fsm.note_external("CONNECTED", f"provisioned {ssid}")
            self._refresh_mdns()
            return {"ok": True, "state": "CONNECTED", "mode": "sta", "ssid": ssid, "ip": self.current_ip()}
        # 配网失败：同步启 AP，避免 FSM 再双开
        self.fsm.clear_requested_mode()
        ap_ok = self.ap.start()
        if ap_ok:
            self.fsm.note_external("AP_MODE", "provision failed, back to AP")
        else:
            self.fsm.note_external("RECONNECT", "provision failed and AP start failed")
        self._refresh_mdns()
        return {"ok": False, "error": "connect failed", "ssid": ssid, "mode": "ap" if ap_ok else "offline"}

    def _on_state(self, state: str, snap: dict[str, Any]) -> None:
        self._refresh_mdns()

    def _refresh_mdns(self) -> None:
        payload = self.discover_payload()
        self.mdns.publish(
            payload.get("ip"),
            {
                "id": payload["id"],
                "fw": payload["firmware"],
                "mode": payload["mode"],
            },
        )

    def start(self) -> None:
        self.http.start()
        self.udp.start()
        self.ble.start()
        self.fsm.start()
        self._refresh_mdns()
        log.info("NovaRobot_wifi_manager started id=%s ssid=%s ip=%s", self.dev_id, self.ssid, self.current_ip())

    def stop(self) -> None:
        self.fsm.stop()
        self.udp.stop()
        self.http.stop()
        self.ble.stop()
        self.mdns.stop()


def main() -> int:
    setup_logging()
    if len(sys.argv) > 1 and sys.argv[1] in {"status", "discover"}:
        cfg = load_default()
        iface = cfg["iface"]
        print(
            json.dumps(
                {
                    "id": device_id(iface),
                    "ap_ssid": ap_ssid(iface),
                    "ip": network.iface_ipv4(iface),
                    "ssid": network.current_ssid(iface),
                    "mode": network.wifi_mode(iface),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    mgr = NovaRobotWifiManager()
    stop = False

    def _handle(signum: int, frame: Any) -> None:
        nonlocal stop
        stop = True
        log.info("signal %s, stopping", signum)
        mgr.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    mgr.start()
    while not stop:
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
