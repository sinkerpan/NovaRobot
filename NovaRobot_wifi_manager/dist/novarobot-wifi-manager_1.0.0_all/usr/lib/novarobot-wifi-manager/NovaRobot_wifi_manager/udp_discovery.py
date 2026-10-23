from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable

log = logging.getLogger("novarobot.udp")


class UdpDiscovery:
    def __init__(self, port: int, payload_fn: Callable[[], dict]) -> None:
        self.port = port
        self.payload_fn = payload_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="novarobot-udp", daemon=True)
        self._thread.start()
        log.info("UDP discovery listening on 0.0.0.0:%s", self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)
        self._sock = sock
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="ignore").strip()
            if text and not self._is_discover(text):
                continue
            payload = json.dumps(self.payload_fn(), ensure_ascii=False).encode("utf-8")
            try:
                sock.sendto(payload, addr)
                log.info("UDP discover reply -> %s:%s", addr[0], addr[1])
            except OSError as exc:
                log.warning("UDP reply failed: %s", exc)

    @staticmethod
    def _is_discover(text: str) -> bool:
        lowered = text.lower()
        if lowered in {"novarobot_discover", "discover", "novarobot"}:
            return True
        if "novarobot_discover" in lowered:
            return True
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return False
        return str(obj.get("cmd", "")).lower() in {"discover", "novarobot_discover"}
