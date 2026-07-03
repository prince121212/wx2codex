from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class CloudError(RuntimeError):
    pass


class CloudClient:
    def __init__(self, base_url: str, agent_token: str = "", timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.agent_token = agent_token
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/ilink/register", payload, auth=False)

    def sync_context(self, to_user_id: str, context_token: str, last_inbound_at: str) -> dict[str, Any]:
        return self._request("POST", "/v1/ilink/context", {
            "to_user_id": to_user_id,
            "context_token": context_token,
            "last_inbound_at": last_inbound_at,
        })

    def notify(self, text: str, to_user_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if to_user_id:
            payload["to_user_id"] = to_user_id
        return self._request("POST", "/v1/notify/wechat", payload)

    def heartbeat(self) -> dict[str, Any]:
        return self._request("POST", "/v1/device/heartbeat", {})

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me")

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None, auth: bool = True) -> dict[str, Any]:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json", "User-Agent": "wx2codex-agent/0.1"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth:
            if not self.agent_token:
                raise CloudError("缺少 agent_token，请先运行 wx2codex connect")
            headers["Authorization"] = f"Bearer {self.agent_token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise CloudError(f"云端 HTTP {e.code}: {raw[:500] or e.reason}") from e
        except urllib.error.URLError as e:
            raise CloudError(f"无法连接云端：{e}") from e
        if not raw.strip():
            return {"ok": status < 400}
        try:
            data_obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CloudError(f"云端返回非 JSON：{raw[:300]}") from e
        if isinstance(data_obj, dict) and data_obj.get("ok") is False:
            raise CloudError(f"云端错误：{data_obj.get('error') or data_obj}")
        return data_obj
