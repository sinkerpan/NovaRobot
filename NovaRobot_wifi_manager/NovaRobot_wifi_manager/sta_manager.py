from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import network

log = logging.getLogger("novarobot.sta")

PHONE_HINTS = ("iphone", "android", "redmi", "iqoo", "xiaomi", "oppo", "vivo", "oneplus", "pixel")


class StaManager:
    def __init__(self, iface: str) -> None:
        self.iface = iface
        self.last_error = ""

    def is_connected(self) -> bool:
        active = network.nm_active_wifi()
        ip = network.iface_ipv4(self.iface)
        return bool(active and ip and network.wifi_mode(self.iface) == "sta")

    def disconnect(self) -> None:
        """立刻释放 STA，避免切 AP 时还被 NetworkManager 占着网卡。"""
        if network.wifi_mode(self.iface) != "sta" and not network.nm_active_wifi():
            return
        log.info("disconnect STA on %s", self.iface)
        network.run(["nmcli", "device", "disconnect", self.iface], timeout=6)
        deadline = time.time() + 2
        while time.time() < deadline:
            if not network.nm_active_wifi():
                return
            time.sleep(0.1)

    def current(self) -> dict[str, Any]:
        active = network.nm_active_wifi()
        return {
            "connected": self.is_connected(),
            "ssid": network.current_ssid(self.iface),
            "ip": network.iface_ipv4(self.iface),
            "connection": None if not active else active["name"],
        }

    def saved_profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for item in network.nm_wifi_connections():
            ssid = network.nm_wifi_ssid(item["name"]) or item["name"]
            name_l = item["name"].lower()
            ssid_l = ssid.lower()
            is_phone = any(h in name_l or h in ssid_l for h in PHONE_HINTS)
            profiles.append(
                {
                    "name": item["name"],
                    "ssid": ssid,
                    "priority": 10 if not is_phone else 5,
                    "kind": "hotspot" if is_phone else "home",
                }
            )
        profiles.sort(key=lambda p: p["priority"], reverse=True)
        return profiles

    def has_saved(self) -> bool:
        return bool(self.saved_profiles())

    def ensure_managed(self) -> None:
        # AP 模式会写 unmanaged conf；切回 STA 时必须删掉，否则 NM 永远不用 Wi-Fi 口
        unmanaged = Path("/etc/NetworkManager/conf.d/99-unmanaged-NovaRobot-wifi-manager.conf")
        if unmanaged.exists():
            try:
                unmanaged.unlink()
                network.run(["systemctl", "reload", "NetworkManager"])
                time.sleep(1)
            except OSError as exc:
                log.warning("remove unmanaged conf failed: %s", exc)
        network.run(["nmcli", "device", "set", self.iface, "managed", "yes"])
        network.run(["nmcli", "radio", "wifi", "on"])
        network.run(["rfkill", "unblock", "wifi"])
        # 等 NM 重新认设备
        deadline = time.time() + 8
        while time.time() < deadline:
            result = network.run(["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"])
            for line in result.stdout.splitlines():
                if line.startswith(f"{self.iface}:") and "unmanaged" not in line.lower() and "未托管" not in line:
                    return
            time.sleep(0.5)
        log.warning("wifi iface %s still not managed by NM", self.iface)

    def connect(self, con_name: str, timeout_sec: int = 25) -> bool:
        self.ensure_managed()
        log.info("connecting STA profile %s", con_name)
        self.last_error = ""
        result = network.run(["nmcli", "connection", "up", con_name], timeout=timeout_sec + 5)
        if result.returncode != 0:
            self.last_error = result.stderr.strip()
            log.warning("nmcli up %s failed: %s", con_name, self.last_error)
            return False
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self.is_connected():
                log.info("STA connected: %s ip=%s", network.current_ssid(self.iface), network.iface_ipv4(self.iface))
                return True
            time.sleep(1)
        self.last_error = "no ip"
        return False

    def connect_ssid(self, ssid: str, timeout_sec: int = 25) -> bool:
        for profile in self.saved_profiles():
            if profile["ssid"] == ssid:
                return self.connect(profile["name"], timeout_sec=timeout_sec)
        return False

    def wait_visible(self, ssid: str, timeout_sec: int = 15) -> bool:
        """iPhone 个人热点只在设置页打开时广播，先反复扫描等它出现再连。"""
        if ssid in network.visible_ssids(self.iface):
            return True
        deadline = time.time() + timeout_sec
        while True:
            network.run(["nmcli", "device", "wifi", "rescan", "ifname", self.iface, "ssid", ssid], timeout=10)
            time.sleep(2)
            if ssid in network.visible_ssids(self.iface):
                return True
            if time.time() >= deadline:
                log.warning("SSID %s not visible after %ss", ssid, timeout_sec)
                return False

    def _fail_reason(self, visible: bool) -> str:
        err = (self.last_error or "").lower()
        if "secrets" in err or "password" in err or "psk" in err:
            return "auth"
        if not visible or "no network with ssid" in err:
            return "not_found"
        return "timeout"

    def connect_best(self, preferred_ssid: str | None, timeout_sec: int = 25) -> bool:
        if self.is_connected():
            return True
        profiles = self.saved_profiles()
        if preferred_ssid:
            profiles.sort(key=lambda p: 0 if p["ssid"] == preferred_ssid else 1)
        for profile in profiles:
            if self.connect(profile["name"], timeout_sec=timeout_sec):
                return True
        return False

    def provision(self, ssid: str, password: str, timeout_sec: int = 25) -> tuple[bool, str]:
        """返回 (是否连上, 失败原因 not_found / auth / timeout / add_failed)。"""
        self.ensure_managed()
        visible = self.wait_visible(ssid)
        if not visible:
            # 扫不到就不动当前连接，避免把正在用的 Wi-Fi 断掉
            return False, "not_found"
        # 已保存网络：密码可留空，直接 up 已有 NM 配置
        if not password:
            saved = [p for p in self.saved_profiles() if p["ssid"] == ssid]
            for profile in saved:
                if self.connect(profile["name"], timeout_sec=timeout_sec):
                    return True, ""
            if saved:
                return False, self._fail_reason(visible)
        con_name = f"novarobot-{ssid}"
        network.run(["nmcli", "connection", "delete", con_name])
        cmd = [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            self.iface,
            "con-name",
            con_name,
            "ssid",
            ssid,
            "ipv4.method",
            "auto",
            "ipv6.method",
            "ignore",
            "connection.autoconnect",
            "yes",
        ]
        if password:
            cmd.extend(["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
        else:
            cmd.extend(["wifi-sec.key-mgmt", "none"])
        add = network.run(cmd)
        if add.returncode != 0:
            log.warning("add wifi failed: %s", add.stderr.strip())
            return False, "add_failed"
        if self.connect(con_name, timeout_sec=timeout_sec):
            return True, ""
        return False, self._fail_reason(visible)
