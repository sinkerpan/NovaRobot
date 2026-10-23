from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .power_button import PowerButton
from .status_led import StatusLed

log = logging.getLogger("novarobot.ble")

BLUEZ = "org.bluez"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MGR = "org.bluez.AgentManager1"
AGENT_PATH = "/org/bluez/novarobot/agent"
CAPABILITY = "DisplayYesNo"


def _device_addr(device: Any) -> str:
    text = str(device or "")
    if "/dev_" in text:
        return text.rsplit("/dev_", 1)[-1].replace("_", ":")
    return text


class PairingConfirmer:
    """
    BlueZ Agent: 手机弹出配对确认时 LED 快闪，短按 Jetson 电源键等同桌面点「允许」。
    """

    def __init__(
        self,
        led: StatusLed,
        button: PowerButton,
        timeout_sec: int = 30,
        on_state: Callable[[bool, str | None], None] | None = None,
    ) -> None:
        self.led = led
        self.button = button
        self.timeout_sec = max(5, int(timeout_sec))
        self.on_state = on_state
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._glib = None
        self._agent = None
        self._bus = None

    @property
    def pending_device(self) -> str | None:
        pending = self._pending
        if not pending:
            return None
        return str(pending.get("addr") or "")

    def attach(self, bus: Any, glib: Any) -> None:
        import dbus
        import dbus.service

        self._glib = glib
        self._bus = bus
        confirmer = self

        class Agent(dbus.service.Object):
            def __init__(self) -> None:
                dbus.service.Object.__init__(self, bus, AGENT_PATH)

            @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
            def Release(self) -> None:
                log.info("BLE agent released by BlueZ")
                confirmer.finish("cancel", restore_led=True)

            @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
            def RequestPinCode(self, device: Any) -> str:
                raise dbus.exceptions.DBusException("org.bluez.Error.Rejected: pin not supported")

            @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
            def RequestPasskey(self, device: Any) -> int:
                raise dbus.exceptions.DBusException("org.bluez.Error.Rejected: passkey entry not supported")

            @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
            def DisplayPasskey(self, device: Any, passkey: int) -> None:
                log.info("BLE DisplayPasskey %s %06d", _device_addr(device), int(passkey))

            @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
            def DisplayPinCode(self, device: Any, pincode: str) -> None:
                log.info("BLE DisplayPinCode %s %s", _device_addr(device), pincode)

            @dbus.service.method(
                AGENT_IFACE,
                in_signature="ou",
                out_signature="",
                async_callbacks=("reply", "error"),
            )
            def RequestConfirmation(self, device: Any, passkey: int, reply: Any, error: Any) -> None:
                confirmer.begin(
                    kind="confirmation",
                    device=device,
                    detail=f"{int(passkey):06d}",
                    reply=reply,
                    error=error,
                )

            @dbus.service.method(
                AGENT_IFACE,
                in_signature="o",
                out_signature="",
                async_callbacks=("reply", "error"),
            )
            def RequestAuthorization(self, device: Any, reply: Any, error: Any) -> None:
                confirmer.begin(
                    kind="authorization",
                    device=device,
                    detail="",
                    reply=reply,
                    error=error,
                )

            @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
            def AuthorizeService(self, device: Any, uuid: str) -> None:
                log.info("BLE AuthorizeService %s %s (auto-accept)", _device_addr(device), uuid)

            @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
            def Cancel(self) -> None:
                log.info("BLE pairing canceled by BlueZ")
                confirmer.finish("cancel", restore_led=True)

        self._agent = Agent()
        mgr = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR)
        try:
            mgr.UnregisterAgent(AGENT_PATH)
        except Exception:
            pass
        mgr.RegisterAgent(AGENT_PATH, CAPABILITY)
        try:
            mgr.RequestDefaultAgent(AGENT_PATH)
        except Exception as exc:
            log.warning("RequestDefaultAgent failed: %s", exc)
        log.info("BLE pairing agent registered path=%s cap=%s", AGENT_PATH, CAPABILITY)

        self.button.start(on_short_press=self._on_short_press, on_press=self._on_press, on_release=self._on_release)

    def ensure_default(self) -> None:
        if self._bus is None:
            return
        try:
            import dbus

            mgr = dbus.Interface(self._bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR)
            mgr.RequestDefaultAgent(AGENT_PATH)
        except Exception as exc:
            log.debug("RequestDefaultAgent retry: %s", exc)

    def begin(self, kind: str, device: Any, detail: str, reply: Any, error: Any) -> None:
        addr = _device_addr(device)
        with self._lock:
            pending = self._pending
            same = bool(pending and pending.get("addr") == addr)
        if same:
            log.info("BLE pair extra %s for %s while waiting — auto-accept", kind, addr)
            try:
                reply()
            except Exception:
                log.exception("BLE extra pair reply failed")
            return
        log.info(
            "BLE pair wait kind=%s device=%s detail=%s — LED blink, press KEY_POWER to confirm (press=off, release=on)",
            kind,
            addr,
            detail,
        )
        with self._lock:
            old = self._pending
            self._pending = {
                "kind": kind,
                "device": device,
                "addr": addr,
                "detail": detail,
                "reply": reply,
                "error": error,
                "timer": None,
            }
        if old:
            self._reject(old, "org.bluez.Error.Canceled", "superseded")
        self.led.start_fast_blink()
        self.button.grab()
        if self.on_state:
            self.on_state(True, addr)
        glib = self._glib
        if glib is not None:
            timer = glib.timeout_add_seconds(self.timeout_sec, self._on_timeout)
            with self._lock:
                if self._pending is not None:
                    self._pending["timer"] = timer

    def _on_timeout(self) -> bool:
        log.warning("BLE pair confirm timed out after %ss", self.timeout_sec)
        self.finish("timeout", restore_led=True)
        return False

    def _on_press(self) -> None:
        if self._pending is None:
            return
        self.led.solid(False)

    def _on_release(self, held_ms: float) -> None:
        if self._pending is None:
            return
        self.led.solid(True)
        if held_ms > self.button.short_press_max_ms:
            self.led.start_fast_blink()

    def _on_short_press(self) -> None:
        glib = self._glib
        if glib is None:
            self.finish("accept", restore_led=True)
            return
        glib.idle_add(lambda: self.finish("accept", restore_led=True) or False)

    def finish(self, result: str, restore_led: bool = True) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return
        timer = pending.get("timer")
        glib = self._glib
        if timer and glib is not None:
            try:
                glib.source_remove(timer)
            except Exception:
                pass
        self.button.ungrab()
        addr = pending.get("addr")
        if result == "accept":
            log.info("BLE pair confirmed by power button device=%s", addr)
            try:
                pending["reply"]()
            except Exception:
                log.exception("BLE pair reply failed")
            self.led.pulse_ok()
        else:
            name = "org.bluez.Error.Canceled" if result == "cancel" else "org.bluez.Error.Rejected"
            log.info("BLE pair %s device=%s", result, addr)
            self._reject(pending, name, result)
            if restore_led:
                self.led.restore()
        if self.on_state:
            self.on_state(False, None)

    def _reject(self, pending: dict[str, Any], name: str, reason: str) -> None:
        err = pending.get("error")
        if err is None:
            return
        try:
            import dbus

            err(dbus.exceptions.DBusException(f"{name}: {reason}"))
        except Exception:
            log.exception("BLE pair error reply failed")

    def stop(self) -> None:
        self.finish("cancel", restore_led=True)
        self.button.stop()
        self.led.restore()
