from __future__ import annotations

import ctypes
import fcntl
import logging
import os
import threading
import time

log = logging.getLogger("novarobot.hw")

# P3768 J14 LED+/LED- → MOSFET Q21 ← PWR_LED_CTRL = GPIO04 = AON PCC.01
DEFAULT_CHIP = "gpiochip1"
DEFAULT_LINE = 13


class gpiohandle_request(ctypes.Structure):
    _fields_ = [
        ("lineoffsets", ctypes.c_uint32 * 64),
        ("flags", ctypes.c_uint32),
        ("default_values", ctypes.c_uint8 * 64),
        ("consumer_label", ctypes.c_char * 32),
        ("lines", ctypes.c_uint32),
        ("fd", ctypes.c_int),
    ]


class gpiohandle_data(ctypes.Structure):
    _fields_ = [("values", ctypes.c_uint8 * 64)]


def _ioc(dir_: int, type_: int, nr: int, size: int) -> int:
    return (dir_ << 30) | (size << 16) | (type_ << 8) | nr


GPIO_GET_LINEHANDLE = _ioc(3, 0xB4, 0x03, ctypes.sizeof(gpiohandle_request))
GPIOHANDLE_SET_VALUES = _ioc(3, 0xB4, 0x09, ctypes.sizeof(gpiohandle_data))
GPIOHANDLE_REQUEST_OUTPUT = 1 << 1


class StatusLed:
    """Drive the carrier J14 LED+/LED- via gpiochip1 PCC.01 (line 13). 1=on, 0=off."""

    def __init__(
        self,
        name: str = "auto",
        on_ms: int = 80,
        off_ms: int = 80,
        chip: str = DEFAULT_CHIP,
        line: int = DEFAULT_LINE,
    ) -> None:
        self.requested_name = name
        self.on_ms = max(30, int(on_ms))
        self.off_ms = max(30, int(off_ms))
        self.chip_name = chip if chip else DEFAULT_CHIP
        self.line = int(line) if line is not None else DEFAULT_LINE
        self.path = None
        self.error: str | None = None
        self._chip_fd: int | None = None
        self._line_fd: int | None = None
        self._lock = threading.Lock()
        self._mode = "idle"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._open()

    @property
    def name(self) -> str | None:
        if self.error and self._line_fd is None:
            return None
        return f"{self.chip_name}:{self.line}/PCC.01"

    def _open(self) -> None:
        chip_path = f"/dev/{self.chip_name}"
        try:
            chip_fd = os.open(chip_path, os.O_RDONLY)
            req = gpiohandle_request()
            req.lineoffsets[0] = self.line
            req.flags = GPIOHANDLE_REQUEST_OUTPUT
            req.default_values[0] = 1
            req.consumer_label = b"novarobot-led"
            req.lines = 1
            fcntl.ioctl(chip_fd, GPIO_GET_LINEHANDLE, req)
            self._chip_fd = chip_fd
            self._line_fd = req.fd
            log.info("status LED using %s line %s (PWR_LED_CTRL)", chip_path, self.line)
        except OSError as exc:
            self.error = str(exc)
            log.warning("status LED open failed: %s", exc)
            if self._chip_fd is not None:
                try:
                    os.close(self._chip_fd)
                except OSError:
                    pass
                self._chip_fd = None

    def _set(self, on: bool) -> None:
        fd = self._line_fd
        if fd is None:
            return
        data = gpiohandle_data()
        data.values[0] = 1 if on else 0
        try:
            fcntl.ioctl(fd, GPIOHANDLE_SET_VALUES, data)
        except OSError as exc:
            log.debug("LED set failed: %s", exc)

    def start_fast_blink(self) -> None:
        self.stop_blink(restore=False)
        if self._line_fd is None:
            return
        self._stop = threading.Event()
        self._mode = "fast"
        self._thread = threading.Thread(target=self._soft_blink, name="novarobot-led", daemon=True)
        self._thread.start()
        log.info("LED %s fast blink %sms/%sms", self.name, self.on_ms, self.off_ms)

    def _soft_blink(self) -> None:
        while not self._stop.is_set():
            self._set(True)
            if self._stop.wait(self.on_ms / 1000.0):
                break
            self._set(False)
            if self._stop.wait(self.off_ms / 1000.0):
                break

    def solid(self, on: bool = True) -> None:
        self.stop_blink(restore=False)
        self._set(on)
        self._mode = "solid"

    def pulse_ok(self, ms: int = 400) -> None:
        self.solid(True)

    def stop_blink(self, restore: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        self._mode = "idle"
        if thread is not None:
            thread.join(timeout=1.0)
        if restore:
            self.restore()

    def restore(self) -> None:
        self.stop_blink(restore=False)
        self._set(True)
        self._mode = "idle"
        log.info("LED %s restored ON", self.name)

    def close(self) -> None:
        self.stop_blink(restore=False)
        self._set(True)
        for fd in (self._line_fd, self._chip_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._line_fd = None
        self._chip_fd = None
