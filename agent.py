"""Standalone JSON email agent, with an optional local read-only dashboard."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import threading

from filelock import FileLock, Timeout

from mailagent.config import Config, ConfigError, ROOT
from mailagent.engine import Engine
from mailagent.mailio import archive_folders, close_smtp, imap_connection, smtp_connection
from mailagent.reply_once import arm_reply_once, cancel_reply_once
from mailagent.store import Store, atomic_json, now, read_json

LOG = logging.getLogger("email-agent")


class Service:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.runtime = self.path.parent / "data"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.mutex = threading.Lock()
        self.state = {"pid": os.getpid(), "started_at": now(), "state": "starting"}
        self.store = None

    def status(self, **values):
        with self.mutex:
            self.state.update(values, heartbeat_at=now())
            atomic_json(self.runtime / "service-status.json", self.state)

    def heartbeat(self):
        while not self.stop.wait(5):
            self.status()

    def wait(self, seconds):
        # A config edit or stop marker wakes the loop within two seconds.
        try:
            signature = (self.path.stat().st_mtime_ns, self.path.stat().st_size)
        except OSError:
            signature = None
        deadline = datetime.now(timezone.utc).timestamp() + seconds
        while not self.stop.wait(min(2, max(0, deadline - datetime.now(timezone.utc).timestamp()))):
            if (self.runtime / "stop.request").exists():
                return
            try:
                changed = (self.path.stat().st_mtime_ns, self.path.stat().st_size) != signature
            except OSError:
                changed = signature is not None
            if changed or datetime.now(timezone.utc).timestamp() >= deadline:
                return

    def run(self, once=False):
        self.status()
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        try:
            while not self.stop.is_set():
                if (self.runtime / "stop.request").exists():
                    break
                interval = 15
                try:
                    cfg = Config.load(self.path)
                    missing = cfg.missing()
                    if missing:
                        self.status(state="waiting_configuration", missing=missing, last_error=None)
                    else:
                        if self.store is None or self.store.root != cfg.storage / "accounts" / cfg.account_id:
                            self.store = Store(cfg)
                            self.store.recover_outbox()
                        self.status(state="syncing", missing=[], last_error=None, account_id=cfg.account_id)
                        stopping = lambda: self.stop.is_set() or (self.runtime / "stop.request").exists()
                        engine = Engine(cfg, self.store, stop_requested=stopping)
                        try:
                            result = archive_folders(cfg, self.store, stop_requested=stopping)
                        except Exception as exc:
                            result = {"fetched": 0, "errors": [{"error_type": type(exc).__name__}]}
                            LOG.warning("收信连接失败；已有归档和待发任务继续处理；类型=%s", type(exc).__name__)
                        self.status(state="processing", sync=result)
                        engine.process_reply_once()
                        handled = engine.process_pending()
                        engine.process_reply_once()
                        # Reload before delivery so a newly saved pause/config change is respected.
                        latest = Config.load(self.path)
                        if not latest.missing() and latest.account_id == cfg.account_id:
                            Engine(latest, self.store, stop_requested=stopping).deliver()
                        counts = Counter(job["status"] for _, job in self.store.jobs())
                        from mailagent.procurement import snapshot
                        self.status(state="running" if not result.get("errors") else "retrying_mail_sync", last_cycle_at=now(),
                                    archived_count=self.store.state["archived_count"],
                                    pending_analysis=len(self.store.state["queue"]), outbox=dict(counts),
                                    last_processed=handled, last_error=None, procurement=snapshot(cfg))
                        interval = cfg.poll_seconds
                except ConfigError as exc:
                    self.status(state="waiting_configuration", last_error=str(exc))
                except Exception as exc:
                    # Error class only. Provider error strings sometimes contain mail or credentials.
                    self.status(state="retrying", last_error=type(exc).__name__)
                    LOG.warning("本轮未完成，将自动重试；类型=%s", type(exc).__name__)
                if once:
                    break
                self.wait(interval)
        finally:
            self.stop.set()
            thread.join(timeout=6)
            self.status(state="stopped", stopped_at=now())
            (self.runtime / "stop.request").unlink(missing_ok=True)


def configure_logging(root):
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(root / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main():
    parser = argparse.ArgumentParser(description="163 + DeepSeek JSON 邮件助手")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="后台循环，未配置时等待配置")
    commands.add_parser("once", help="处理一轮（服务必须已停止）")
    commands.add_parser("status", help="只读查看状态，可与后台进程同时执行")
    commands.add_parser("stop", help="请求后台进程完成本轮后退出")
    check = commands.add_parser("check", help="检查 IMAP/SMTP 登录，不发送邮件")
    check.add_argument("--ai", action="store_true", help="额外发送不含邮件内容的模型连通测试")
    resolve = commands.add_parser("resolve", help="人工核实 SMTP 不确定状态后再执行（服务须停止）")
    resolve.add_argument("job_id")
    resolve.add_argument("--as", dest="resolution", choices=["sent", "retry", "cancelled"], required=True)
    reply = commands.add_parser("reply-once", help="启用下一封普通来信回复‘收到’（先停止服务）")
    reply.add_argument("--cancel", action="store_true", help="取消尚未发送的一次性回信")
    reply.add_argument('--mode', choices=['ack', 'csv'], default='ack', help='ack 回复收到；csv 回复下一封 CSV 的 AI 摘要及提取内容')
    commands.add_parser('procurement-enable', help='启用本账号的控制邮件询价及供应商报价入库')
    commands.add_parser('procurement-disable', help='暂停本账号的采购自动流程')
    commands.add_parser('procurement-status', help='只读查看采购数据库和自动流程状态')
    args = parser.parse_args()
    path = Path(args.config).resolve()
    runtime = path.parent / "data"
    runtime.mkdir(parents=True, exist_ok=True)
    if args.command == "status":
        print(json.dumps(read_json(runtime / "service-status.json", {"state": "not_started"}), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'procurement-status':
        from mailagent.procurement import snapshot
        print(json.dumps(snapshot(Config.load(path)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        (runtime / "stop.request").touch()
        print("已请求停止；正在进行的网络操作完成或超时后退出。")
        return 0
    configure_logging(runtime)
    try:
        with FileLock(str(runtime / "service.lock"), timeout=0):
            if args.command in {"run", "once"}:
                (runtime / "stop.request").unlink(missing_ok=True)
                service = Service(path)
                signal.signal(signal.SIGINT, lambda *_: service.stop.set())
                signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
                service.run(once=args.command == "once")
            else:
                cfg = Config.load(path)
                if cfg.missing():
                    raise ConfigError("请先填写：" + ", ".join(cfg.missing()))
                if args.command in {'procurement-enable', 'procurement-disable'}:
                    from mailagent.procurement import enable, disable
                    from mailagent.config import update_config_section
                    update_config_section(path,'procurement',{'enabled':args.command=='procurement-enable'})
                    cfg=Config.load(path)
                    store = Store(cfg)
                    if args.command == 'procurement-enable':
                        options = enable(cfg, store)
                        cancel_reply_once(store)
                        print('采购自动流程已启用。生效时间：' + options['enabled_at'])
                    else:
                        disable(cfg, store)
                        print('采购自动流程已暂停；未发送的询价保留在数据库中。')
                elif args.command == "check":
                    with imap_connection(cfg) as box:
                        folders = box.folder.list()
                        print(f"IMAP 登录成功，可见 {len(folders)} 个文件夹")
                    client = smtp_connection(cfg)
                    close_smtp(client)
                    print("SMTP 登录成功，未发送邮件")
                    if args.ai:
                        from mailagent.ai import DeepSeek
                        result = DeepSeek(cfg).request('输出 JSON {"ok":true}', {"test": True})
                        if result.get("ok") is not True:
                            raise ConfigError("模型连通测试未返回预期结果")
                        print("DeepSeek JSON 响应正常")
                elif args.command == "reply-once":
                    store = Store(cfg)
                    if args.cancel:
                        cancel_reply_once(store)
                        print("一次性回信已取消；已经发送或结果不确定的邮件不会撤回。")
                    else:
                        task = arm_reply_once(cfg, store, args.mode)
                        print(('下一封带 CSV 附件的普通来信将回复 AI 摘要和提取内容。' if args.mode == 'csv' else
                               '下一封普通来信将回复‘收到’。') + '仅执行一次。生效时间：' + task['armed_at'])
                elif args.command == "resolve":
                    store = Store(cfg)
                    if not __import__("re").fullmatch(r"[0-9a-f]{64}", args.job_id):
                        raise ConfigError("job_id 格式无效")
                    job_path = store.root / "outbox" / f"{args.job_id}.json"
                    job = read_json(job_path)
                    if job is None or job["status"] not in {"sending", "uncertain"}:
                        raise ConfigError("只可处理 sending/uncertain 发件记录")
                    job.update(status={"sent": "sent", "retry": "pending", "cancelled": "cancelled"}[args.resolution],
                               manually_resolved_at=now(), retry_after=None)
                    atomic_json(job_path, job)
                    print("发件记录已更新；retry 仍会经过当前任务权限检查。")
        return 0
    except Timeout:
        print("服务已经运行；维护操作请先停止服务。")
        return 2
    except ConfigError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print("操作未完成，错误类型：" + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
