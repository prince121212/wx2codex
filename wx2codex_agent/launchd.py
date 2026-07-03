from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import app_dir

LABEL = "xyz.292828.wx2codex.agent"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install_launch_agent(extra_args: Optional[list[str]] = None) -> Path:
    log_dir = app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Use unbuffered output so `~/.wx2codex/logs/agent.*.log` reflects the
    # currently running provider immediately.  This is especially useful when
    # switching between the legacy app_server provider and the desktop IPC
    # provider during troubleshooting.
    args = [sys.executable, "-u", "-m", "wx2codex_agent", "run"] + (extra_args or [])
    plist = {
        "Label": LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_dir / "agent.out.log"),
        "StandardErrorPath": str(log_dir / "agent.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
        }
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as f:
        plistlib.dump(plist, f)
    unload_launch_agent(ignore_errors=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)], check=False)
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"], check=False)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=False)
    return PLIST_PATH


def unload_launch_agent(ignore_errors: bool = False) -> None:
    proc = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)], capture_output=True, text=True)
    if proc.returncode != 0 and not ignore_errors:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "launchctl bootout 失败")


def uninstall_launch_agent() -> None:
    unload_launch_agent(ignore_errors=True)
    try:
        PLIST_PATH.unlink()
    except FileNotFoundError:
        pass
