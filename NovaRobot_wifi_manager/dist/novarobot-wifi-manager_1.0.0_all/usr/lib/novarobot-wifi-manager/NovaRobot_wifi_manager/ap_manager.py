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

    def start(self) -> bool:
        # 串行化：避免 BLE set_mode 与 FSM request_mode 双开 hostapd
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.is_active():
            log.info("AP already up: %s %s", self.ssid, network.iface_ipv4(self.iface))
            return True
        log.info("starting robot AP %s on %s", self.ssid, self.iface)
        self._write_hostapd()
        self._unmanage_iface()
        network.run(["nmcli", "device", "set", self.iface, "managed", "no"])
        # 清掉可能残留的 hostapd / 半残 AP，避免 Match already configured
        self._stop_radio_stack()
        network.run(["ip", "link", "set", self.iface, "down"])
        network.run(["ip", "addr", "flush", "dev", self.iface])
        network.run(["iw", "dev", self.iface, "set", "type", "managed"])
        network.run(["ip", "addr", "add", f"{self.ip}/24", "brd", "+", "dev", self.iface])
        network.run(["ip", "link", "set", self.iface, "up"])
        network.run(["rfkill", "unblock", "wifi"])
        time.sleep(0.4)
        self._start_dnsmasq()
        ok_hostapd = self._start_hostapd()
        if not ok_hostapd:
            log.error("hostapd process failed to start")
            return False
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.is_active():
                log.info("AP is up: %s %s", self.ssid, self.ip)
                return True
            time.sleep(0.4)
        log.error("AP failed to enter hostapd AP mode")
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
                    "hw_mode=g",
                    f"channel={self.channel}",
                    "beacon_int=100",
                    "dtim_period=2",
                    "max_num_sta=16",
                    "ieee80211n=1",
                    "wmm_enabled=1",
                    # 兼容 RTL8822CE：不要写 TX-STBC/过强能力，否则 hostapd 直接失败
                    "ht_capab=[SHORT-GI-20][RX-STBC1]",
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

    def _unmanage_iface(self) -> None:
        NM_UNMANAGED.parent.mkdir(parents=True, exist_ok=True)
        NM_UNMANAGED.write_text(
            "[keyfile]\n" f"unmanaged-devices=interface-name:{self.iface}\n",
            encoding="utf-8",
        )
        network.run(["systemctl", "reload", "NetworkManager"])
        # 等 NM 真正放手
        deadline = time.time() + 5
        while time.time() < deadline:
            st = network.run(["nmcli", "-g", "GENERAL.STATE", "device", "show", self.iface])
            if "unmanaged" in (st.stdout or "").lower():
                break
            time.sleep(0.3)
        time.sleep(0.3)

    def _stop_radio_stack(self) -> None:
        self._kill_pidfile(HOSTAPD_PID)
        self._kill_pidfile(DNSMASQ_PID)
        # 只杀我们的 hostapd 配置进程；避免误伤系统其它实例时仍兜底
        network.run(["pkill", "-f", f"hostapd.*{HOSTAPD_CONF}"])
        network.run(["pkill", "-f", "NovaRobot-wifi-manager-dnsmasq"])
        # 等 nl80211 释放 Match
        deadline = time.time() + 3
        while time.time() < deadline:
            if network.wifi_mode(self.iface) != "ap":
                break
            time.sleep(0.2)
        time.sleep(0.3)

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
        network.run(["systemctl", "stop", "nimo-hotspot.service"])
        # 前台捕获错误再 daemon；-B 时失败信息常在 stdout
        proc = network.run(
            [
                "hostapd",
                "-B",
                "-P",
                str(HOSTAPD_PID),
                str(HOSTAPD_CONF),
            ],
            timeout=15,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            log.warning("hostapd exit=%s stdout=%s stderr=%s", proc.returncode, out, err)
            return False
        if out or err:
            # Match already configured 等可能仍 return 0 后立刻退出
            log.info("hostapd: %s %s", out, err)
        # 确认进程还在
        time.sleep(0.5)
        if HOSTAPD_PID.exists():
            try:
                pid = int(HOSTAPD_PID.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
                return True
            except (OSError, ValueError):
                pass
        # pid 文件没有也不代表失败，再看接口模式
        return network.wifi_mode(self.iface) == "ap" or proc.returncode == 0

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
