from __future__ import annotations

import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import app_dir
from .ilink import ILinkClient, MediaAttachment, detect_mime


@dataclass
class SavedAttachment:
    media_type: str
    filename: str
    path: str
    mime: str
    size: int
    label: str

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/") or self.media_type == "image"


def save_wechat_attachments(
    cfg: dict[str, Any],
    ilink: ILinkClient,
    attachments: list[MediaAttachment],
    *,
    from_user: str = "",
) -> list[SavedAttachment]:
    if not attachments:
        return []
    base_dir = attachment_base_dir(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    user_part = safe_filename(from_user.split("@", 1)[0] or "wechat")[:32]
    target_dir = base_dir / f"{stamp}-{user_part}-{uuid.uuid4().hex[:8]}"
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: list[SavedAttachment] = []
    for idx, attachment in enumerate(attachments, 1):
        if not attachment.cdn_media:
            print(f"[附件] 缺少媒体下载参数：{attachment.label}")
            continue
        try:
            data = ilink.download_media(attachment.cdn_media)
            mime = detect_mime(data)
            filename = normalize_attachment_filename(attachment.filename, attachment.media_type, mime, idx)
            path = unique_path(target_dir / filename)
            path.write_bytes(data)
            item = SavedAttachment(
                media_type=attachment.media_type,
                filename=path.name,
                path=str(path),
                mime=mime,
                size=len(data),
                label=attachment.label,
            )
            saved.append(item)
            print(f"[附件] 已保存：{item.path} ({item.mime}, {item.size} bytes)")
        except Exception as e:
            print(f"[附件] 下载失败：{attachment.label} {attachment.filename}: {e}")
    return saved


def attachment_base_dir(cfg: dict[str, Any]) -> Path:
    cwd = ((cfg.get("codex") or {}).get("cwd") or "").strip()
    if cwd:
        try:
            path = Path(cwd).expanduser().resolve()
            if path.exists() and path.is_dir():
                return path / ".wx2codex_attachments"
        except Exception:
            pass
    return app_dir() / "attachments"


def normalize_attachment_filename(filename: str, media_type: str, mime: str, index: int) -> str:
    name = safe_filename(filename or "")
    if not name or name in {".", ".."}:
        name = f"{media_type or 'attachment'}-{index}"
    if "." not in Path(name).name:
        name += extension_for_mime(mime, media_type)
    return name[:120]


def extension_for_mime(mime: str, media_type: str = "") -> str:
    explicit = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "audio/silk": ".silk",
        "audio/amr": ".amr",
        "audio/wav": ".wav",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    if mime in explicit:
        return explicit[mime]
    guessed = mimetypes.guess_extension(mime or "")
    if guessed:
        return guessed
    fallback = {
        "image": ".jpg",
        "file": ".bin",
        "voice": ".silk",
        "video": ".mp4",
    }
    return fallback.get(media_type, ".bin")


def safe_filename(value: str) -> str:
    value = value.strip().replace("\x00", "")
    value = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r'[<>:"|?*]+', "_", value)
    value = value.strip(" .")
    return value or "attachment"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")


def format_attachment_prompt(display: str, attachments: list[SavedAttachment]) -> str:
    if not attachments:
        return display
    lines = [display.strip() or "用户发送了附件，请根据附件内容完成任务。", "", "微信附件已下载到本机："]
    for idx, item in enumerate(attachments, 1):
        size_text = human_size(item.size)
        kind = {
            "image": "图片",
            "file": "文件",
            "voice": "语音",
            "video": "视频",
        }.get(item.media_type, item.media_type or "附件")
        extra = "（同时已作为 localImage 输入）" if item.is_image else ""
        lines.append(f"{idx}. {kind}: {item.path}")
        lines.append(f"   文件名: {item.filename}；类型: {item.mime}；大小: {size_text}{extra}")
    lines.extend([
        "",
        "请优先读取/分析这些附件；如果是图片，请直接看图；如果是文档、表格或 PDF，请读取上面的文件路径后再回答。",
    ])
    return "\n".join(lines).strip()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"
