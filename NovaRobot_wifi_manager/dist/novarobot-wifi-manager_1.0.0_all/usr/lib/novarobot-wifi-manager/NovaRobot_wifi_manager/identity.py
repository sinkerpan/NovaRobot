from __future__ import annotations

from pathlib import Path


DEFAULT_IFACE = "wlP1p1s0"


def resolve_wifi_iface(configured: str) -> str:
    if configured and Path(f"/sys/class/net/{configured}").exists():
        return configured
    net = Path("/sys/class/net")
    if not net.exists():
        return configured or DEFAULT_IFACE
    for path in sorted(net.iterdir()):
        if path.name == "lo":
            continue
        if (path / "wireless").exists() or (path / "phy80211").exists():
            return path.name
    return configured or DEFAULT_IFACE


def read_mac(iface: str = DEFAULT_IFACE) -> str:
    path = Path(f"/sys/class/net/{iface}/address")
    if path.exists():
        return path.read_text(encoding="utf-8").strip().lower()
    return "00:00:00:00:00:00"


def suffix_from_mac(mac: str) -> str:
    return mac.replace(":", "")[-4:].upper()


def device_id(iface: str = DEFAULT_IFACE) -> str:
    return f"NovaRobot-{suffix_from_mac(read_mac(iface))}"


def ap_ssid(iface: str = DEFAULT_IFACE) -> str:
    return f"NovaRobot-{suffix_from_mac(read_mac(iface))}"


def mdns_hostname(iface: str = DEFAULT_IFACE) -> str:
    return f"novarobot-{suffix_from_mac(read_mac(iface)).lower()}"
