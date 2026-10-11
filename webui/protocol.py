"""The file-based handshake between the bot and the web interface.

Both containers mount the same data volume (/fgc/data). There is no network
connection and no Docker API between them: the web interface writes small
request files, the bot (webui/bridge.py) picks them up and writes its status
back. Claim runs themselves go through the shared job queue (webui/jobqueue.py).
Standard library only, because both sides import it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Everything the web interface shares with the bot lives in <data>/webui/,
# except config.env, which is the file the bot itself already reads
# (src/core/config.py loads data/config.env after .env).
CONFIG_FILE = "config.env"
WEBUI_DIR = "webui"
STATUS_FILE = "status.json"          # written by the bot, read by the web interface
RESTART_REQUEST = "request-restart.json"  # "settings changed, restart", written by the web interface
RESTART_NOTE = "restarted.json"      # left by the bot right before it restarts itself
LOG_FILE = "bot.log"                 # plain-text copy of the bot's log

# The bot refreshes status.json at least this often; older means it is not running.
HEARTBEAT_SECONDS = 10


def data_dir() -> Path:
    """The shared data folder: /fgc/data in both containers, overridable for tests."""
    return Path(os.environ.get("FGC_DATA_DIR") or "/fgc/data")


def webui_dir(base: Path | None = None) -> Path:
    return (base or data_dir()) / WEBUI_DIR


def match_owner(path: Path, reference: Path) -> None:
    """Give a file the owner of the data folder, so the bot (maybe running as PUID) can use it."""
    try:
        if os.geteuid() != 0:
            return
        st = reference.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a file so a reader never sees half of it: temp file, fsync, rename."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # A folder we create in a bot's volume must belong to the bot, or it cannot remove requests.
        match_owner(path.parent, path.parent.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        match_owner(Path(tmp), path.parent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, indent=2, default=str))


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def take_request(path: Path) -> dict | None:
    """Read and remove a request file in one go, so a request is acted on once."""
    data = read_json(path)
    if data is None and not path.exists():
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data or {}
