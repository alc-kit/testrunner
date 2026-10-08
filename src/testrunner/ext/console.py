"""Capture a libvirt domain's serial console to a file, from outside the guest.

    cap = ConsoleCapture(log_dir)
    cap.start("web-1", domain_uuid)        # idempotent: a live capture is left alone
    cap.stop_all()

Each capture is `virsh console --force <domain>` under `script` (which gives virsh the
terminal it insists on), started in its own session so it survives the step — and the
runner — that started it, and follows the domain across guest reboots. A pid file per
name makes start() idempotent and stop_all() complete. Why not libvirt's own file-backed
serial: QEMU would then write into the caller's tree, which SELinux refuses on hosts that
enforce it.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


class ConsoleCapture:
    def __init__(self, log_dir: Path, uri: str = "qemu:///system"):
        self.log_dir, self.uri = Path(log_dir), uri

    def _pidfile(self, name: str) -> Path:
        return self.log_dir / f"{name}.pid"

    def running(self, name: str) -> bool:
        try:
            pid = int(self._pidfile(name).read_text().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            return False

    def start(self, name: str, domain: str) -> bool:
        """Start capturing `domain` into <log_dir>/<name>.log; False if one already runs."""
        if self.running(name):
            return False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(
            ["script", "-qfa", "-c", f"virsh -c {self.uri} console --force {domain}",
             str(self.log_dir / f"{name}.log")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self._pidfile(name).write_text(f"{p.pid}\n")
        return True

    def stop_all(self) -> list[str]:
        stopped = []
        for pidf in sorted(self.log_dir.glob("*.pid")):
            try:
                os.killpg(int(pidf.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            pidf.unlink(missing_ok=True)
            stopped.append(pidf.stem)
        return stopped
