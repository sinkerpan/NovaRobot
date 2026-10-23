from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.json"
SYSTEM_CONFIG_PATH = Path("/etc/novarobot-wifi-manager/default.json")


def config_candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("NOVAROBOT_WIFI_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(SYSTEM_CONFIG_PATH)
    paths.append(DEFAULT_CONFIG_PATH)
    return paths


def load_default() -> dict[str, Any]:
    chosen: Path | None = None
    for path in config_candidates():
        if path.is_file():
            chosen = path
            break
    if chosen is None:
        raise FileNotFoundError("no NovaRobot wifi config (checked NOVAROBOT_WIFI_CONFIG, /etc, source tree)")
    with chosen.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    data_dir = Path(str(cfg.get("data_dir") or "data"))
    if not data_dir.is_absolute():
        cfg["data_dir"] = str(PACKAGE_ROOT / data_dir)
    return cfg


class ConfigStore:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or load_default()
        self.data_dir = Path(self.cfg["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.wifi_path = self.data_dir / "wifi.json"
        self.identity_path = self.data_dir / "identity.json"

    def load_wifi(self) -> dict[str, Any]:
        if not self.wifi_path.exists():
            return {"profiles": [], "preferred_ssid": None}
        with self.wifi_path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def save_wifi(self, data: dict[str, Any]) -> None:
        tmp = self.wifi_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.wifi_path)

    def save_identity(self, data: dict[str, Any]) -> None:
        with self.identity_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def add_profile(self, ssid: str, password: str | None = None, priority: int = 20) -> dict[str, Any]:
        data = self.load_wifi()
        profiles = [p for p in data.get("profiles", []) if p.get("ssid") != ssid]
        item: dict[str, Any] = {"ssid": ssid, "priority": priority, "source": "provisioned"}
        if password:
            item["has_password"] = True
        profiles.append(item)
        data["profiles"] = sorted(profiles, key=lambda p: p.get("priority", 0), reverse=True)
        data["preferred_ssid"] = ssid
        self.save_wifi(data)
        return data
