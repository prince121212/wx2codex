from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Optional

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
EXPIRED_CODES = {-14, 40014, 1002}


class ILinkError(RuntimeError):
    pass


@dataclass
class LoginResult:
    bot_token: str
    bot_id: str
    ilink_user_id: str


@dataclass
class MediaAttachment:
    media_type: str
    label: str
    filename: str
    cdn_media: dict[str, Any]
    duration_ms: int = 0
    raw_item: Optional[dict[str, Any]] = None


class ILinkClient:
    def __init__(self, bot_token: str = "", base_url: str = ILINK_BASE_URL, timeout: int = 30):
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_bot_qrcode(self) -> dict[str, Any]:
        url = f"{self.base_url}/ilink/bot/get_bot_qrcode?bot_type=3"
        return self._get_json(url)

    def get_qrcode_status(self, qrcode_key: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"qrcode": qrcode_key})
        url = f"{self.base_url}/ilink/bot/get_qrcode_status?{query}"
        return self._get_json(url, headers={"iLink-App-ClientVersion": "1"}, timeout=8)

    def wait_login(self, qrcode_key: str, timeout_seconds: int = 120, poll_interval: float = 1.5) -> LoginResult:
        start = time.time()
        last_status = ""
        while time.time() - start < timeout_seconds:
            status = self.get_qrcode_status(qrcode_key)
            current = status.get("status") or ""
            if current != last_status:
                if current == "scaned":
                    print("已扫码，请在手机上确认...")
                elif current:
                    print(f"二维码状态：{current}")
                last_status = current
            if current == "confirmed":
                bot_token = status.get("bot_token") or ""
                if not bot_token:
                    raise ILinkError("微信已确认，但没有返回 bot_token")
                return LoginResult(
                    bot_token=bot_token,
                    bot_id=status.get("ilink_bot_id") or "",
                    ilink_user_id=status.get("ilink_user_id") or "",
                )
            if current == "expired":
                raise ILinkError("二维码已过期，请重新运行 connect")
            time.sleep(poll_interval)
        raise ILinkError("等待扫码确认超时")

    def get_updates(self, cursor: str = "", timeout: int = 25) -> dict[str, Any]:
        return self.post("getupdates", {"get_updates_buf": cursor}, timeout=timeout)

    def send_text(self, to_user_id: str, context_token: str, text: str) -> dict[str, Any]:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"wx2codex:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            }
        }
        return self.post("sendmessage", body)

    def send_image(
        self,
        to_user_id: str,
        context_token: str,
        image_bytes: bytes,
        filename: str = "image.jpg",
        description: str = "",
    ) -> dict[str, Any]:
        uploaded = self.upload_media(image_bytes, filename, media_type=1, to_user_id=to_user_id)
        image_item = {
            "media": uploaded["media"],
            "aeskey": uploaded["aes_key_hex"],
            "mid_size": uploaded["encrypted_size"],
        }
        item_list: list[dict[str, Any]] = [{"type": 2, "image_item": image_item}]
        if description:
            item_list.append({"type": 1, "text_item": {"text": description}})
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"wx2codex:image:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": item_list,
            }
        }
        return self.post("sendmessage", body)

    def upload_media(self, file_bytes: bytes, filename: str, media_type: int, to_user_id: str) -> dict[str, Any]:
        if not file_bytes:
            raise ILinkError("图片内容为空")
        aes_key_hex = os.urandom(16).hex()
        aes_key_bytes = bytes.fromhex(aes_key_hex)
        encrypted = aes_ecb_encrypt_pkcs7(file_bytes, aes_key_bytes)
        filekey = os.urandom(16).hex()
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": len(file_bytes),
            "rawfilemd5": hashlib.md5(file_bytes).hexdigest(),
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
        }
        result = self.post("getuploadurl", body, timeout=20)
        ret = result.get("ret")
        errcode = result.get("errcode")
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise ILinkError(f"getuploadurl 失败：{result}")
        upload_param = result.get("upload_param") or ""
        if not upload_param:
            raise ILinkError(f"getuploadurl 未返回 upload_param：{result}")
        cdn_url = (
            f"{ILINK_CDN_BASE}/upload?encrypted_query_param="
            f"{urllib.parse.quote(upload_param, safe='')}&filekey={urllib.parse.quote(filekey, safe='')}"
        )
        req = urllib.request.Request(
            cdn_url,
            data=encrypted,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "User-Agent": "wx2codex-agent/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                encrypted_param = resp.headers.get("x-encrypted-param", "")
                if not encrypted_param:
                    body_preview = resp.read(200).decode("utf-8", errors="replace")
                    raise ILinkError(f"CDN 上传缺少 x-encrypted-param：HTTP {resp.status} {body_preview}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise ILinkError(f"CDN 上传 HTTP {e.code}: {raw[:300] or e.reason}") from e
        except urllib.error.URLError as e:
            raise ILinkError(f"CDN 上传失败：{e}") from e
        return {
            "filekey": filekey,
            "media": {
                "encrypt_query_param": encrypted_param,
                "aes_key": base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8"),
                "encrypt_type": 1,
            },
            "aes_key_hex": aes_key_hex,
            "raw_size": len(file_bytes),
            "encrypted_size": len(encrypted),
            "filename": filename,
        }

    def download_media(self, cdn_media_info: dict[str, Any], *, timeout: int = 60, max_bytes: int = 50 * 1024 * 1024) -> bytes:
        encrypt_query_param = (
            cdn_media_info.get("encrypt_query_param")
            or cdn_media_info.get("encrypted_query_param")
            or ""
        )
        aes_key_b64 = cdn_media_info.get("aes_key") or ""
        if not encrypt_query_param or not aes_key_b64:
            raise ILinkError("媒体下载参数不完整")
        download_url = f"{ILINK_CDN_BASE}/download?encrypted_query_param={urllib.parse.quote(encrypt_query_param, safe='')}"
        req = urllib.request.Request(download_url, headers={"User-Agent": "wx2codex-agent/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ILinkError(f"媒体过大：{length} bytes")
                encrypted = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise ILinkError(f"CDN 下载 HTTP {e.code}: {raw[:300] or e.reason}") from e
        except urllib.error.URLError as e:
            raise ILinkError(f"CDN 下载失败：{e}") from e
        if len(encrypted) > max_bytes:
            raise ILinkError(f"媒体过大：{len(encrypted)} bytes")
        aes_key_bytes = decode_aes_key(aes_key_b64)
        return aes_ecb_decrypt_pkcs7(encrypted, aes_key_bytes)

    def get_config(self, ilink_user_id: str, context_token: str) -> dict[str, Any]:
        return self.post("getconfig", {
            "ilink_user_id": ilink_user_id,
            "context_token": context_token,
        }, timeout=10)

    def send_typing(self, to_user_id: str, context_token: str, status: int = 1) -> dict[str, Any]:
        """Show the WeChat-side "对方正在输入..." indicator.

        iLink requires a short-lived typing_ticket from getconfig before
        sendtyping can be called. This mirrors the behavior used by Zyn.
        """
        config = self.get_config(to_user_id, context_token)
        typing_ticket = config.get("typing_ticket") or ""
        if not typing_ticket:
            raise ILinkError(f"没有获取到 typing_ticket：{config}")
        return self.post("sendtyping", {
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }, timeout=10)

    def post(self, endpoint: str, body: dict[str, Any], timeout: Optional[int] = None) -> dict[str, Any]:
        if not self.bot_token:
            raise ILinkError("缺少 bot_token，请先运行 wx2codex connect")
        payload = dict(body)
        payload["base_info"] = {"channel_version": "1.0.3"}
        url = f"{self.base_url}/ilink/bot/{endpoint.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise ILinkError(f"iLink HTTP {e.code}: {raw[:300] or e.reason}") from e
        except urllib.error.URLError as e:
            raise ILinkError(f"iLink 请求失败：{e}") from e
        if not raw.strip() or raw.strip() == "{}":
            return {"ret": 0}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ILinkError(f"iLink 返回非 JSON：{raw[:300]}") from e

    def _headers(self) -> dict[str, str]:
        random_uin = random.randint(0, 0xFFFFFFFF)
        wechat_uin = base64.b64encode(str(random_uin).encode()).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.bot_token}",
            "X-WECHAT-UIN": wechat_uin,
            "User-Agent": "wx2codex-agent/0.1",
        }

    @staticmethod
    def _get_json(url: str, headers: Optional[dict[str, str]] = None, timeout: int = 30) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "wx2codex-agent/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise ILinkError(f"iLink HTTP {e.code}: {raw[:300] or e.reason}") from e
        except urllib.error.URLError as e:
            raise ILinkError(f"iLink 请求失败：{e}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ILinkError(f"iLink 返回非 JSON：{raw[:300]}") from e


def extract_message_content(msg: dict[str, Any]) -> tuple[str, list[MediaAttachment]]:
    texts: list[str] = []
    attachments: list[MediaAttachment] = []
    for item in msg.get("item_list") or []:
        if item.get("type") == 1 or item.get("text_item"):
            text = ((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                texts.append(text)
            continue
        attachment = extract_media_attachment(item)
        if attachment:
            attachments.append(attachment)
    return "\n".join(texts).strip(), attachments


def extract_text_and_media(msg: dict[str, Any]) -> tuple[str, list[str]]:
    text, attachments = extract_message_content(msg)
    return text, [item.label for item in attachments]


def extract_media_attachment(item: dict[str, Any]) -> Optional[MediaAttachment]:
    if item.get("image_item"):
        image_item = item.get("image_item") or {}
        filename = image_item.get("filename") or image_item.get("file_name") or "image"
        return MediaAttachment("image", "[图片]", filename, extract_cdn_media(image_item), raw_item=item)
    if item.get("file_item"):
        file_item = item.get("file_item") or {}
        filename = (
            file_item.get("file_name")
            or file_item.get("filename")
            or file_item.get("name")
            or file_item.get("description")
            or "file.bin"
        )
        return MediaAttachment("file", f"[文件] {filename}", filename, extract_cdn_media(file_item), raw_item=item)
    if item.get("voice_item"):
        voice_item = item.get("voice_item") or {}
        duration = int(voice_item.get("playtime") or voice_item.get("duration") or 0)
        filename = voice_item.get("filename") or voice_item.get("file_name") or "voice.silk"
        return MediaAttachment("voice", "[语音]", filename, extract_cdn_media(voice_item), duration_ms=duration, raw_item=item)
    if item.get("video_item"):
        video_item = item.get("video_item") or {}
        duration = int(video_item.get("play_length") or video_item.get("duration") or 0)
        filename = video_item.get("filename") or video_item.get("file_name") or "video.mp4"
        return MediaAttachment("video", "[视频]", filename, extract_cdn_media(video_item), duration_ms=duration, raw_item=item)
    return None


def extract_cdn_media(media_item: dict[str, Any]) -> dict[str, Any]:
    cdn_media = dict(media_item.get("media") or {})
    aeskey = media_item.get("aeskey") or media_item.get("aes_key") or ""
    if aeskey and not cdn_media.get("aes_key"):
        cdn_media["aes_key"] = base64.b64encode(str(aeskey).encode("utf-8")).decode("utf-8")
    return cdn_media


def aes_ecb_encrypt_pkcs7(plain: bytes, key: bytes) -> bytes:
    try:
        from Cryptodome.Cipher import AES  # type: ignore
    except ImportError as e:
        raise ILinkError("缺少 pycryptodomex，无法加密上传图片；请重新安装 wx2codex") from e
    pad_len = 16 - (len(plain) % 16)
    padded = plain + bytes([pad_len]) * pad_len
    return AES.new(key, AES.MODE_ECB).encrypt(padded)


def aes_ecb_decrypt_pkcs7(encrypted: bytes, key: bytes) -> bytes:
    try:
        from Cryptodome.Cipher import AES  # type: ignore
    except ImportError as e:
        raise ILinkError("缺少 pycryptodomex，无法解密下载媒体；请重新安装 wx2codex") from e
    if len(encrypted) % 16 != 0:
        raise ILinkError("媒体密文长度不是 16 的倍数")
    plain = AES.new(key, AES.MODE_ECB).decrypt(encrypted)
    if not plain:
        return plain
    pad_len = plain[-1]
    if pad_len < 1 or pad_len > 16 or plain[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ILinkError("媒体解密 padding 校验失败")
    return plain[:-pad_len]


def decode_aes_key(aes_key_b64: str) -> bytes:
    try:
        decoded = base64.b64decode(aes_key_b64)
    except Exception as e:
        raise ILinkError("媒体 aes_key 不是合法 base64") from e
    if len(decoded) == 16:
        return decoded
    try:
        text = decoded.decode("utf-8")
        return bytes.fromhex(text)
    except Exception as e:
        raise ILinkError("媒体 aes_key 格式无法识别") from e


def detect_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WAVE":
        return "audio/wav"
    if len(data) > 9 and (data.startswith(b"#!SILK_V3") or (data[:1] == b"\x02" and data[1:10] == b"#!SILK_V3")):
        return "audio/silk"
    if data.startswith(b"#!AMR"):
        return "audio/amr"
    if len(data) > 8 and data[:4] == b"\x00\x00\x00" and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return "application/octet-stream"
