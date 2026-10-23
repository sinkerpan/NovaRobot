from __future__ import annotations

import logging
import subprocess
from typing import Any

log = logging.getLogger("novarobot.mdns")


class MdnsPublisher:
    def __init__(self, hostname: str, service_type: str, port: int) -> None:
        self.hostname = hostname
        self.service_type = service_type
        self.port = port
        self._procs: list[subprocess.Popen[str]] = []
        self._ip: str | None = None

    def publish(self, ip: str | None, txt: dict[str, Any] | None = None) -> None:
        if not ip:
            return
        if ip == self._ip and self._alive():
            return
        self.stop()
        self._ip = ip
        txt_args: list[str] = []
        for key, value in (txt or {}).items():
            txt_args.append(f"{key}={value}")
        cmds = [
            ["avahi-publish", "-a", "-R", f"{self.hostname}.local", ip],
            [
                "avahi-publish",
                "-s",
                self.hostname,
                self.service_type,
                str(self.port),
                *txt_args,
            ],
        ]
        for cmd in cmds:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                self._procs.append(proc)
            except OSError as exc:
                log.warning("avahi-publish failed: %s", exc)
        log.info("mDNS published %s.local -> %s", self.hostname, ip)

    def stop(self) -> None:
        for proc in self._procs:
            try:
                proc.terminate()
            except OSError:
                pass
        self._procs.clear()
        self._ip = None

    def _alive(self) -> bool:
        return bool(self._procs) and all(p.poll() is None for p in self._procs)
