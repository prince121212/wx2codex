from __future__ import annotations

import glob
import json
import os
import queue
import socket
import struct
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .codex_app_server import (
    CodexTurnResult,
    dedupe_keep_order,
    first_line,
    remember_recent_threads,
    resolve_thread_ref,
)


class CodexDesktopIpcError(RuntimeError):
    pass


IPC_METHOD_VERSIONS: dict[str, int] = {
    "initialize": 0,
    "thread-follower-start-turn": 1,
    "thread-follower-load-complete-history": 1,
    "thread-follower-compact-thread": 1,
    "thread-follower-steer-turn": 1,
    "thread-follower-interrupt-turn": 2,
    "thread-follower-update-thread-settings": 1,
    "thread-follower-edit-last-user-turn": 1,
    "thread-follower-command-approval-decision": 1,
    "thread-follower-file-approval-decision": 1,
    "thread-follower-permissions-request-approval-response": 1,
    "thread-follower-submit-user-input": 1,
    "thread-follower-submit-mcp-server-elicitation-response": 1,
    "thread-follower-set-queued-follow-ups-state": 1,
    "thread-queued-followups-changed": 1,
    "client-status-changed": 0,
}


@dataclass
class DesktopThreadState:
    thread_id: str
    title: str = ""
    cwd: str = ""
    updated_at: float = 0
    raw: Optional[dict[str, Any]] = None


class DesktopIpcClient:
    """Client for the Codex desktop app's local IPC router.

    This talks to the already-running Codex desktop process through
    `$TMPDIR/codex-ipc/ipc-<uid>.sock`.  It does not spawn another Codex
    app-server, so user tasks run in the same desktop Codex environment that
    already has macOS privacy permissions.
    """

    def __init__(
        self,
        *,
        socket_path: str = "",
        auto_launch: bool = True,
        app_name: str = "Codex",
    ):
        self.socket_path = socket_path
        self.auto_launch = auto_launch
        self.app_name = app_name or "Codex"
        self.sock: Optional[socket.socket] = None
        self.client_id = "initializing-client"
        self._pending_broadcasts: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.thread_states: dict[str, DesktopThreadState] = {}

    @property
    def is_connected(self) -> bool:
        return self.sock is not None

    def connect(self, timeout: float = 15) -> None:
        if self.sock is not None:
            return
        path = self._wait_socket(timeout=timeout)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
        except OSError as e:
            raise CodexDesktopIpcError(f"无法连接 Codex 桌面 IPC：{path} ({e})") from e
        self.sock = sock
        init = self.request(
            "initialize",
            {"clientType": "wx2codex"},
            timeout=8,
            allow_uninitialized=True,
        )
        if isinstance(init, dict) and init.get("clientId"):
            self.client_id = str(init["clientId"])

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: float = 30,
        allow_uninitialized: bool = False,
    ) -> Any:
        if self.sock is None:
            self.connect()
        if self.sock is None:
            raise CodexDesktopIpcError("Codex 桌面 IPC 未连接")
        if not allow_uninitialized and self.client_id == "initializing-client":
            raise CodexDesktopIpcError("Codex 桌面 IPC 尚未初始化")
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "version": IPC_METHOD_VERSIONS.get(method, 0),
            "method": method,
            "params": params or {},
        }
        if timeout:
            payload["timeoutMs"] = int(max(1, timeout) * 1000)
        self._write(payload)
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise CodexDesktopIpcError(f"Codex 桌面 IPC 请求超时：{method}")
            message = self._read(timeout=min(1.0, remain))
            if not message:
                continue
            handled = self._handle_message(message)
            if handled:
                continue
            if message.get("type") == "response" and message.get("requestId") == request_id:
                if message.get("resultType") == "error":
                    raise CodexDesktopIpcError(str(message.get("error") or f"{method} failed"))
                return message.get("result")

    def pump(self, timeout: float = 0.5) -> Optional[dict[str, Any]]:
        if self.sock is None:
            self.connect()
        message = self._read(timeout=timeout)
        if not message:
            return None
        if self._handle_message(message):
            return None
        return message

    def drain_broadcasts(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(self._pending_broadcasts.get_nowait())
            except queue.Empty:
                return items

    def _wait_socket(self, timeout: float) -> str:
        deadline = time.time() + timeout
        launched = False
        while time.time() < deadline:
            path = self._find_socket_path()
            if path:
                return path
            if self.auto_launch and not launched:
                launched = True
                subprocess.run(["open", "-a", self.app_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
        raise CodexDesktopIpcError(
            "没有找到 Codex 桌面 IPC socket。请先打开 Codex 桌面 App，再重试。"
        )

    def _find_socket_path(self) -> str:
        if self.socket_path:
            p = Path(self.socket_path).expanduser()
            return str(p) if p.exists() else ""
        uid = os.getuid()
        candidates = [
            Path(tempfile.gettempdir()) / "codex-ipc" / f"ipc-{uid}.sock",
            Path("/tmp") / "codex-ipc" / f"ipc-{uid}.sock",
        ]
        candidates.extend(Path(p) for p in glob.glob(f"/var/folders/*/*/*/T/codex-ipc/ipc-{uid}.sock"))
        for p in candidates:
            if p.exists():
                return str(p)
        return ""

    def _write(self, payload: dict[str, Any]) -> None:
        if self.sock is None:
            raise CodexDesktopIpcError("Codex 桌面 IPC 未连接")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = struct.pack("<I", len(raw)) + raw
        try:
            self.sock.sendall(frame)
        except OSError as e:
            self.close()
            raise CodexDesktopIpcError(f"Codex 桌面 IPC 写入失败：{e}") from e

    def _read(self, timeout: float) -> Optional[dict[str, Any]]:
        if self.sock is None:
            raise CodexDesktopIpcError("Codex 桌面 IPC 未连接")
        self.sock.settimeout(timeout)
        try:
            header = self._recv_exact(4)
            if not header:
                self.close()
                raise CodexDesktopIpcError("Codex 桌面 IPC 已断开")
            size = struct.unpack("<I", header)[0]
            if size <= 0 or size > 256 * 1024 * 1024:
                raise CodexDesktopIpcError(f"Codex 桌面 IPC 帧长度异常：{size}")
            body = self._recv_exact(size)
            if not body:
                self.close()
                raise CodexDesktopIpcError("Codex 桌面 IPC 已断开")
            message = json.loads(body.decode("utf-8"))
            return message if isinstance(message, dict) else None
        except socket.timeout:
            return None
        except OSError as e:
            self.close()
            raise CodexDesktopIpcError(f"Codex 桌面 IPC 读取失败：{e}") from e
        except json.JSONDecodeError as e:
            raise CodexDesktopIpcError(f"Codex 桌面 IPC JSON 解析失败：{e}") from e

    def _recv_exact(self, size: int) -> bytes:
        if self.sock is None:
            return b""
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                return b""
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handle_message(self, message: dict[str, Any]) -> bool:
        typ = message.get("type")
        if typ == "broadcast":
            self._handle_broadcast(message)
            self._pending_broadcasts.put(message)
            return True
        if typ == "client-discovery-request":
            # wx2codex is a controller, not a desktop UI owner.  Always tell the
            # router that we cannot handle other clients' requests, otherwise
            # the desktop app may wait for us unnecessarily.
            self._write({
                "type": "client-discovery-response",
                "requestId": message.get("requestId"),
                "response": {"canHandle": False},
            })
            return True
        if typ == "request":
            self._write({
                "type": "response",
                "requestId": message.get("requestId"),
                "resultType": "error",
                "error": "no-handler-for-request",
            })
            return True
        return False

    def _handle_broadcast(self, message: dict[str, Any]) -> None:
        if message.get("method") != "thread-stream-state-changed":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        change = params.get("change")
        state = None
        if isinstance(change, dict):
            state = change.get("conversationState")
        if not isinstance(state, dict):
            state = params.get("conversationState")
        if not isinstance(state, dict):
            return
        thread = thread_state_from_conversation(state)
        if thread.thread_id:
            self.thread_states[thread.thread_id] = thread


class CodexDesktopController:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.client: Optional[DesktopIpcClient] = None

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def doctor(self) -> dict[str, Any]:
        client = self.ensure_client()
        client.connect()
        self.observe_for(1.0)
        return {
            "ok": True,
            "provider": "desktop",
            "socket_path": client._find_socket_path(),
            "client_id": client.client_id,
            "observed_threads": list(client.thread_states.keys()),
        }

    def list_threads(self, limit: int = 20, cwd: Optional[str] = None) -> list[dict[str, Any]]:  # noqa: ARG002
        client = self.ensure_client()
        client.connect()
        self.observe_for(1.2)
        observed = [thread_state_to_row(v) for v in client.thread_states.values()]
        cached = [t for t in (self.codex_config().get("recent_threads") or []) if isinstance(t, dict)]
        merged: dict[str, dict[str, Any]] = {}
        for item in observed + cached:
            thread_id = str(item.get("id") or "")
            if thread_id and thread_id not in merged:
                merged[thread_id] = item
        rows = list(merged.values())[:limit]
        remember_recent_threads(self.cfg, rows)
        return rows

    def start_thread(self, cwd: Optional[str] = None) -> str:  # noqa: ARG002
        active = self.active_thread_id()
        if active:
            self.codex_config()["current_thread_id"] = active
            return active
        raise CodexDesktopIpcError("desktop 模式不能后台新建线程。请先在 Codex 桌面打开一个线程，或发送 /use thread_id。")

    def use_thread(self, thread_ref: str) -> str:
        thread_id = resolve_thread_ref(self.cfg, thread_ref)
        if not thread_id and len(thread_ref.strip()) >= 8:
            thread_id = thread_ref.strip()
        if not thread_id:
            raise CodexDesktopIpcError(f"没有找到线程：{thread_ref}。请先运行 /threads 查看已观察到的线程。")
        self.codex_config()["current_thread_id"] = thread_id
        open_codex_thread(thread_id)
        return thread_id

    def run_turn(
        self,
        text: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        local_images: Optional[list[str]] = None,
    ) -> CodexTurnResult:
        client = self.ensure_client()
        client.connect()
        thread_id = self.codex_config().get("current_thread_id") or self.active_thread_id()
        if not thread_id:
            raise CodexDesktopIpcError("没有可用的 Codex 桌面线程。请先在 Codex 桌面打开一个线程。")
        # Bring the selected thread to the desktop app if possible.  This is not
        # UI automation; it only asks Codex to navigate to the existing thread.
        open_codex_thread(thread_id)
        self.observe_for(0.8)

        input_items: list[dict[str, Any]] = [{"type": "text", "text": text, "text_elements": []}]
        for image_path in local_images or []:
            if image_path:
                input_items.append({"type": "localImage", "path": str(image_path)})
        turn_start_params: dict[str, Any] = {"input": input_items}
        effective_cwd = cwd if cwd is not None else self.current_cwd()
        if effective_cwd:
            turn_start_params["cwd"] = effective_cwd

        response = client.request(
            "thread-follower-start-turn",
            {
                "conversationId": thread_id,
                "turnStartParams": turn_start_params,
            },
            timeout=60,
        )
        turn_start = response.get("result") if isinstance(response, dict) and isinstance(response.get("result"), dict) else response
        turn_id = extract_desktop_turn_id(turn_start)
        result = self.wait_turn_completed(thread_id, turn_id=turn_id, timeout=timeout)
        self.codex_config()["current_thread_id"] = result.thread_id
        remember_recent_threads(self.cfg, [{"id": result.thread_id, "title": first_line(text), "cwd": effective_cwd or "", "updated_at": int(time.time())}])
        return result

    def wait_turn_completed(
        self,
        thread_id: str,
        *,
        turn_id: str = "",
        timeout: Optional[int] = None,
    ) -> CodexTurnResult:
        client = self.ensure_client()
        deadline = time.time() + (timeout or int(self.codex_config().get("turn_timeout_seconds") or 3600))
        next_probe = 0.0
        last_turn: dict[str, Any] = {}

        while time.time() < deadline:
            now = time.time()
            if now >= next_probe:
                next_probe = now + 2.0
                try:
                    client.request(
                        "thread-follower-load-complete-history",
                        {"conversationId": thread_id},
                        timeout=10,
                    )
                except CodexDesktopIpcError:
                    # The regular stream may still be enough; surface a real
                    # failure only if the completion wait eventually times out.
                    pass

            state = client.thread_states.get(thread_id)
            if state and state.raw:
                turn = find_turn(state.raw, turn_id)
                if turn:
                    last_turn = turn
                    status = str(turn.get("status") or "")
                    if status and status != "inProgress":
                        assistant_text = assistant_text_from_turn(turn)
                        return CodexTurnResult(
                            thread_id=thread_id,
                            status=status,
                            assistant_text=assistant_text,
                            raw_turn=turn,
                        )

            try:
                client.pump(timeout=min(1.0, max(0.1, deadline - time.time())))
            except CodexDesktopIpcError:
                raise

        raise CodexDesktopIpcError(f"等待 Codex 桌面 turn 完成超时：thread={thread_id} turn={turn_id or '-'}")

    def observe_for(self, seconds: float) -> None:
        client = self.ensure_client()
        deadline = time.time() + max(0, seconds)
        while time.time() < deadline:
            client.pump(timeout=min(0.2, max(0.01, deadline - time.time())))

    def active_thread_id(self) -> str:
        client = self.ensure_client()
        client.connect()
        self.observe_for(0.5)
        if client.thread_states:
            rows = sorted(client.thread_states.values(), key=lambda item: item.updated_at, reverse=True)
            return rows[0].thread_id
        recent = self.codex_config().get("recent_threads") or []
        for item in recent:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return ""

    def current_cwd(self) -> str:
        value = self.codex_config().get("cwd") or ""
        return str(Path(value).expanduser()) if value else ""

    def codex_config(self) -> dict[str, Any]:
        return self.cfg.setdefault("codex", {})

    def ensure_client(self) -> DesktopIpcClient:
        if self.client and self.client.is_connected:
            return self.client
        ccfg = self.codex_config()
        self.client = DesktopIpcClient(
            socket_path=str(ccfg.get("desktop_socket_path") or ""),
            auto_launch=bool(ccfg.get("desktop_auto_launch", True)),
            app_name=str(ccfg.get("target_app") or "Codex"),
        )
        return self.client


def thread_state_from_conversation(state: dict[str, Any]) -> DesktopThreadState:
    thread_id = str(state.get("id") or state.get("sessionId") or state.get("conversationId") or "")
    title = str(state.get("title") or state.get("name") or "Codex 桌面线程")
    cwd = str(state.get("cwd") or (state.get("latestThreadSettings") or {}).get("cwd") or "")
    updated = state.get("updatedAt") or state.get("updated_at") or 0
    try:
        updated_at = float(updated) / (1000 if float(updated) > 10_000_000_000 else 1)
    except Exception:
        updated_at = time.time()
    return DesktopThreadState(thread_id=thread_id, title=title, cwd=cwd, updated_at=updated_at, raw=state)


def thread_state_to_row(state: DesktopThreadState) -> dict[str, Any]:
    return {
        "id": state.thread_id,
        "title": state.title or "Codex 桌面线程",
        "cwd": state.cwd,
        "updated_at": state.updated_at,
    }


def find_turn(conversation_state: dict[str, Any], turn_id: str = "") -> Optional[dict[str, Any]]:
    turns = conversation_state.get("turns")
    if not isinstance(turns, list):
        return None
    if turn_id:
        for turn in turns:
            if isinstance(turn, dict) and str(turn.get("turnId") or turn.get("id") or "") == turn_id:
                return turn
    for turn in reversed(turns):
        if isinstance(turn, dict):
            return turn
    return None


def assistant_text_from_turn(turn: dict[str, Any]) -> str:
    messages: list[str] = []
    for item in turn.get("items") or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        if typ in {"agentMessage", "assistantMessage", "assistant_message"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
            continue
        if typ in {"message", "assistant"} and item.get("role") == "assistant":
            text = item.get("text") or item.get("content")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())
    return "\n\n".join(dedupe_keep_order(messages)).strip()


def extract_desktop_turn_id(response: Any) -> str:
    if isinstance(response, dict):
        turn = response.get("turn")
        if isinstance(turn, dict):
            return str(turn.get("id") or turn.get("turnId") or "")
        return str(response.get("turnId") or response.get("id") or "")
    return ""


def open_codex_thread(thread_id: str) -> None:
    if not thread_id or os.uname().sysname != "Darwin":
        return
    # Both forms are harmless; current Codex builds understand the local route.
    url = f"codex://local/{thread_id}"
    subprocess.run(["open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

