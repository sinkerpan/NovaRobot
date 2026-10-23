from __future__ import annotations

import fcntl
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("novarobot.hw")

KEY_POWER = 116
EV_KEY = 0x01
EVENT_FORMAT = "llHHI"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EVIOCGRAB = 0x40044590


def find_gpio_keys() -> str | None:
    root = Path("/sys/class/input")
    if not root.exists():
        return None
    for node in sorted(root.glob("event*")):
        name_path = node / "device" / "name"
        try:
            name = name_path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if name == "gpio-keys":
            dev = f"/dev/input/{node.name}"
            if os.path.exists(dev):
                return dev
    keys_path = Path("/sys/devices/platform/gpio-keys/input")
    if keys_path.exists():
        for inp in keys_path.glob("input*/event*"):
            dev = f"/dev/input/{inp.name}"
            if os.path.exists(dev):
                return dev
    return None


class PowerButton:
    """Listen to J14 PWR BTN (gpio-keys KEY_POWER=116)."""

    def __init__(
        self,
        short_press_max_ms: int = 1500,
        short_press_min_ms: int = 40,
    ) -> None:
        self.short_press_max_ms = max(200, int(short_press_max_ms))
        self.short_press_min_ms = max(0, int(short_press_min_ms))
        self.path = find_gpio_keys()
        self.error: str | None = None if self.path else "gpio-keys event node not found"
        self._fd: int | None = None
        self._grabbed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._press_ns: int | None = None
        self._on_short: Callable[[], None] | None = None
        self._on_press: Callable[[], None] | None = None
        self._on_release: Callable[[float], None] | None = None

    def start(
        self,
        on_short_press: Callable[[], None] | None = None,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[float], None] | None = None,
    ) -> None:
        self._on_short = on_short_press
        self._on_press = on_press
        self._on_release = on_release
        if not self.path:
            log.warning("power button unavailable: %s", self.error)
            return
        try:
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = str(exc)
            log.warning("open %s failed: %s", self.path, exc)
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="novarobot-pwrbtn", daemon=True)
        self._thread.start()
        log.info("power button listening on %s KEY_POWER short<=%sms", self.path, self.short_press_max_ms)

    def stop(self) -> None:
        self._stop.set()
        self.ungrab()
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.5)

    def grab(self) -> None:
        fd = self._fd
        if fd is None or self._grabbed:
            return
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            self._grabbed = True
            log.info("power button grabbed")
        except OSError as exc:
            log.warning("EVIOCGRAB on failed: %s", exc)

    def ungrab(self) -> None:
        fd = self._fd
        if fd is None or not self._grabbed:
            self._grabbed = False
            return
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 0)
            log.info("power button released")
        except OSError as exc:
            log.warning("EVIOCGRAB off failed: %s", exc)
        self._grabbed = False

    def _loop(self) -> None:
        fd = self._fd
        if fd is None:
            return
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(fd, EVENT_SIZE * 8)
            except BlockingIOError:
                self._stop.wait(0.05)
                continue
            except OSError:
                if self._stop.is_set():
                    break
                self._stop.wait(0.2)
                continue
            if not chunk:
                self._stop.wait(0.05)
                continue
            buf += chunk
            while len(buf) >= EVENT_SIZE:
                raw, buf = buf[:EVENT_SIZE], buf[EVENT_SIZE:]
                _sec, _usec, ev_type, code, value = struct.unpack(EVENT_FORMAT, raw)
                if ev_type != EV_KEY or code != KEY_POWER:
                    continue
                now = time.time_ns()
                if value == 1:
                    self._press_ns = now
                    cb = self._on_press
                    if cb is not None:
                        try:
                            cb()
                        except Exception:
                            log.exception("power button press callback failed")
                elif value == 0:
                    start = self._press_ns
                    self._press_ns = None
                    held_ms = (now - start) / 1_000_000 if start is not None else 0.0
                    rel = self._on_release
                    if rel is not None:
                        try:
                            rel(held_ms)
                        except Exception:
                            log.exception("power button release callback failed")
                    if start is None:
                        continue
                    if self.short_press_min_ms <= held_ms <= self.short_press_max_ms:
                        log.info("power button short press %.0fms", held_ms)
                        cb = self._on_short
                        if cb is not None:
                            try:
                                cb()
                            except Exception:
                                log.exception("power button short-press callback failed")
                    else:
                        log.info("power button ignored press %.0fms", held_ms)
