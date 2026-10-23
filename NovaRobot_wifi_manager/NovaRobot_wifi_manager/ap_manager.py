from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import network

log = logging.getLogger("novarobot.ap")

HOSTAPD_CONF = Path("/etc/hostapd/NovaRobot-wifi-manager.conf")
DNSMASQ_PID = Path("/run/NovaRobot-wifi-manager-dnsmasq.pid")
HOSTAPD_PID = Path("/run/NovaRobot-wifi-manager-hostapd.pid")
LEASES = Path("/var/lib/misc/NovaRobot-wifi-manager.leases")
NM_UNMANAGED = Path("/etc/NetworkManager/conf.d/99-unmanaged-NovaRobot-wifi-manager.conf")


class ApManager:
    def __init__(self, cfg: dict[str, Any], ssid: str) -> None:
        self.cfg = cfg
        self.iface = cfg["iface"]
        self.ssid = ssid
        self.password = cfg["ap_password"]
        self.ip = cfg["ap_ip"]
        self.channel = int(cfg["ap_channel"])
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        return network.wifi_mode(self.iface) == "ap" and bool(network.iface_ipv4(self.iface))

    def client_count(self) -> int:
        """已关联到热点的站点。手机连上 AP 才算开热点成功。"""
        if not self.is_active():
            return 0
        proc = network.run(["iw", "dev", self.iface, "station", "dump"], timeout=3)
        text_out = proc.stdout or ""
        return sum(1 for line in text_out.splitlines() if line.startswith("Station "))

    def client_connected(self) -> bool:
        return self.client_count() > 0

    def start(self) -> bool:
        # 串行化：避免 BLE set_mode 与 FSM request_mode 双开 hostapd
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.is_active():
            log.info("AP already up: %s %s", self.ssid, network.iface_ipv4(self.iface))
            return True
        t0 = time.time()
        log.info("starting robot AP %s on %s", self.ssid, self.iface)
        # 先踢掉 STA，否则 NM reload / unmanaged 会卡满数秒
        network.run(["nmcli", "device", "disconnect", self.iface], timeout=6)
        self._write_hostapd()
        self._unmanage_iface()
        # 清掉可能残留的 hostapd / 半残 AP，避免 Match already configured
        self._stop_radio_stack()
        network.run(["ip", "link", "set", self.iface, "down"], timeout=5)
        network.run(["ip", "addr", "flush", "dev", self.iface], timeout=5)
        network.run(["iw", "dev", self.iface, "set", "type", "managed"], timeout=5)
        network.run(["ip", "addr", "add", f"{self.ip}/24", "brd", "+", "dev", self.iface], timeout=5)
        network.run(["ip", "link", "set", self.iface, "up"], timeout=5)
        network.run(["rfkill", "unblock", "wifi"], timeout=5)
        self._start_dnsmasq()
        ok_hostapd = self._start_hostapd()
        if not ok_hostapd:
            log.error("hostapd process failed to start elapsed=%.2fs", time.time() - t0)
            return False
        deadline = time.time() + 6
        while time.time() < deadline:
            if self.is_active():
                log.info("AP is up: %s %s elapsed=%.2fs", self.ssid, self.ip, time.time() - t0)
                self._recover_bluetooth()
                return True
            time.sleep(0.15)
        log.error("AP failed to enter hostapd AP mode elapsed=%.2fs", time.time() - t0)
        # 失败时不要留半残 hostapd
        self._stop_radio_stack()
        return False

    def stop(self) -> None:
        with self._lock:
            log.info("stopping robot AP")
            self._stop_radio_stack()
            if NM_UNMANAGED.exists():
                try:
                    NM_UNMANAGED.unlink()
                except OSError:
                    pass
                network.run(["systemctl", "reload", "NetworkManager"])
            network.run(["ip", "addr", "flush", "dev", self.iface])
            network.run(["nmcli", "device", "set", self.iface, "managed", "yes"])
            network.run(["nmcli", "radio", "wifi", "on"])

    def info(self) -> dict[str, Any]:
        return {
            "active": self.is_active(),
            "ssid": self.ssid,
            "password": self.password,
            "ip": self.ip,
            "channel": self.channel,
        }

    def _write_hostapd(self) -> None:
        HOSTAPD_CONF.parent.mkdir(parents=True, exist_ok=True)
        HOSTAPD_CONF.write_text(
            "\n".join(
                [
                    f"interface={self.iface}",
                    "driver=nl80211",
                    f"ssid={self.ssid}",
                    "country_code=CN",
                    "ieee80211d=0",
                    # 这张 AX200 的 5GHz AP（hostapd）起不来，热点固定走 2.4G。
                    "hw_mode=g" if self.channel <= 14 else "hw_mode=a",
                    f"channel={self.channel}",
                    "beacon_int=100",
                    "dtim_period=2",
                    "max_num_sta=16",
                    "ieee80211n=1",
                    "wmm_enabled=1",
                    # 5GHz（AX200 Band2）没有 RX STBC；写 RX-STBC1 会让 hostapd 立刻退出
                    (
                        "ht_capab=[HT20][SHORT-GI-20]"
                        if self.channel > 14
                        else "ht_capab=[HT20][SHORT-GI-20][RX-STBC1]"
                    ),
                    "auth_algs=1",
                    "ignore_broadcast_ssid=0",
                    "ap_isolate=0",
                    "disassoc_low_ack=0",
                    "wpa=2",
                    f"wpa_passphrase={self.password}",
                    "wpa_key_mgmt=WPA-PSK",
                    "rsn_pairwise=CCMP",
                    "ieee80211w=0",
                    "logger_syslog=-1",
                    "logger_syslog_level=2",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _recover_bluetooth(self) -> None:
        """2.4G AP 和蓝牙共用天线。热点起来后把 hci0 重新打开，避免必须整机重启。"""
        network.run(["rfkill", "unblock", "bluetooth"], timeout=5)
        up = network.run(["hciconfig", "hci0", "up"], timeout=5)
        if up.returncode != 0:
            log.warning("hci0 up: %s", (up.stderr or up.stdout or "").strip())
        else:
            log.info("bluetooth hci0 brought back up after AP start")

    def _nm_unmanaged(self) -> bool:
        st = network.run(["nmcli", "-g", "GENERAL.STATE", "device", "show", self.iface], timeout=5)
        text = f"{st.stdout or ''} {st.stderr or ''}".lower()
        return "unmanaged" in text or "未托管" in text

    def _unmanage_iface(self) -> None:
        if self._nm_unmanaged():
            return
        NM_UNMANAGED.parent.mkdir(parents=True, exist_ok=True)
        wanted = "[keyfile]\n" f"unmanaged-devices=interface-name:{self.iface}\n"
        already = NM_UNMANAGED.exists() and NM_UNMANAGED.read_text(encoding="utf-8") == wanted
        if not already:
            NM_UNMANAGED.write_text(wanted, encoding="utf-8")
        # 先直接松手；多数情况不用 reload 整个 NetworkManager
        network.run(["nmcli", "device", "set", self.iface, "managed", "no"], timeout=6)
        if self._nm_unmanaged():
            return
        network.run(["systemctl", "reload", "NetworkManager"], timeout=8)
        deadline = time.time() + 3
        while time.time() < deadline:
            if self._nm_unmanaged():
                return
            time.sleep(0.15)
        log.warning("NM still managing %s after unmanage", self.iface)

    def _stop_radio_stack(self) -> None:
        self._kill_pidfile(HOSTAPD_PID)
        self._kill_pidfile(DNSMASQ_PID)
        # 只杀我们的 hostapd 配置进程；避免误伤系统其它实例时仍兜底
        network.run(["pkill", "-f", f"hostapd.*{HOSTAPD_CONF}"], timeout=5)
        network.run(["pkill", "-f", "NovaRobot-wifi-manager-dnsmasq"], timeout=5)
        if network.wifi_mode(self.iface) != "ap":
            return
        deadline = time.time() + 2
        while time.time() < deadline:
            if network.wifi_mode(self.iface) != "ap":
                return
            time.sleep(0.1)

    def _start_dnsmasq(self) -> None:
        self._kill_pidfile(DNSMASQ_PID)
        LEASES.parent.mkdir(parents=True, exist_ok=True)
        result = network.run(
            [
                "dnsmasq",
                "--conf-file=/dev/null",
                f"--interface={self.iface}",
                "--bind-interfaces",
                "--except-interface=lo",
                f"--listen-address={self.ip}",
                f"--dhcp-range={self.cfg['dhcp_start']},{self.cfg['dhcp_end']},12h",
                f"--dhcp-option=3,{self.ip}",
                f"--dhcp-option=6,{self.ip}",
                "--no-resolv",
                "--server=223.5.5.5",
                f"--pid-file={DNSMASQ_PID}",
                f"--dhcp-leasefile={LEASES}",
            ]
        )
        if result.returncode != 0:
            log.warning("dnsmasq: %s", result.stderr.strip())

    def _start_hostapd(self) -> bool:
        old = network.run(["systemctl", "is-active", "nimo-hotspot.service"], timeout=3)
        if (old.stdout or "").strip() == "active":
            network.run(["systemctl", "stop", "nimo-hotspot.service"], timeout=6)
        # 前台捕获错误再 daemon；-B 时失败信息常在 stdout
        proc = network.run(
            [
                "hostapd",
                "-B",
                "-P",
                str(HOSTAPD_PID),
                str(HOSTAPD_CONF),
            ],
            timeout=8,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            log.warning("hostapd exit=%s stdout=%s stderr=%s", proc.returncode, out, err)
            return False
        if out or err:
            # Match already configured 等可能仍 return 0 后立刻退出
            log.info("hostapd: %s %s", out, err)
        deadline = time.time() + 1.2
        while time.time() < deadline:
            if HOSTAPD_PID.exists():
                try:
                    pid = int(HOSTAPD_PID.read_text(encoding="utf-8").strip())
                    os.kill(pid, 0)
                    return True
                except (OSError, ValueError):
                    pass
            if network.wifi_mode(self.iface) == "ap":
                return True
            time.sleep(0.1)
        return proc.returncode == 0

    @staticmethod
    def _kill_pidfile(path: Path) -> None:
        if not path.exists():
            return
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        except (OSError, ValueError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
