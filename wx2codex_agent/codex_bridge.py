from __future__ import annotations

import os
import shutil
import subprocess
import time


class CodexBridgeError(RuntimeError):
    pass


def send_to_codex(text: str, target_app: str = "Codex", activate_delay: float = 0.8) -> None:
    """Paste text into the Codex desktop app and press Enter.

    This intentionally uses macOS clipboard + System Events. The user must grant
    Accessibility permission to the terminal/python process running wx2codex.
    """
    if os.uname().sysname != "Darwin":
        raise CodexBridgeError("当前 UI 自动化只支持 macOS")
    if not text.strip():
        return
    set_clipboard(text)
    activate_app(target_app)
    time.sleep(activate_delay)
    paste_and_enter()


def set_clipboard(text: str) -> None:
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise CodexBridgeError(proc.stderr.strip() or "pbcopy 失败")


def activate_app(target_app: str) -> None:
    script = f'tell application "{escape_applescript(target_app)}" to activate'
    run_osascript(script)


def paste_and_enter() -> None:
    script = '''
tell application "System Events"
    keystroke "v" using command down
    delay 0.1
    key code 36
end tell
'''
    run_osascript(script)


def run_osascript(script: str) -> None:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "osascript 失败"
        raise CodexBridgeError(err)


def escape_applescript(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')


def has_cliclick() -> bool:
    return shutil.which("cliclick") is not None
