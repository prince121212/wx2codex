from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .attachments import SavedAttachment, format_attachment_prompt, save_wechat_attachments
from .cloud import CloudClient, CloudError
from .codex_app_server import (
    CodexAppServerController,
    CodexAppServerError,
    CodexTurnResult,
    format_threads,
    remember_recent_threads,
    resolve_codex_binary,
)
from .codex_bridge import CodexBridgeError, send_to_codex
from .codex_desktop_ipc import CodexDesktopController, CodexDesktopIpcError
from .config import app_dir, config_path, load_config, redacted, save_config
from .ilink import ILinkClient, ILinkError, extract_message_content
from .launchd import install_launch_agent, uninstall_launch_agent
from .outbound import extract_outbound_files, relay_outbound_files


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("已退出")
        return 130
    except (CloudError, ILinkError, CodexBridgeError, CodexAppServerError, CodexDesktopIpcError, RuntimeError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wx2codex", description="微信 iLink 与 Codex 的 macOS 本地桥接 Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("configure", help="配置云端地址或 Codex 连接方式")
    p.add_argument("--cloud-url", help="云端 Worker 地址，例如 https://codex.292828.xyz")
    p.add_argument("--codex-provider", choices=["desktop", "app_server", "ui"], help="Codex 连接方式：desktop、app_server 或 ui")
    p.add_argument("--codex-binary", help="codex CLI 路径；默认自动探测")
    p.add_argument("--cwd", help="默认项目目录，普通微信消息会发到这个目录下的 Codex 线程")
    p.add_argument("--approval-policy", choices=["never", "on-request", "on-failure", "untrusted"], help="Codex approval policy")
    p.add_argument("--sandbox-mode", choices=["read-only", "workspace-write", "danger-full-access"], help="Codex sandbox mode")
    p.add_argument("--network-access", choices=["0", "1", "true", "false"], help="workspace/read-only sandbox 下是否允许网络")
    p.add_argument("--auto-approve", choices=["0", "1", "true", "false"], help="旧 app_server provider 收到审批请求时是否自动同意；默认不同意")
    p.add_argument("--heartbeat-interval-seconds", type=int, help="云端心跳间隔秒数；默认 3600（1 小时）")
    p.add_argument("--target-app", help="Codex 应用名，默认 Codex")
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("connect", help="扫码连接微信 iLink，并注册到云端")
    p.add_argument("--no-cloud", action="store_true", help="只保存本地 token，不注册云端")
    p.add_argument("--timeout", type=int, default=120, help="等待扫码确认秒数")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("run", help="持续监听微信消息并转发到 Codex")
    p.add_argument("--once", action="store_true", help="只拉取一次后退出")
    p.add_argument("--no-codex", action="store_true", help="只打印消息并同步云端，不输入 Codex")
    p.add_argument("--poll-timeout", type=int, default=25, help="getupdates 请求超时秒数")
    p.add_argument("--sleep", type=float, default=0.6, help="空轮询后的暂停秒数")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("notify", help="通过云端给微信发送通知")
    p.add_argument("message", nargs="*", help="通知内容")
    p.add_argument("--to-user-id", default="", help="指定微信 iLink user_id；默认使用最近会话")
    p.add_argument("--no-prefix", action="store_true", help="不添加 wx2codex 前缀")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("status", help="查看本地配置与云端状态")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_status)

    p_codex = sub.add_parser("codex", help="管理 Codex 线程和项目目录")
    codex_sub = p_codex.add_subparsers(dest="codex_cmd", required=True)

    p = codex_sub.add_parser("doctor", help="检查 Codex 桌面/连接是否可用")
    p.set_defaults(func=cmd_codex_doctor)

    p = codex_sub.add_parser("threads", help="列出最近 Codex 线程")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="不按当前 cwd 过滤")
    p.set_defaults(func=cmd_codex_threads)

    p = codex_sub.add_parser("thread", help="查看当前 Codex 线程")
    p.set_defaults(func=cmd_codex_thread)

    p = codex_sub.add_parser("use", help="切换当前 Codex 线程")
    p.add_argument("thread_ref", help="thread_id、thread_id 前缀，或 threads 列表里的序号")
    p.set_defaults(func=cmd_codex_use)

    p = codex_sub.add_parser("new", help="新建 Codex 线程；可附带第一条任务")
    p.add_argument("prompt", nargs="*", help="可选：新线程的第一条任务")
    p.set_defaults(func=cmd_codex_new)

    p = codex_sub.add_parser("cwd", help="查看或切换默认项目目录")
    p.add_argument("path", nargs="?", help="项目目录")
    p.set_defaults(func=cmd_codex_cwd)

    p = codex_sub.add_parser("projects", help="列出最近使用过的项目目录")
    p.set_defaults(func=cmd_codex_projects)

    p = sub.add_parser("install-service", help="安装 macOS LaunchAgent 后台服务")
    p.add_argument("--no-codex", action="store_true", help="后台只打印/同步，不输入 Codex")
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("uninstall-service", help="卸载 macOS LaunchAgent 后台服务")
    p.set_defaults(func=cmd_uninstall_service)

    p = sub.add_parser("uninstall", help="完整卸载 wx2codex：后台服务、命令软链和本地数据")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认，直接卸载")
    p.add_argument("--dry-run", action="store_true", help="只显示将删除的内容，不实际删除")
    p.add_argument("--keep-data", action="store_true", help="保留 ~/.wx2codex 本地数据和虚拟环境，只移除后台服务和命令软链")
    p.set_defaults(func=cmd_uninstall)

    return parser


def cmd_configure(args: argparse.Namespace) -> int:
    cfg = load_config()
    codex_cfg = cfg.setdefault("codex", {})
    if args.cloud_url:
        cfg["cloud_url"] = args.cloud_url.rstrip("/")
    if args.codex_provider:
        codex_cfg["provider"] = args.codex_provider
    if args.codex_binary:
        codex_cfg["codex_binary"] = resolve_codex_binary(args.codex_binary)
    elif not codex_cfg.get("codex_binary"):
        codex_cfg["codex_binary"] = shutil.which("codex") or "codex"
    if args.cwd:
        codex_cfg["cwd"] = normalize_cwd(args.cwd)
        remember_project(cfg, codex_cfg["cwd"])
    if args.approval_policy:
        codex_cfg["approval_policy"] = args.approval_policy
    if args.sandbox_mode:
        codex_cfg["sandbox_mode"] = args.sandbox_mode
    if args.network_access is not None:
        codex_cfg["network_access"] = parse_bool(args.network_access)
    if args.auto_approve is not None:
        codex_cfg["auto_approve"] = parse_bool(args.auto_approve)
    if args.heartbeat_interval_seconds is not None:
        codex_cfg["heartbeat_interval_seconds"] = max(60, int(args.heartbeat_interval_seconds))
    if args.target_app:
        codex_cfg["target_app"] = args.target_app
    save_config(cfg)
    print(f"配置已保存：{config_path()}")
    print(f"cloud_url = {cfg.get('cloud_url')}")
    print(f"codex.provider = {codex_cfg.get('provider')}")
    print(f"codex.codex_binary = {codex_cfg.get('codex_binary')}")
    print(f"codex.cwd = {codex_cfg.get('cwd') or '-'}")
    print(f"codex.target_app = {codex_cfg.get('target_app')}")
    print(f"codex.heartbeat_interval_seconds = {codex_cfg.get('heartbeat_interval_seconds')}")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = ILinkClient()
    qr = client.get_bot_qrcode()
    qrcode_key = qr.get("qrcode") or ""
    qrcode_url = qr.get("qrcode_img_content") or ""
    if not qrcode_key:
        raise ILinkError(f"没有获取到二维码：{qr}")
    print("请用微信扫描下面的 iLink 二维码并确认连接：")
    print_qrcode(qrcode_url)
    result = client.wait_login(qrcode_key, timeout_seconds=args.timeout)

    cfg["bot_token"] = result.bot_token
    cfg["bot_id"] = result.bot_id
    cfg["ilink_user_id"] = result.ilink_user_id
    cfg.setdefault("cursor", "")
    save_config(cfg)
    print("微信 iLink 连接成功：")
    print(f"  ilink_user_id = {result.ilink_user_id}")
    print(f"  bot_id = {result.bot_id}")
    print(f"  bot_token = {redacted(result.bot_token)}")

    if not args.no_cloud:
        cloud = CloudClient(cfg["cloud_url"])
        resp = cloud.register({
            "ilink_user_id": result.ilink_user_id,
            "bot_id": result.bot_id,
            "bot_token": result.bot_token,
            "device_id": cfg.get("device_id"),
            "device_name": cfg.get("device_name"),
        })
        cfg["agent_token"] = resp.get("agent_token") or ""
        cfg["device_id"] = resp.get("device_id") or cfg.get("device_id")
        save_config(cfg)
        print("云端注册成功，agent_token 已保存。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.get("bot_token"):
        raise RuntimeError("缺少 bot_token，请先运行 wx2codex connect")
    ilink = ILinkClient(cfg["bot_token"])
    cloud = CloudClient(cfg["cloud_url"], cfg.get("agent_token") or "") if cfg.get("agent_token") else None
    provider = cfg.get("codex", {}).get("provider") or "desktop"
    codex_controller: Optional[Any] = None
    if provider in {"app_server", "desktop"} and not args.no_codex:
        codex_controller = create_codex_controller(cfg)
    stop = False

    def handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print("wx2codex-agent 已启动，正在监听微信消息...")
    print(f"Codex provider = {provider}")
    heartbeat_interval = max(60, int((cfg.get("codex") or {}).get("heartbeat_interval_seconds") or 3600))
    next_heartbeat_at = 0.0
    try:
        while not stop:
            changed = False
            try:
                result = ilink.get_updates(cfg.get("cursor") or "", timeout=args.poll_timeout)
            except ILinkError as e:
                print(f"[iLink] {e}", file=sys.stderr)
                time.sleep(3)
                if args.once:
                    return 1
                continue

            if result.get("get_updates_buf"):
                cfg["cursor"] = result["get_updates_buf"]
                changed = True

            messages = result.get("msgs") or []
            for msg in messages:
                if process_message(cfg, cloud, msg, no_codex=args.no_codex, codex_controller=codex_controller):
                    changed = True

            if changed:
                save_config(cfg)

            if cloud and time.monotonic() >= next_heartbeat_at:
                try:
                    cloud.heartbeat()
                    next_heartbeat_at = time.monotonic() + heartbeat_interval
                except CloudError as e:
                    print(f"[cloud heartbeat] {e}", file=sys.stderr)
                    next_heartbeat_at = time.monotonic() + min(300, heartbeat_interval)

            if args.once:
                break
            if not messages:
                time.sleep(args.sleep)
    finally:
        if codex_controller:
            codex_controller.close()
    return 0


def create_codex_controller(cfg: dict[str, Any]) -> Any:
    provider = cfg.get("codex", {}).get("provider") or "desktop"
    if provider == "desktop":
        return CodexDesktopController(cfg)
    return CodexAppServerController(cfg)


def process_message(
    cfg: dict[str, Any],
    cloud: Optional[CloudClient],
    msg: dict[str, Any],
    no_codex: bool = False,
    codex_controller: Optional[Any] = None,
) -> bool:
    from_user = msg.get("from_user_id") or ""
    context_token = msg.get("context_token") or ""
    text, attachments = extract_message_content(msg)
    changed = False
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if from_user and context_token:
        cfg.setdefault("contexts", {})[from_user] = {
            "context_token": context_token,
            "last_inbound_at": now,
        }
        changed = True
        if cloud:
            try:
                resp = cloud.sync_context(from_user, context_token, now)
                if resp.get("context_expires_at"):
                    cfg["contexts"][from_user]["context_expires_at"] = resp["context_expires_at"]
                membership = resp.get("membership") or {}
                if membership and membership.get("is_active") is False:
                    notice = resp.get("notice") or {}
                    payment = resp.get("payment") or {}
                    if notice.get("sent") or notice.get("ok"):
                        print(f"[会员] 已拦截过期用户消息；云端已发送支付通知：user={from_user} order={payment.get('order_id') or '-'}")
                    else:
                        print(
                            f"[会员] 已拦截过期用户消息；云端支付通知发送失败："
                            f"user={from_user} order={payment.get('order_id') or '-'} error={notice.get('error') or '-'}",
                            file=sys.stderr,
                        )
                    return True
            except CloudError as e:
                print(f"[cloud context] {e}", file=sys.stderr)
                print("[会员] 云端会员校验失败，已按 fail-closed 策略拦截本条消息。", file=sys.stderr)
                return True

    display_parts = []
    if attachments:
        display_parts.extend(item.label for item in attachments)
    if text:
        display_parts.append(text)
    display = "\n".join(display_parts).strip()
    if not display:
        return changed

    print(f"\n[微信] {from_user}: {display}")
    typing_indicator = start_typing_if_needed(cfg, from_user, context_token, display, no_codex=no_codex)
    try:
        if is_wx_command(display):
            reply, command_changed = handle_wx_command(cfg, display, codex_controller, allow_codex=not no_codex)
            changed = changed or command_changed
            send_wechat_reply(cloud, from_user, reply)
            return True

        if no_codex:
            return changed

        saved_attachments: list[SavedAttachment] = []
        if attachments:
            saved_attachments = save_wechat_attachments(
                cfg,
                ILinkClient(cfg.get("bot_token") or ""),
                attachments,
                from_user=from_user,
            )

        provider = cfg.get("codex", {}).get("provider") or "desktop"
        prefix = str(cfg.get("codex", {}).get("message_prefix") or "").strip()
        display_for_codex = format_attachment_prompt(display, saved_attachments)
        payload = build_codex_payload(
            display_for_codex,
            prefix=prefix,
            include_outbound_hint=should_include_outbound_hint(display),
        )
        local_images = [item.path for item in saved_attachments if item.is_image]

        if provider in {"app_server", "desktop"}:
            controller = codex_controller or create_codex_controller(cfg)
            should_close = codex_controller is None
            try:
                result = controller.run_turn(payload, local_images=local_images)
                print(f"[Codex {provider}] turn 完成：thread={result.thread_id} status={result.status}")
                reply_text, outbound_files = extract_outbound_files(format_turn_reply(result))
                if reply_text:
                    send_wechat_reply(cloud, from_user, reply_text)
                relay_errors = relay_outbound_files(
                    cfg,
                    to_user_id=from_user,
                    context_token=context_token,
                    paths=outbound_files,
                )
                if relay_errors:
                    send_wechat_reply(cloud, from_user, "\n".join(relay_errors))
                changed = True
            except (CodexAppServerError, CodexDesktopIpcError) as e:
                print(f"[Codex {provider}] 执行失败：{e}", file=sys.stderr)
                send_wechat_reply(cloud, from_user, f"❌ Codex 执行失败：{e}")
            finally:
                if should_close:
                    controller.close()
            return changed

        target_app = cfg.get("codex", {}).get("target_app") or "Codex"
        try:
            send_to_codex(payload, target_app=target_app)
            print("[Codex UI] 已输入并回车")
        except CodexBridgeError as e:
            print(f"[Codex UI] 输入失败：{e}", file=sys.stderr)
            send_wechat_reply(cloud, from_user, f"❌ Codex UI 输入失败：{e}")
        return changed
    finally:
        typing_indicator.stop()


def cmd_notify(args: argparse.Namespace) -> int:
    cfg = load_config()
    text = " ".join(args.message).strip() or "Codex 任务已完成，请回到电脑查看结果。"
    if not args.no_prefix:
        text = f"✅ wx2codex 通知\n{text}\n时间：{datetime.now().strftime('%H:%M:%S')}"
    cloud = CloudClient(cfg["cloud_url"], cfg.get("agent_token") or "")
    resp = cloud.notify(text, to_user_id=args.to_user_id)
    print(f"已发送微信通知：{resp.get('to_user_id', '')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    local = {
        "config_path": str(config_path()),
        "cloud_url": cfg.get("cloud_url"),
        "device_id": cfg.get("device_id"),
        "device_name": cfg.get("device_name"),
        "agent_token": redacted(cfg.get("agent_token") or ""),
        "ilink_user_id": cfg.get("ilink_user_id"),
        "bot_id": cfg.get("bot_id"),
        "bot_token": redacted(cfg.get("bot_token") or ""),
        "cursor_len": len(cfg.get("cursor") or ""),
        "codex": sanitize_codex_config(cfg.get("codex", {})),
        "contexts": {
            uid: {
                "context_token": redacted((ctx or {}).get("context_token") or ""),
                "last_inbound_at": (ctx or {}).get("last_inbound_at"),
                "context_expires_at": (ctx or {}).get("context_expires_at"),
            }
            for uid, ctx in (cfg.get("contexts") or {}).items()
        }
    }
    if args.json:
        print(json.dumps(local, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(local, ensure_ascii=False, indent=2))
        if cfg.get("agent_token"):
            try:
                remote = CloudClient(cfg["cloud_url"], cfg["agent_token"]).me()
                print("\n云端状态：")
                print(json.dumps(remote, ensure_ascii=False, indent=2))
            except CloudError as e:
                print(f"\n云端状态获取失败：{e}", file=sys.stderr)
    return 0


def cmd_codex_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    controller = create_codex_controller(cfg)
    try:
        result = controller.doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        controller.close()
    return 0


def cmd_codex_threads(args: argparse.Namespace) -> int:
    cfg = load_config()
    controller = create_codex_controller(cfg)
    try:
        threads = controller.list_threads(limit=max(1, args.limit), cwd="" if args.all else None)
        save_config(cfg)
        print(format_threads(threads, cfg.get("codex", {}).get("current_thread_id") or ""))
    finally:
        controller.close()
    return 0


def cmd_codex_thread(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    print(format_current_thread(cfg))
    return 0


def cmd_codex_use(args: argparse.Namespace) -> int:
    cfg = load_config()
    controller = create_codex_controller(cfg)
    try:
        thread_id = controller.use_thread(args.thread_ref)
        save_config(cfg)
        print(f"当前 Codex 线程已切换为：{thread_id}")
    finally:
        controller.close()
    return 0


def cmd_codex_new(args: argparse.Namespace) -> int:
    cfg = load_config()
    prompt = " ".join(args.prompt).strip()
    if (cfg.get("codex", {}) or {}).get("provider") == "desktop":
        cfg.setdefault("codex", {})["current_thread_id"] = ""
        save_config(cfg)
        if prompt:
            print("desktop 模式暂不支持从 CLI 后台自动新建并提交线程。请先在 Codex 桌面新建/打开线程后再发送任务。")
        else:
            print("desktop 模式已切换为跟随 Codex 桌面当前打开线程。")
        return 0
    if prompt:
        controller = create_codex_controller(cfg)
        try:
            result = controller.run_turn(prompt)
            save_config(cfg)
            print(format_turn_reply(result))
        finally:
            controller.close()
    else:
        cfg.setdefault("codex", {})["current_thread_id"] = ""
        save_config(cfg)
        print("已切换为新线程模式：下一条任务会自动创建一个新的 Codex 线程。")
    return 0


def cmd_codex_cwd(args: argparse.Namespace) -> int:
    cfg = load_config()
    codex_cfg = cfg.setdefault("codex", {})
    if not args.path:
        print(codex_cfg.get("cwd") or "未设置默认项目目录。可执行：wx2codex codex cwd /path/to/project")
        return 0
    path = normalize_cwd(args.path)
    codex_cfg["cwd"] = path
    remember_project(cfg, path)
    save_config(cfg)
    print(f"默认项目目录已切换为：{path}")
    return 0


def cmd_codex_projects(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    print(format_projects(cfg))
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    extra = ["--no-codex"] if args.no_codex else []
    path = install_launch_agent(extra_args=extra)
    print(f"LaunchAgent 已安装：{path}")
    print("日志目录：~/.wx2codex/logs")
    return 0


def cmd_uninstall_service(args: argparse.Namespace) -> int:  # noqa: ARG001
    uninstall_launch_agent()
    print("LaunchAgent 已卸载")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    install_dir = app_dir()
    targets = uninstall_targets(install_dir)
    print("wx2codex 将卸载以下内容：")
    print(f"- LaunchAgent: {targets['plist']}")
    for link in targets["bin_links"]:
        print(f"- 命令软链: {link}")
    if args.keep_data:
        print(f"- 保留本地数据目录: {install_dir}")
    else:
        print(f"- 本地数据目录: {install_dir}")

    if args.dry_run:
        print("dry-run：未执行任何删除。")
        return 0

    if not args.yes:
        answer = input("确认卸载？这会停止 wx2codex，并删除本地 token/日志/附件/虚拟环境。输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消卸载。")
            return 0

    uninstall_launch_agent()
    stop_running_agents()
    removed_links: list[Path] = []
    for link in targets["bin_links"]:
        if remove_owned_bin_link(Path(link), install_dir):
            removed_links.append(Path(link))

    if not args.keep_data and install_dir.exists():
        shutil.rmtree(install_dir)

    print("wx2codex 已卸载。")
    if removed_links:
        print("已删除命令软链：")
        for link in removed_links:
            print(f"- {link}")
    if args.keep_data:
        print(f"已保留本地数据目录：{install_dir}")
    return 0


def uninstall_targets(install_dir: Path) -> dict[str, Any]:
    candidates: set[Path] = {Path.home() / ".local" / "bin" / "wx2codex"}
    env_bin_dir = os.environ.get("WX2CODEX_BIN_DIR")
    if env_bin_dir:
        candidates.add(Path(env_bin_dir).expanduser() / "wx2codex")
    found = shutil.which("wx2codex")
    if found:
        candidates.add(Path(found).expanduser())
    return {
        "plist": Path.home() / "Library" / "LaunchAgents" / "xyz.292828.wx2codex.agent.plist",
        "bin_links": sorted(candidates, key=lambda p: str(p)),
    }


def remove_owned_bin_link(path: Path, install_dir: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if not path.is_symlink():
        print(f"跳过非软链命令文件：{path}")
        return False
    raw_target = os.readlink(path)
    target = Path(raw_target)
    if not target.is_absolute():
        target = (path.parent / target).resolve()
    else:
        target = target.resolve(strict=False)
    install_root = install_dir.expanduser().resolve(strict=False)
    try:
        owned = target.is_relative_to(install_root)
    except AttributeError:
        owned = str(target).startswith(str(install_root) + os.sep)
    if not owned:
        print(f"跳过非 wx2codex 安装目录软链：{path} -> {target}")
        return False
    path.unlink()
    return True


def stop_running_agents(timeout: float = 3.0) -> None:
    """Best-effort cleanup for manually started or slow-to-exit agents."""
    pattern = "wx2codex_agent run"
    subprocess.run(["pkill", "-f", pattern], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(["pgrep", "-f", pattern], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if not proc.stdout.strip():
            return
        time.sleep(0.2)


class TypingIndicator:
    def __init__(self, cfg: dict[str, Any], to_user_id: str, context_token: str, interval_seconds: int = 5):
        self.cfg = cfg
        self.to_user_id = to_user_id
        self.context_token = context_token
        self.interval_seconds = max(3, int(interval_seconds or 5))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "TypingIndicator":
        if not self.to_user_id or not self.context_token or not self.cfg.get("bot_token"):
            return self
        self._send_once()
        self._thread = threading.Thread(target=self._loop, name="wx2codex-typing", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._send_once()

    def _send_once(self) -> None:
        try:
            ILinkClient(self.cfg.get("bot_token") or "").send_typing(self.to_user_id, self.context_token)
            print("[微信] 已发送正在输入状态")
        except Exception as e:
            # typing 只是体验增强，失败不能影响任务主链路。
            print(f"[微信] 正在输入状态发送失败：{e}", file=sys.stderr)


class NullTypingIndicator:
    def stop(self) -> None:
        return


def start_typing_if_needed(
    cfg: dict[str, Any],
    to_user_id: str,
    context_token: str,
    text: str,
    *,
    no_codex: bool = False,
) -> Any:
    codex_cfg = cfg.get("codex", {}) or {}
    if no_codex or not codex_cfg.get("typing_indicator_enabled", True):
        return NullTypingIndicator()
    if not should_show_typing(text):
        return NullTypingIndicator()
    return TypingIndicator(
        cfg,
        to_user_id,
        context_token,
        interval_seconds=int(codex_cfg.get("typing_interval_seconds") or 5),
    ).start()


def should_show_typing(text: str) -> bool:
    stripped = text.strip()
    if not is_wx_command(stripped):
        return True
    command, _, rest = stripped.partition(" ")
    # 只有真正会触发 Codex 长任务的 slash command 才显示 typing。
    return command.lower() == "/new" and bool(rest.strip())


def is_wx_command(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("/") and len(stripped) > 1


def handle_wx_command(
    cfg: dict[str, Any],
    text: str,
    codex_controller: Optional[Any],
    *,
    allow_codex: bool = True,
) -> tuple[str, bool]:
    stripped = text.strip()
    command, _, rest = stripped.partition(" ")
    command = command.lower()
    arg = rest.strip()
    controller: Optional[Any] = codex_controller
    owned_controller = False

    def get_controller() -> Any:
        nonlocal controller, owned_controller
        if not controller:
            controller = create_codex_controller(cfg)
            owned_controller = True
        return controller

    try:
        if command in {"/help", "/?"}:
            return wx_help_text(), False

        if command == "/status":
            return format_wx_status(cfg), False

        if command == "/thread":
            return format_current_thread(cfg), False

        if command == "/threads":
            if not allow_codex:
                return "当前以 --no-codex 模式运行，不能读取 Codex 线程。", False
            threads = get_controller().list_threads(limit=20, cwd="" if arg.lower() in {"all", "全部"} else None)
            return format_threads(threads, cfg.get("codex", {}).get("current_thread_id") or ""), True

        if command == "/use":
            if not arg:
                return "用法：/use 序号 或 /use thread_id", False
            if not allow_codex:
                return "当前以 --no-codex 模式运行，不能切换 Codex 线程。", False
            thread_id = get_controller().use_thread(arg)
            return f"当前 Codex 线程已切换为：\n{thread_id}", True

        if command == "/new":
            if not allow_codex:
                return "当前以 --no-codex 模式运行，不能新建 Codex 线程。", False
            if (cfg.get("codex", {}) or {}).get("provider") == "desktop":
                cfg.setdefault("codex", {})["current_thread_id"] = ""
                if arg:
                    return "desktop 模式暂不支持从微信后台自动新建并提交线程。请先在 Codex 桌面新建/打开线程，然后直接发送这条任务。", True
                return "desktop 模式已切换为跟随 Codex 桌面当前打开线程。请在 Codex 桌面新建/打开线程后发送普通消息。", True
            if arg:
                result = get_controller().run_turn(arg)
                return format_turn_reply(result), True
            cfg.setdefault("codex", {})["current_thread_id"] = ""
            return "已切换为新线程模式：下一条普通微信消息会自动创建一个新的 Codex 线程。", True

        if command == "/cwd":
            codex_cfg = cfg.setdefault("codex", {})
            if not arg:
                return f"当前项目目录：\n{codex_cfg.get('cwd') or '未设置'}\n\n切换用法：/cwd /path/to/project", False
            path = normalize_cwd(arg)
            codex_cfg["cwd"] = path
            remember_project(cfg, path)
            return f"默认项目目录已切换为：\n{path}", True

        if command == "/projects":
            return format_projects(cfg), False

        return f"未知命令：{command}\n\n{wx_help_text()}", False
    except Exception as e:
        return f"❌ 命令执行失败：{e}", False
    finally:
        if owned_controller and controller:
            controller.close()


def wx_help_text() -> str:
    return "\n".join([
        "wx2codex 可用命令：",
        "/threads  列出最近 Codex 线程",
        "/threads all  列出全部最近线程",
        "/thread   查看当前线程",
        "/use 1    切换到线程列表第 1 个",
        "/use <thread_id>  切换到指定线程",
        "/new      app_server 模式新建线程；desktop 模式跟随桌面当前线程",
        "/new 任务说明  app_server 模式新建线程并发送第一条任务",
        "/cwd      查看当前项目目录",
        "/cwd /path/to/project  切换项目目录",
        "/projects 查看最近项目目录",
        "/status   查看连接状态",
        "/help     查看帮助",
    ])


def send_wechat_reply(cloud: Optional[CloudClient], to_user_id: str, text: str) -> None:
    if not text:
        return
    chunks = split_text_for_wechat(text)
    if not cloud:
        print(f"[微信回复/未配置云端] {text}")
        return
    try:
        for chunk in chunks:
            cloud.notify(chunk, to_user_id=to_user_id)
            # 避免连续长消息在微信侧偶发乱序或限流。
            if len(chunks) > 1:
                time.sleep(0.35)
        print(f"[微信回复] 已发送{len(chunks)}段")
    except CloudError as e:
        print(f"[微信回复] 发送失败：{e}", file=sys.stderr)


def split_text_for_wechat(text: str, max_len: int = 1800) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest.strip())
            break
        cut = rest.rfind("\n\n", 0, max_len)
        if cut < max_len // 3:
            cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 3:
            cut = max_len
        chunk = rest[:cut].strip()
        if chunk:
            chunks.append(chunk)
        rest = rest[cut:].lstrip()
    return chunks


def format_turn_reply(result: CodexTurnResult) -> str:
    text = result.assistant_text.strip()
    if not text:
        text = "Codex 已完成，但没有提取到最终文本。请回到电脑查看完整结果。"
    return text


def build_codex_payload(text: str, *, prefix: str = "", include_outbound_hint: bool = False) -> str:
    """Build the visible message inserted into the Codex desktop thread.

    Desktop IPC messages are shown in the Codex UI, so keep the common path as
    clean as possible: plain WeChat text in, plain Codex user message out.
    """
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    parts.append(text.strip())
    if include_outbound_hint:
        parts.append(codex_outbound_hint())
    return "\n".join(part for part in parts if part.strip()).strip()


def codex_outbound_hint() -> str:
    outbox = app_dir() / "outbox"
    try:
        outbox.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    display_outbox = display_path_for_prompt(outbox)
    return (
        "提示：如需把本地图片发回微信，请先保存/复制到 "
        f"{display_outbox}/ ，最终单独写一行：WX2CODEX_SEND_FILE: {display_outbox}/文件名.png"
    )


def display_path_for_prompt(path: Path) -> str:
    """Return a user-independent path when possible.

    `~/...` is understood by shells and is expanded by wx2codex before reading
    the file, so prompts do not leak or bake in the developer's home directory.
    """
    try:
        home = Path.home().resolve()
        resolved = path.expanduser().resolve()
        rel = resolved.relative_to(home)
        return "~" if str(rel) == "." else f"~/{rel.as_posix()}"
    except Exception:
        return str(path)


def should_include_outbound_hint(text: str) -> bool:
    """Only show the WX2CODEX_SEND_FILE hint when the user likely needs media back."""
    lowered = text.lower()
    keywords = [
        "截图",
        "截屏",
        "截个屏",
        "截一下屏",
        "屏幕",
        "当前屏",
        "全屏",
        "图片",
        "照片",
        "发图",
        "图发",
        "发给我",
        "发我",
        "传给我",
        "发到微信",
        "发送到微信",
        "传到微信",
        "二维码",
        "png",
        "jpg",
        "jpeg",
        "image",
        "photo",
        "screenshot",
    ]
    return any(word in lowered for word in keywords)


def format_wx_status(cfg: dict[str, Any]) -> str:
    codex_cfg = cfg.get("codex", {}) or {}
    contexts = cfg.get("contexts") or {}
    return "\n".join([
        "wx2codex 状态：",
        f"provider: {codex_cfg.get('provider') or 'desktop'}",
        f"cwd: {codex_cfg.get('cwd') or '-'}",
        f"thread: {codex_cfg.get('current_thread_id') or '-'}",
        f"codex_binary: {codex_cfg.get('codex_binary') or 'codex'}",
        f"ilink_user_id: {cfg.get('ilink_user_id') or '-'}",
        f"最近微信会话数: {len(contexts)}",
    ])


def format_current_thread(cfg: dict[str, Any]) -> str:
    codex_cfg = cfg.get("codex", {}) or {}
    current = codex_cfg.get("current_thread_id") or ""
    if not current:
        return "当前没有选择 Codex 线程。可发送 /threads 查看，或 /new 新建。"
    for item in codex_cfg.get("recent_threads") or []:
        if isinstance(item, dict) and item.get("id") == current:
            return "\n".join([
                "当前 Codex 线程：",
                f"id: {current}",
                f"title: {item.get('title') or '-'}",
                f"cwd: {item.get('cwd') or codex_cfg.get('cwd') or '-'}",
            ])
    return f"当前 Codex 线程：\n{current}"


def format_projects(cfg: dict[str, Any]) -> str:
    codex_cfg = cfg.get("codex", {}) or {}
    projects = codex_cfg.get("projects") or {}
    if not isinstance(projects, dict) or not projects:
        return "暂无项目目录记录。可发送 /cwd /path/to/project 设置。"
    current = codex_cfg.get("cwd") or ""
    rows = sorted(projects.values(), key=lambda item: (item or {}).get("last_used_at", ""), reverse=True)
    lines = ["最近项目目录："]
    for idx, item in enumerate(rows[:20], 1):
        if not isinstance(item, dict):
            continue
        path = item.get("path") or ""
        marker = " ← 当前" if path == current else ""
        lines.append(f"{idx}. {path}{marker}")
    lines.append("切换项目：/cwd /path/to/project")
    return "\n".join(lines)


def remember_project(cfg: dict[str, Any], path: str) -> None:
    codex_cfg = cfg.setdefault("codex", {})
    projects = codex_cfg.setdefault("projects", {})
    projects[path] = {"path": path, "last_used_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def normalize_cwd(path: str) -> str:
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    expanded = expanded.resolve()
    if not expanded.exists():
        raise RuntimeError(f"项目目录不存在：{expanded}")
    if not expanded.is_dir():
        raise RuntimeError(f"不是目录：{expanded}")
    return str(expanded)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def short_id(value: str, keep: int = 8) -> str:
    if not value:
        return "-"
    return value if len(value) <= keep * 2 else f"{value[:keep]}…{value[-keep:]}"


def sanitize_codex_config(codex_cfg: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "provider",
        "fallback_provider",
        "target_app",
        "input_mode",
        "desktop_socket_path",
        "desktop_auto_launch",
        "codex_binary",
        "cwd",
        "current_thread_id",
        "approval_policy",
        "sandbox_mode",
        "network_access",
        "turn_timeout_seconds",
        "heartbeat_interval_seconds",
        "auto_approve",
        "typing_indicator_enabled",
        "typing_interval_seconds",
        "recent_threads",
        "projects",
    ]
    return {key: codex_cfg.get(key) for key in keys if key in codex_cfg}


def print_qrcode(qrcode_url: str) -> None:
    if not qrcode_url:
        print("未返回二维码内容")
        return
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        for row in matrix:
            print("".join("██" if cell else "  " for cell in row))
    except Exception:
        print("无法渲染终端二维码，请复制下面的二维码内容到浏览器/二维码工具：")
        print(qrcode_url)


if __name__ == "__main__":
    raise SystemExit(main())
