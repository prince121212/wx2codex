from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class CodexAppServerError(RuntimeError):
    pass


@dataclass
class CodexTurnResult:
    thread_id: str
    status: str
    assistant_text: str
    raw_turn: dict[str, Any]


class CodexAppServerClient:
    """Minimal JSON-RPC client for `codex app-server --listen stdio://`.

    The protocol is newline-delimited JSON-RPC-ish messages over stdio:
    requests are `{id, method, params}`, notifications are `{method, params}`.
    Codex can also send requests back to the client, mainly for approvals.
    """

    def __init__(
        self,
        codex_binary: str = "codex",
        *,
        auto_approve: bool = False,
        env_path: str = "",
    ):
        self.codex_binary = codex_binary or "codex"
        self.auto_approve = auto_approve
        self.env_path = env_path
        self.process: Optional[subprocess.Popen[str]] = None
        self._next_id = 1
        self._pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notifications: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._stderr_lines: list[str] = []
        self._closed = False
        self.initialized = False

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.is_running:
            return
        binary = resolve_codex_binary(self.codex_binary)
        env = os.environ.copy()
        if self.env_path:
            env["PATH"] = self.env_path
        self.process = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read_stdout, name="wx2codex-codex-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="wx2codex-codex-stderr", daemon=True).start()

    def initialize(self) -> dict[str, Any]:
        if self.initialized:
            return {}
        self.start()
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wx2codex_agent",
                    "title": "wx2codex Agent",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self.notify("initialized", {})
        self.initialized = True
        return result if isinstance(result, dict) else {"result": result}

    def request(self, method: str, params: Optional[dict[str, Any]] = None, timeout: float = 30) -> Any:
        self.start()
        if not self.process or not self.process.stdin:
            raise CodexAppServerError("Codex app-server 未启动")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        payload: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
        self._write(payload)
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as e:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise CodexAppServerError(f"Codex app-server 请求超时：{method}") from e
        if "error" in response and response["error"]:
            error = response["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise CodexAppServerError(f"Codex app-server 错误（{method}）：{message}")
        return response.get("result")

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self.start()
        self._write({"method": method, "params": params or {}})

    def drain_notifications(self) -> None:
        while True:
            try:
                self._notifications.get_nowait()
            except queue.Empty:
                return

    def next_notification(self, timeout: float = 1) -> Optional[tuple[str, Any]]:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        proc = self.process
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.process = None

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines[-20:])

    def _write(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise CodexAppServerError("Codex app-server stdin 不可用")
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self.process.stdin.write(line + "\n")
                self.process.stdin.flush()
            except BrokenPipeError as e:
                raise CodexAppServerError(f"Codex app-server 已退出：{self.stderr_tail()}") from e

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            if self._closed:
                return
            raw = line.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            method = message.get("method")
            if isinstance(message_id, int) and method and "result" not in message and "error" not in message:
                self._handle_server_request(message_id, str(method), message.get("params"))
                continue
            if isinstance(message_id, int):
                with self._pending_lock:
                    response_queue = self._pending.pop(message_id, None)
                if response_queue:
                    response_queue.put(message)
                continue
            if isinstance(method, str):
                self._notifications.put((method, message.get("params")))

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            text = line.rstrip()
            if text:
                self._stderr_lines.append(text)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines = self._stderr_lines[-200:]

    def _handle_server_request(self, request_id: int, method: str, params: Any) -> None:
        try:
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                result = {"decision": "accept" if self.auto_approve else "decline"}
            elif method in {"item/tool/requestUserInput", "tool/requestUserInput"}:
                result = {"answers": []}
            else:
                result = {}
            self._write({"id": request_id, "result": result})
        except Exception as e:
            try:
                self._write({"id": request_id, "error": {"message": str(e)}})
            except Exception:
                pass


class CodexAppServerController:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.client: Optional[CodexAppServerClient] = None

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def doctor(self) -> dict[str, Any]:
        client = self.ensure_client()
        init = client.initialize()
        return {
            "ok": True,
            "provider": "app_server",
            "codex_binary": resolve_codex_binary(self.codex_config().get("codex_binary") or "codex"),
            "initialize": init,
        }

    def list_threads(self, limit: int = 20, cwd: Optional[str] = None) -> list[dict[str, Any]]:
        client = self.ensure_client()
        client.initialize()
        params: dict[str, Any] = {"limit": limit}
        effective_cwd = cwd if cwd is not None else self.current_cwd()
        if effective_cwd:
            params["cwd"] = effective_cwd
        response = client.request("thread/list", params, timeout=30)
        data = response.get("data") if isinstance(response, dict) else []
        threads = [normalize_thread(row) for row in data if isinstance(row, dict)]
        remember_recent_threads(self.cfg, threads)
        return threads

    def start_thread(self, cwd: Optional[str] = None) -> str:
        client = self.ensure_client()
        client.initialize()
        params = self.thread_start_params(cwd=cwd)
        response = client.request("thread/start", params, timeout=60)
        thread_id = extract_thread_id(response)
        if not thread_id:
            raise CodexAppServerError(f"Codex 没有返回 thread id：{response}")
        self.codex_config()["current_thread_id"] = thread_id
        remember_recent_threads(self.cfg, [{"id": thread_id, "title": "新线程", "cwd": params.get("cwd") or "", "updated_at": int(time.time())}])
        return thread_id

    def use_thread(self, thread_ref: str) -> str:
        thread_id = resolve_thread_ref(self.cfg, thread_ref)
        if not thread_id:
            # If it was not in the cached list, try refreshing and matching again.
            self.list_threads(limit=30)
            thread_id = resolve_thread_ref(self.cfg, thread_ref)
        if not thread_id:
            raise CodexAppServerError(f"没有找到线程：{thread_ref}。请先运行 /threads 查看可用线程。")
        self.codex_config()["current_thread_id"] = thread_id
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
        client.initialize()
        thread_id = self.codex_config().get("current_thread_id") or ""
        if thread_id:
            try:
                self.resume_thread(thread_id, cwd=cwd)
            except CodexAppServerError as e:
                if not is_missing_thread_error(str(e)):
                    raise
                # A thread created without any turn may not have a persisted
                # rollout after the app-server process exits. In that case,
                # transparently create a fresh thread for the incoming task.
                self.codex_config()["current_thread_id"] = ""
                thread_id = self.start_thread(cwd=cwd)
        else:
            thread_id = self.start_thread(cwd=cwd)
        client.drain_notifications()
        params = self.turn_start_params(thread_id=thread_id, text=text, cwd=cwd, local_images=local_images)
        response = client.request("turn/start", params, timeout=60)
        turn_id = extract_turn_id(response)
        result = self.wait_turn_completed(thread_id, turn_id=turn_id, timeout=timeout)
        self.codex_config()["current_thread_id"] = result.thread_id
        remember_recent_threads(self.cfg, [{"id": result.thread_id, "title": first_line(text), "cwd": params.get("cwd") or "", "updated_at": int(time.time())}])
        return result

    def resume_thread(self, thread_id: str, *, cwd: Optional[str] = None) -> None:
        client = self.ensure_client()
        params: dict[str, Any] = {"threadId": thread_id}
        effective_cwd = cwd if cwd is not None else self.current_cwd()
        if effective_cwd:
            params["cwd"] = effective_cwd
        approval_policy = self.codex_config().get("approval_policy") or "never"
        sandbox_mode = self.codex_config().get("sandbox_mode") or "workspace-write"
        params["approvalPolicy"] = approval_policy
        params["sandbox"] = sandbox_mode
        client.request("thread/resume", params, timeout=60)

    def wait_turn_completed(
        self,
        thread_id: str,
        *,
        turn_id: str = "",
        timeout: Optional[int] = None,
    ) -> CodexTurnResult:
        client = self.ensure_client()
        deadline = time.time() + (timeout or int(self.codex_config().get("turn_timeout_seconds") or 3600))
        assistant_by_item: dict[str, str] = {}
        completed_messages: list[str] = []
        last_turn: dict[str, Any] = {}

        while time.time() < deadline:
            note = client.next_notification(timeout=min(1.0, max(0.1, deadline - time.time())))
            if not note:
                continue
            method, params = note
            if not isinstance(params, dict):
                continue
            note_thread_id = params.get("threadId")
            if note_thread_id and note_thread_id != thread_id:
                continue

            if method in {"item/agentMessage/delta", "agent/message/delta"}:
                item_id = str(params.get("itemId") or params.get("item_id") or "")
                delta = params.get("delta")
                if item_id and isinstance(delta, str):
                    assistant_by_item[item_id] = assistant_by_item.get(item_id, "") + delta
                continue

            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and is_agent_message_item(item):
                    text = item.get("text")
                    item_id = str(item.get("id") or "")
                    if isinstance(text, str) and text.strip():
                        completed_messages.append(text.strip())
                    elif item_id and assistant_by_item.get(item_id, "").strip():
                        completed_messages.append(assistant_by_item[item_id].strip())
                continue

            if method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict):
                    last_turn = turn
                if turn_id:
                    completed_turn_id = ""
                    if isinstance(turn, dict):
                        completed_turn_id = str(turn.get("id") or "")
                    if completed_turn_id and completed_turn_id != turn_id:
                        continue
                status = str((turn or {}).get("status") or "completed") if isinstance(turn, dict) else "completed"
                assistant_text = "\n\n".join(dedupe_keep_order(completed_messages)).strip()
                if not assistant_text:
                    assistant_text = "\n\n".join(v.strip() for v in assistant_by_item.values() if v.strip())
                return CodexTurnResult(
                    thread_id=thread_id,
                    status=status,
                    assistant_text=assistant_text.strip(),
                    raw_turn=last_turn,
                )

        raise CodexAppServerError(f"等待 Codex turn 完成超时：thread={thread_id}")

    def thread_start_params(self, *, cwd: Optional[str] = None) -> dict[str, Any]:
        ccfg = self.codex_config()
        params: dict[str, Any] = {
            "approvalPolicy": ccfg.get("approval_policy") or "never",
            "sandbox": ccfg.get("sandbox_mode") or "workspace-write",
        }
        effective_cwd = cwd if cwd is not None else self.current_cwd()
        if effective_cwd:
            params["cwd"] = effective_cwd
        return params

    def turn_start_params(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: Optional[str] = None,
        local_images: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        ccfg = self.codex_config()
        input_items: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image_path in local_images or []:
            if image_path:
                input_items.append({"type": "localImage", "path": str(image_path)})
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            "approvalPolicy": ccfg.get("approval_policy") or "never",
            "sandboxPolicy": sandbox_policy(ccfg.get("sandbox_mode") or "workspace-write", bool(ccfg.get("network_access"))),
        }
        effective_cwd = cwd if cwd is not None else self.current_cwd()
        if effective_cwd:
            params["cwd"] = effective_cwd
        return params

    def current_cwd(self) -> str:
        value = self.codex_config().get("cwd") or ""
        return str(Path(value).expanduser()) if value else ""

    def codex_config(self) -> dict[str, Any]:
        return self.cfg.setdefault("codex", {})

    def ensure_client(self) -> CodexAppServerClient:
        if self.client and self.client.is_running:
            return self.client
        ccfg = self.codex_config()
        env_path = os.environ.get("PATH", "")
        self.client = CodexAppServerClient(
            ccfg.get("codex_binary") or "codex",
            auto_approve=bool(ccfg.get("auto_approve")),
            env_path=env_path,
        )
        return self.client


def resolve_codex_binary(value: str) -> str:
    value = str(value or "").strip() or "codex"
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded) and os.access(expanded, os.X_OK):
        return expanded
    found = shutil.which(expanded)
    if found:
        return found
    raise CodexAppServerError(f"找不到 codex 命令：{value}。请先安装 Codex CLI，或运行 wx2codex configure --codex-binary /path/to/codex")


def sandbox_policy(mode: str, network_access: bool = False) -> dict[str, Any]:
    if mode == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": network_access}
    return {"type": "workspaceWrite", "networkAccess": network_access}


def normalize_thread(row: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(row.get("id") or row.get("sessionId") or "")
    title = str(row.get("name") or row.get("preview") or "未命名线程").strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return {
        "id": thread_id,
        "title": title,
        "cwd": str(row.get("cwd") or ""),
        "updated_at": row.get("updatedAt") or row.get("createdAt") or 0,
        "status": row.get("status") or {},
    }


def remember_recent_threads(cfg: dict[str, Any], threads: list[dict[str, Any]]) -> None:
    ccfg = cfg.setdefault("codex", {})
    existing = [t for t in (ccfg.get("recent_threads") or []) if isinstance(t, dict)]
    merged: dict[str, dict[str, Any]] = {}
    for item in threads + existing:
        thread_id = item.get("id")
        if isinstance(thread_id, str) and thread_id and thread_id not in merged:
            merged[thread_id] = {
                "id": thread_id,
                "title": str(item.get("title") or item.get("preview") or "未命名线程"),
                "cwd": str(item.get("cwd") or ""),
                "updated_at": item.get("updated_at") or item.get("updatedAt") or 0,
            }
    ccfg["recent_threads"] = list(merged.values())[:30]


def resolve_thread_ref(cfg: dict[str, Any], ref: str) -> str:
    ref = ref.strip()
    if not ref:
        return ""
    recent = [t for t in (cfg.get("codex", {}).get("recent_threads") or []) if isinstance(t, dict)]
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(recent):
            return str(recent[idx].get("id") or "")
    for item in recent:
        thread_id = str(item.get("id") or "")
        if thread_id == ref or thread_id.startswith(ref):
            return thread_id
    if len(ref) >= 8:
        return ref
    return ""


def extract_thread_id(response: Any) -> str:
    if isinstance(response, dict):
        thread = response.get("thread")
        if isinstance(thread, dict):
            return str(thread.get("id") or thread.get("sessionId") or "")
        return str(response.get("threadId") or response.get("id") or "")
    return ""


def extract_turn_id(response: Any) -> str:
    if isinstance(response, dict):
        turn = response.get("turn")
        if isinstance(turn, dict):
            return str(turn.get("id") or "")
        return str(response.get("turnId") or "")
    return ""


def is_agent_message_item(item: dict[str, Any]) -> bool:
    typ = str(item.get("type") or "")
    return typ in {"agentMessage", "AgentMessage", "assistant_message", "assistantMessage"}


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def first_line(text: str, max_len: int = 80) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "新任务"
    return line[: max_len - 3] + "..." if len(line) > max_len else line


def is_missing_thread_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "no rollout found" in lowered
        or "thread not found" in lowered
        or "no archived rollout found" in lowered
    )


def format_threads(threads: list[dict[str, Any]], current_thread_id: str = "") -> str:
    if not threads:
        return "没有找到 Codex 线程。可以发送 /new 新建一个线程。"
    lines = ["最近 Codex 线程："]
    for idx, item in enumerate(threads, 1):
        marker = " ← 当前" if item.get("id") == current_thread_id else ""
        title = str(item.get("title") or "未命名线程").replace("\n", " ")
        thread_id = str(item.get("id") or "")
        cwd = str(item.get("cwd") or "")
        lines.append(f"{idx}. {title}{marker}\n   id: {thread_id}\n   cwd: {cwd or '-'}")
    lines.append("切换线程：/use 序号 或 /use thread_id")
    return "\n".join(lines)
