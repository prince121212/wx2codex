from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

DEFAULT_CLOUD_URL = "https://codex.292828.xyz"
APP_DIR_NAME = ".wx2codex"
CONFIG_FILE_NAME = "config.json"
LEGACY_MESSAGE_PREFIX = "来自微信的远程指令："


def app_dir() -> Path:
    return Path(os.environ.get("WX2CODEX_HOME", Path.home() / APP_DIR_NAME)).expanduser()


def config_path() -> Path:
    return app_dir() / CONFIG_FILE_NAME


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def default_config() -> dict[str, Any]:
    return {
        "cloud_url": os.environ.get("WX2CODEX_CLOUD_URL", DEFAULT_CLOUD_URL),
        "device_id": str(uuid.uuid4()),
        "device_name": platform.node() or "Mac",
        "agent_token": "",
        "ilink_user_id": "",
        "bot_id": "",
        "bot_token": "",
        "cursor": "",
        "contexts": {},
        "codex": {
            "provider": os.environ.get("WX2CODEX_CODEX_PROVIDER", "desktop"),
            "fallback_provider": "ui",
            "target_app": os.environ.get("WX2CODEX_TARGET_APP", "Codex"),
            "input_mode": "applescript",
            "desktop_socket_path": os.environ.get("WX2CODEX_DESKTOP_SOCKET_PATH", ""),
            "desktop_auto_launch": os.environ.get("WX2CODEX_DESKTOP_AUTO_LAUNCH", "1") not in {"0", "false", "FALSE", "no", "NO"},
            "codex_binary": os.environ.get("WX2CODEX_CODEX_BINARY", shutil.which("codex") or "codex"),
            "cwd": os.environ.get("WX2CODEX_CWD", ""),
            "current_thread_id": "",
            "recent_threads": [],
            "projects": {},
            "approval_policy": os.environ.get("WX2CODEX_APPROVAL_POLICY", "never"),
            "sandbox_mode": os.environ.get("WX2CODEX_SANDBOX_MODE", "workspace-write"),
            "network_access": os.environ.get("WX2CODEX_NETWORK_ACCESS", "0") in {"1", "true", "TRUE", "yes", "YES"},
            "turn_timeout_seconds": env_int("WX2CODEX_TURN_TIMEOUT_SECONDS", 3600),
            "heartbeat_interval_seconds": env_int("WX2CODEX_HEARTBEAT_INTERVAL_SECONDS", 3600),
            "auto_approve": os.environ.get("WX2CODEX_AUTO_APPROVE", "0") in {"1", "true", "TRUE", "yes", "YES"},
            "typing_indicator_enabled": os.environ.get("WX2CODEX_TYPING_INDICATOR", "1") not in {"0", "false", "FALSE", "no", "NO"},
            "typing_interval_seconds": env_int("WX2CODEX_TYPING_INTERVAL_SECONDS", 5),
            # Keep desktop Codex threads clean: by default only the user's
            # WeChat text is inserted.  Users who want an origin marker can set
            # this explicitly via config.
            "message_prefix": os.environ.get("WX2CODEX_MESSAGE_PREFIX", "")
        }
    }


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = default_config()
    deep_update(merged, cfg)
    if not merged.get("device_id"):
        merged["device_id"] = str(uuid.uuid4())
    codex_cfg = merged.setdefault("codex", {})
    if codex_cfg.get("message_prefix") == LEGACY_MESSAGE_PREFIX:
        # Older installs stored this verbose marker in config.json.  Migrate it
        # away automatically so a plain WeChat message stays plain in Codex.
        codex_cfg["message_prefix"] = ""
    return merged


def deep_update(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value


def save_config(cfg: dict[str, Any]) -> None:
    directory = app_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def redacted(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return value[:2] + "..."
    return f"{value[:keep]}...{value[-keep:]}"
