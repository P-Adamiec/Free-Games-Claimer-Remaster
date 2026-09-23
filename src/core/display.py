"""What the container's virtual screen is doing right now.

Chrome draws into TurboVNC's display here, so a screen that died takes every store with
it and noVNC goes quiet at the same moment (issue #52).
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger("fgc.display")

# The entrypoint uses the same script, so the screen comes back exactly as it started.
VNC_SCRIPT = Path("/fgc/start-vnc.sh")


def display_number(value: str | None = None) -> int:
    """The screen number out of a DISPLAY value such as ':1', 1 when there is nothing to read."""
    raw = str(os.environ.get("DISPLAY", "") if value is None else value)
    tail = raw.rsplit(":", 1)[-1].split(".")[0].strip()
    return int(tail) if tail.isdigit() else 1


def socket_path(number: int | None = None) -> Path:
    """Where X keeps the socket file for that screen."""
    return Path("/tmp/.X11-unix") / f"X{display_number() if number is None else number}"


def xvnc_running() -> bool:
    """True when TurboVNC's X server is still alive."""
    try:
        import psutil
    except Exception:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            if "xvnc" in (proc.info.get("name") or "").lower():
                return True
        except Exception:
            continue
    return False


def screen_is_managed() -> bool:
    """True when this machine brings up its own screen, which is what the container does."""
    return VNC_SCRIPT.exists()


def screen_is_alive() -> bool:
    """True when the virtual screen exists and the server behind it is running."""
    return socket_path().exists() and xvnc_running()


def port_is_open(port: int | str, host: str = "127.0.0.1") -> bool:
    """True when something answers on that port."""
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except Exception:
        return False


def screen_state() -> str:
    """One readable summary of the screen and its two ports, for the log."""
    number = display_number()
    vnc_port = os.environ.get("VNC_PORT", "5900")
    novnc_port = os.environ.get("NOVNC_PORT", "7080")
    return (f":{number} socket={socket_path(number).exists()} Xvnc={xvnc_running()} "
            f"vnc:{vnc_port}={port_is_open(vnc_port)} novnc:{novnc_port}={port_is_open(novnc_port)}")


def restart_screen(timeout: int = 90) -> bool:
    """Start the screen again with the container's own script, and say whether it came back."""
    if not VNC_SCRIPT.exists():
        logger.debug("No %s here, the screen cannot be restarted (normal outside the container).", VNC_SCRIPT)
        return False
    logger.warning("The virtual screen is gone, starting it again with %s", VNC_SCRIPT)
    try:
        done = subprocess.run(["bash", str(VNC_SCRIPT)], capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        logger.warning("Could not run %s: %s", VNC_SCRIPT, exc)
        return False
    if done.returncode != 0:
        said = [line for line in (done.stdout + done.stderr).strip().splitlines() if line.strip()]
        logger.warning("The screen would not start again: %s", " / ".join(said[-3:]) or "it said nothing")
        return False
    back = screen_is_alive()
    logger.info("The virtual screen is back." if back else "The screen script finished but the screen is still gone.")
    return back


def running_as() -> str:
    """The user this process runs as, which on a NAS is often not root."""
    try:
        import pwd
        return f"{pwd.getpwuid(os.getuid()).pw_name}({os.getuid()})"
    except Exception:
        return "unknown"


def free_memory() -> str:
    """Memory the kernel says is still available, the usual reason Chrome dies at once."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return f"{int(line.split()[1]) / 1_000_000:.1f} GB available"
    except Exception:
        pass
    return "unknown"
