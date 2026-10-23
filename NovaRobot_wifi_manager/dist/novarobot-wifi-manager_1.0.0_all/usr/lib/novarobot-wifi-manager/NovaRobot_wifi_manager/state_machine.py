from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("novarobot.fsm")

BOOT = "BOOT"
CHECK_CONFIG = "CHECK_CONFIG"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
RECONNECT = "RECONNECT"
AP_MODE = "AP_MODE"


class NetworkStateMachine:
    def __init__(
        self,
        cfg: dict[str, Any],
        sta,
        ap,
        store,
        on_change: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.sta = sta
        self.ap = ap
        self.store = store
        self.on_change = on_change
        self.state = BOOT
        self.detail = "booting"
        self.requested_mode: str | None = None
        self.last_error: str | None = None
        # 手机 BLE 已接管时，禁止自动 AP/STA 抢切
        self.app_control_fn: Callable[[], bool] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def app_controls(self) -> bool:
        try:
            return bool(self.app_control_fn and self.app_control_fn())
        except Exception:
            return False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="novarobot-fsm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def request_mode(self, mode: str) -> dict[str, Any]:
        with self._lock:
            self.requested_mode = mode
        return {"ok": True, "requested": mode, "state": self.state}

    def clear_requested_mode(self) -> None:
        with self._lock:
            self.requested_mode = None

    def note_external(self, state: str, detail: str) -> None:
        """BLE/HTTP 已同步完成模式切换时，更新 FSM，避免再双开 AP/STA。"""
        with self._lock:
            self.requested_mode = None
            self.last_error = None if state in {CONNECTED, AP_MODE} else self.last_error
            self._set(state, detail)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "requested_mode": self.requested_mode,
            "last_error": self.last_error,
        }

    def _set(self, state: str, detail: str) -> None:
        changed = state != self.state or detail != self.detail
        self.state = state
        self.detail = detail
        if changed:
            log.info("state=%s detail=%s", state, detail)
            if self.on_change:
                self.on_change(state, self.snapshot())

    def _loop(self) -> None:
        self._set(BOOT, "service start")
        time.sleep(0.3)
        self._set(CHECK_CONFIG, "inspect saved wifi and current link")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("state machine tick failed")
            self._stop.wait(2)

    def _tick(self) -> None:
        with self._lock:
            requested = self.requested_mode
            self.requested_mode = None

        if requested == "ap":
            self._enter_ap("manual request")
            return
        if requested == "sta":
            if self.ap.is_active():
                self.ap.stop()
            self._set(CONNECTING, "manual STA request")
            self._try_sta()
            return

        if self.sta.is_connected() and not self.ap.is_active():
            self._set(CONNECTED, f"STA {self.sta.current().get('ssid')}")
            return

        if self.ap.is_active():
            self._set(AP_MODE, f"AP {self.ap.ssid}")
            return

        # 手机已通过 BLE 接管：只维持现状，等 App 下发 mode/配网
        if self.app_controls():
            self._set(self.state if self.state != BOOT else RECONNECT, "app controls mode via BLE")
            return

        wifi = self.store.load_wifi()
        has_saved = self.sta.has_saved() or bool(wifi.get("profiles"))
        if not has_saved:
            self._enter_ap("no saved wifi")
            return

        if self.state in {CONNECTED, RECONNECT}:
            self._set(RECONNECT, "link lost, retry saved wifi")
        else:
            self._set(CONNECTING, "try saved wifi")
        self._try_sta()

    def _try_sta(self) -> None:
        preferred = self.store.load_wifi().get("preferred_ssid")
        ok = self.sta.connect_best(preferred, timeout_sec=int(self.cfg["sta_timeout_sec"]))
        if ok:
            self.last_error = None
            self._set(CONNECTED, f"STA {self.sta.current().get('ssid')}")
            return
        self.last_error = "STA connect failed"
        if self._should_fallback_ap():
            self._enter_ap("STA failed, fallback AP")
        else:
            self._set(RECONNECT, "STA failed, will retry")

    def _should_fallback_ap(self) -> bool:
        if self.app_controls():
            return False
        disable = self.store.data_dir / "disable-ap"
        if disable.exists():
            log.warning("AP fallback blocked by %s", disable)
            return False
        return True

    def _enter_ap(self, reason: str) -> None:
        # App 接管后，仅允许 manual/BLE/HTTP 的 request_mode("ap") 进 AP
        if self.app_controls() and reason != "manual request":
            self._set(RECONNECT, "app controls mode, skip auto AP")
            return
        disable = self.store.data_dir / "disable-ap"
        if disable.exists():
            self._set(RECONNECT, "AP fallback disabled by disable-ap")
            return
        if self.sta.is_connected() and reason != "manual request":
            self._set(CONNECTED, "keep current STA, skip AP")
            return
        ok = self.ap.start()
        if ok:
            self._set(AP_MODE, reason)
        else:
            self.last_error = "AP start failed"
            self._set(RECONNECT, "AP start failed")
