from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from .ilink import ILinkClient, ILinkError, detect_mime


OUTBOUND_FILE_RE = re.compile(
    r"^\s*(?:WX2CODEX_SEND_FILE|WX2CODEX_SEND_IMAGE|IMAGE_PATH)\s*[:=：]\s*(?P<path>.+?)\s*$",
    re.IGNORECASE,
)


def extract_outbound_files(text: str) -> tuple[str, list[Path]]:
    """Extract explicit file relay markers from Codex's final text.

    wx2codex is intentionally not a task runner. Codex must create/capture/
    download the file itself, then opt in to transport by emitting a marker:

        WX2CODEX_SEND_FILE: ~/.wx2codex/outbox/image.png

    The marker line is removed from the WeChat text reply. `~` and environment
    variables such as `$HOME` are expanded before validation. Only paths that
    resolve to absolute local paths are accepted, so ordinary prose or relative
    paths are never treated as files to send.
    """
    clean_lines: list[str] = []
    paths: list[Path] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = OUTBOUND_FILE_RE.match(line)
        if not match:
            clean_lines.append(line)
            continue
        raw_path = cleanup_marker_path(match.group("path") or "")
        path = Path(os.path.expandvars(raw_path)).expanduser()
        if not path.is_absolute():
            clean_lines.append(line)
            continue
        normalized = str(path)
        if normalized not in seen:
            seen.add(normalized)
            paths.append(path)
    return "\n".join(clean_lines).strip(), paths


def relay_outbound_files(
    cfg: dict[str, Any],
    *,
    to_user_id: str,
    context_token: str,
    paths: list[Path],
) -> list[str]:
    """Relay Codex-produced local image files to WeChat.

    This is a transport step only: wx2codex does not create or modify the file.
    Unsupported file types are reported instead of being executed/processed by
    the agent.
    """
    if not paths:
        return []
    if not to_user_id or not context_token:
        return ["⚠️ 有文件需要发送，但当前微信会话缺少 context_token，请先再发一条文字消息。"]

    client = ILinkClient(cfg.get("bot_token") or "")
    errors: list[str] = []
    for path in paths:
        try:
            if not path.exists() or not path.is_file():
                errors.append(f"⚠️ Codex 要发送的文件不存在：{path}")
                continue
            data = path.read_bytes()
            mime = detect_mime(data)
            if not mime.startswith("image/"):
                errors.append(f"⚠️ 暂时只能转发图片到微信，无法发送：{path}（{mime}）")
                continue
            resp = client.send_image(to_user_id, context_token, data, filename=path.name)
            ret = resp.get("ret")
            errcode = resp.get("errcode")
            if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
                raise ILinkError(f"sendmessage 返回异常：{resp}")
            print(f"[文件转发] 已发送图片：{path}")
        except Exception as e:
            print(f"[文件转发] 发送失败：{path}: {e}", file=sys.stderr)
            if isinstance(e, PermissionError):
                errors.append(
                    "⚠️ 图片转发失败：后台 wx2codex 没有权限读取这个路径。\n"
                    f"文件：{path}\n"
                    "请让 Codex 把图片保存/复制到 ~/.wx2codex/outbox/ 后再输出 WX2CODEX_SEND_FILE。"
                )
            else:
                errors.append(f"⚠️ 图片转发失败：{path}\n原因：{e}")
    return errors


def cleanup_marker_path(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    value = value.strip("'\"")
    # Remove common Markdown/code punctuation around the marker value without
    # touching valid spaces inside a macOS path.
    value = value.rstrip("，,。；;")
    return value
