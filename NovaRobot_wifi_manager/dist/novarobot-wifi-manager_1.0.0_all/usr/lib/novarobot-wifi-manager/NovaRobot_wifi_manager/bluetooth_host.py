from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("novarobot.ble")

DROPIN_DIR = Path("/etc/systemd/system/bluetooth.service.d")
DROPIN_PATH = DROPIN_DIR / "zz-novarobot-bluetooth.conf"
DROPIN_BODY = """[Service]
ExecStart=
# Must sort after nv-bluetooth-service.conf so -E is not overwritten
ExecStart=/usr/lib/bluetooth/bluetoothd -E --noplugin=audio,a2dp,avrcp
"""
OLD_DROPINS = (
    "10-experimental.conf",
    "99-novarobot-experimental.conf",
)


def _bluetoothd_cmdline() -> str:
    try:
        out = subprocess.check_output(["pgrep", "-n", "bluetoothd"], text=True).strip()
        pid = out.splitlines()[0]
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _ensure_main_conf_experimental() -> None:
    conf = Path("/etc/bluetooth/main.conf")
    if not conf.exists():
        return
    try:
        text = conf.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        log.warning("read main.conf failed: %s", exc)
        return
    lines = text.splitlines()
    changed = False
    found = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and "Experimental" in line:
            lines[i] = "Experimental = true"
            found = True
            changed = True
            break
        if line.strip().startswith("Experimental"):
            found = True
            if "true" not in line.lower():
                lines[i] = "Experimental = true"
                changed = True
            break
    if not found:
        if not any(l.strip() == "[General]" for l in lines):
            lines.extend(["", "[General]"])
        lines.append("Experimental = true")
        changed = True
    if changed:
        try:
            conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("set Experimental=true in %s", conf)
        except Exception as exc:
            log.warning("write main.conf failed: %s", exc)


def ensure_bluetooth_experimental() -> bool:
    """
    以 root 运行时：写入排在 NVIDIA drop-in 之后的 zz-*.conf，保证 bluetoothd -E。
    返回 True 表示当前 cmdline 已含 -E。
    """
    if os.geteuid() != 0:
        log.warning("not root; cannot ensure bluetoothd -E")
        return " -E" in f" {_bluetoothd_cmdline()} "

    try:
        DROPIN_DIR.mkdir(parents=True, exist_ok=True)
        for name in OLD_DROPINS:
            p = DROPIN_DIR / name
            if p.exists():
                p.unlink()
                log.info("removed stale bluetooth drop-in %s", p)
        if not DROPIN_PATH.exists() or DROPIN_PATH.read_text(encoding="utf-8", errors="ignore") != DROPIN_BODY:
            DROPIN_PATH.write_text(DROPIN_BODY, encoding="utf-8")
            log.info("wrote %s", DROPIN_PATH)
        _ensure_main_conf_experimental()
    except Exception as exc:
        log.warning("bluetooth drop-in ensure failed: %s", exc)

    cmdline = _bluetoothd_cmdline()
    if "-E" in cmdline:
        log.info("bluetoothd already experimental: %s", cmdline)
        return True

    log.warning("bluetoothd missing -E (%s); reloading unit", cmdline)
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=30)
        subprocess.run(["systemctl", "restart", "bluetooth"], check=False, timeout=60)
        time.sleep(2)
    except Exception as exc:
        log.warning("restart bluetooth failed: %s", exc)
        return False

    cmdline = _bluetoothd_cmdline()
    ok = "-E" in cmdline
    if ok:
        log.info("bluetoothd experimental OK: %s", cmdline)
    else:
        log.warning("bluetoothd still without -E: %s", cmdline)
    return ok


def stop_adapter_discovery() -> None:
    """外设广播时不要让适配器处于 Discovering。"""
    try:
        subprocess.run(
            ["bluetoothctl", "scan", "off"],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except Exception:
        pass
