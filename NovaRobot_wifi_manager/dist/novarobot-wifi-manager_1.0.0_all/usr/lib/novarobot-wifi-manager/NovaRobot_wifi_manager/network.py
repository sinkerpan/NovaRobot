from __future__ import annotations

import json
import subprocess
import time
from typing import Any


def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def iface_ipv4(iface: str) -> str | None:
    result = run(["ip", "-4", "-json", "addr", "show", "dev", iface])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for item in payload:
        for addr in item.get("addr_info", []):
            if addr.get("family") == "inet":
                return addr.get("local")
    return None


def iface_operstate(iface: str) -> str:
    path = f"/sys/class/net/{iface}/operstate"
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def current_ssid(iface: str) -> str | None:
    result = run(["iw", "dev", iface, "info"])
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("ssid "):
            return line[5:]
    return None


def wifi_mode(iface: str) -> str:
    result = run(["iw", "dev", iface, "info"])
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("type "):
            kind = line.split()[-1]
            if kind == "AP":
                return "ap"
            if kind == "managed":
                return "sta"
            return kind
    return "unknown"


def nm_active_wifi() -> dict[str, str] | None:
    result = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "connection", "show", "--active"])
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        name, typ, device, state = parts[0], parts[1], parts[2], parts[3]
        if typ == "802-11-wireless" and state == "activated":
            return {"name": name, "device": device, "state": state}
    return None


def nm_wifi_connections() -> list[dict[str, str]]:
    result = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show"])
    items: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        name, typ, device = parts[0], parts[1], parts[2]
        if typ != "802-11-wireless":
            continue
        if name.lower() == "hotspot":
            continue
        items.append({"name": name, "device": device})
    return items


def nm_wifi_ssid(con_name: str) -> str | None:
    result = run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", con_name])
    ssid = result.stdout.strip()
    return ssid or None


def scan_wifi(iface: str) -> list[dict[str, Any]]:
    """Scan nearby Wi-Fi. Prefer nmcli; fall back to iw when iface is unmanaged (AP mode)."""
    rows = _scan_wifi_nmcli(iface)
    if rows:
        return rows
    return _scan_wifi_iw(iface)


def _scan_wifi_nmcli(iface: str) -> list[dict[str, Any]]:
    run(["nmcli", "device", "wifi", "rescan", "ifname", iface], timeout=15)
    result = run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list", "ifname", iface],
        timeout=15,
    )
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        ssid, signal, security, in_use = parts[0], parts[1], parts[2], parts[3]
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        rows.append(
            {
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "security": security,
                "in_use": in_use == "*",
                "saved": False,
            }
        )
    rows.sort(key=lambda r: r["signal"], reverse=True)
    return rows[:40]


def _scan_wifi_iw(iface: str) -> list[dict[str, Any]]:
    # Brief scan dump; works even when NM does not manage the iface.
    run(["iw", "dev", iface, "scan", "trigger"], timeout=8)
    time.sleep(2)
    result = run(["iw", "dev", iface, "scan", "dump"], timeout=15)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    ssid = ""
    signal = 0
    security = "Open"
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.startswith("BSS ") and ssid:
            if ssid not in seen:
                seen.add(ssid)
                rows.append({"ssid": ssid, "signal": signal, "security": security, "in_use": False, "saved": False})
            ssid, signal, security = "", 0, "Open"
        elif s.startswith("SSID:"):
            ssid = s[5:].strip()
        elif s.startswith("signal:"):
            # e.g. signal: -42.00 dBm
            try:
                dbm = float(s.split(":")[1].split("dBm")[0].strip())
                signal = max(0, min(100, int(2 * (dbm + 100))))
            except ValueError:
                signal = 0
        elif "RSN:" in s or "WPA:" in s:
            security = "WPA2"
    if ssid and ssid not in seen:
        rows.append({"ssid": ssid, "signal": signal, "security": security, "in_use": False, "saved": False})
    rows.sort(key=lambda r: r["signal"], reverse=True)
    return rows[:40]


def first_ipv4() -> str | None:
    result = run(["hostname", "-I"])
    for token in result.stdout.split():
        if token.startswith("127.") or token.startswith("172.17."):
            continue
        if "." in token:
            return token
    return None
