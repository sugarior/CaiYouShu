"""A locally authorized, single-use acknowledgement, separate from AI task routing."""
from __future__ import annotations

import uuid

from .config import ConfigError
from .store import atomic_json, now, read_json


def reply_once_status(root):
    task = read_json(root / "reply-once.json")
    if task is None:
        return None
    result = {k: task.get(k) for k in ("id", "mode", "status", "armed_at", "source_mail", "job_id", "selected_at", "cancelled_at")}
    if task.get("status") == "queued" and task.get("job_id"):
        job = read_json(root / "outbox" / f"{task['job_id']}.json", {})
        status = job.get("status", "pending")
        result.update(status={"sent": "completed", "sent_partial": "completed"}.get(status, status),
                      recipients=job.get("recipients", []), sent_at=job.get("sent_at"))
    return result


def arm_reply_once(config, store, mode='ack'):
    if mode not in {'ack', 'csv'}:
        raise ConfigError('一次性回信模式必须是 ack 或 csv')
    if config.dry_run:
        raise ConfigError("当前为预览模式，请先在 config.yml 中关闭 dry_run，再启用一次性实际回信。")
    current = reply_once_status(store.root)
    if current and current["status"] not in {"completed", "cancelled", "blocked", "preview"}:
        raise ConfigError("已有一次性回信正在等待、发送或结果待核实；请先查看面板或取消。")
    task = {"schema": 1, "id": uuid.uuid4().hex, "mode": mode, "status": "armed", "armed_at": now()}
    atomic_json(store.root / "reply-once.json", task)
    return task


def cancel_reply_once(store):
    task = read_json(store.root / "reply-once.json")
    if task is not None:
        task.update(status="cancelled", cancelled_at=now())
        atomic_json(store.root / "reply-once.json", task)
