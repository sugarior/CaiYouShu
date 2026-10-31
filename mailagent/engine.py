from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import re
import smtplib

import yaml

from .ai import DeepSeek
from .config import address
from .mailio import build_message, close_smtp, new_message_id, smtp_connection
from .store import atomic_json, now, read_json
from .tabular import ensure_extractions, render_extractions, table_kind
from .procurement import Procurement
from procurementdb import DataError

LOG = logging.getLogger("email-agent")
TASK_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")


class CommandError(ValueError):
    pass


def string(value, label, maximum=12000, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum or "\x00" in value:
        raise CommandError(f"{label} 缺失或长度/格式有误")
    return value.strip()


def task_id(value):
    if not isinstance(value, str) or not TASK_ID.fullmatch(value):
        raise CommandError("任务 id 需为 1–64 位小写字母、数字、下划线或连字符")
    return value


def time_value(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def received_time(item):
    candidates = []
    for source in item["sources"]:
        flags = {v.lower() for v in source.get("folder_flags", []) + source.get("flags", [])}
        folder = source["folder"].lower().split("/")[-1]
        if flags & {"\\sent", "\\drafts", "\\draft"} or folder in {
            "sent", "sent messages", "sent items", "drafts", "已发送", "已发送邮件", "草稿", "草稿箱"
        }:
            continue
        date = time_value(source.get("received_at"))
        if date is not None:
            candidates.append(date)
    return min(candidates) if candidates else None


def job_key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class Engine:
    def __init__(self, config, store, ai=None, smtp_factory=smtp_connection, stop_requested=lambda: False):
        self.config, self.store = config, store
        self.ai = ai if ai is not None else DeepSeek(config)
        self.smtp_factory = smtp_factory
        self.stop_requested = stop_requested
        if config.control_mode == 'natural_language' and not store.state.get('natural_control_enabled_at'):
            store.state['natural_control_enabled_at'] = now()
            store.save_state()

    def reply_once_candidate(self, item, task):
        parsed = item["parsed"]
        if task.get('mode') == 'csv' and not any(table_kind(v) == 'csv' for v in parsed.get('attachments', [])):
            return None
        received, armed = received_time(item), time_value(task.get("armed_at"))
        if not received or not armed or received <= armed:
            return None
        if parsed.get("agent_marker") or parsed.get("parse_error") or parsed.get("parse_defects"):
            return None
        if parsed.get("subject", "").lstrip().startswith(self.config.subject_prefix):
            return None
        senders = parsed.get("from", [])
        if parsed.get("from_header_count") != 1 or len(senders) != 1:
            return None
        try:
            sender = address(senders[0])
        except ValueError:
            return None
        local = sender.split("@", 1)[0]
        if sender == self.config.address or local in {"mailer-daemon", "postmaster", "noreply", "no-reply", "do-not-reply"}:
            return None
        headers = {}
        for key, value in parsed.get("headers", []):
            headers.setdefault(key.lower(), []).append(value.strip().lower())
        if any(v != "no" for v in headers.get("auto-submitted", [])):
            return None
        if any(k in headers for k in ("list-id", "list-unsubscribe", "x-auto-response-suppress")):
            return None
        if any(v in {"bulk", "list", "junk"} for v in headers.get("precedence", [])):
            return None
        if "<>" in headers.get("return-path", []) or any("multipart/report" in v for v in headers.get("content-type", [])):
            return None
        return sender

    def reply_once_job(self, item, task):
        sender = self.reply_once_candidate(item, task)
        if sender is None:
            return None
        key = job_key("reply-once", task["id"])
        subject = re.sub(r"[\r\n\x00]", " ", item["parsed"].get("subject", ""))[:220].strip()
        if not subject.lower().startswith("re:"):
            subject = "Re: " + (subject or "（无主题）")
        body = '收到'
        if task.get('mode') == 'csv':
            if item.get('analysis') is None:
                return None  # Wait for the normal AI queue; failed API calls retain the same source.
            body = ('已收到 CSV 附件，以下为读取结果。\n\nAI 摘要：\n' + item['analysis'].get('summary', '') +
                    '\n\n以下内容由解析器直接提取，未由 AI 重写：\n\n' +
                    render_extractions(item['parsed'], csv_only=True))
        job = {"kind": "reply_once", "reply_once_id": task["id"], "source_mail": item["id"],
               "recipients": [sender], "subject": subject, "body": body,
               "message_id": new_message_id(self.config, key)}
        original_id = item["parsed"].get("message_id", "")
        if re.fullmatch(r"<[^<>\s\x00-\x1f\x7f]{1,240}>", original_id):
            job["in_reply_to"] = original_id
        return key, job

    def process_reply_once(self):
        task_path = self.store.root / "reply-once.json"
        task = read_json(task_path)
        if task is None or task.get("status") not in {"armed", "queued"} or self.stop_requested():
            return
        if task["status"] == "armed":
            selected = None
            # Include already analyzed mail too, so interruption never loses the selected arrival.
            for path in (self.store.root / "mail").glob("*.json"):
                if self.stop_requested():
                    return
                item = read_json(path)
                if self.reply_once_candidate(item, task):
                    rank = (received_time(item), item["archived_at"], item["id"])
                    if selected is None or rank < selected[0]:
                        selected = (rank, item)
            if selected is None:
                return
            item = selected[1]
            key = job_key('reply-once', task['id'])
            # Reserve exactly one source before enqueueing. Restart can finish the same reservation.
            task.update(status="queued", source_mail=item["id"], job_id=key, selected_at=now())
            atomic_json(task_path, task)
        item = self.store.mail(task["source_mail"])
        expected = self.reply_once_job(item, task) if item else None
        if expected and expected[0] == task["job_id"]:
            self.store.enqueue(*expected)

    def control_sender(self, item):
        parsed = item["parsed"]
        senders = parsed.get("from", [])
        if parsed.get("from_header_count") != 1 or len(senders) != 1:
            return None
        return senders[0] if senders[0] in self.config.allowed_senders else None

    def task_fields(self, values, previous=None, request=None):
        if not isinstance(values, dict) or set(values) - {"id", "name", "condition", "recipients", "instruction"}:
            raise CommandError("task 只接受 id/name/condition/recipients/instruction")
        merged = dict(previous or {})
        merged.update(values)
        identity = task_id(merged.get("id"))
        result = {"id": identity,
                  "name": string(merged.get("name", identity), "name", 200),
                  "condition": string(merged.get("condition"), "condition", 8000),
                  "instruction": string(merged.get("instruction", "用中文简要说明来信要点、需要采取的行动及明确给出的截止日期。"), "instruction", 8000)}
        recipients = merged.get("recipients")
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
            raise CommandError("recipients 必须是包含 1–10 个明确邮箱地址的列表")
        try:
            result["recipients"] = list(dict.fromkeys(address(v) for v in recipients))
        except ValueError:
            raise CommandError("recipients 中存在无效邮箱地址") from None
        if request is not None and "recipients" in values:
            explicit = {v.rstrip(".").lower() for v in EMAIL_IN_TEXT.findall(request)}
            if not set(result["recipients"]) <= explicit:
                raise CommandError("模型提出了请求中未明确出现的收件人，请在邮件中写出完整收件邮箱")
        return result

    def apply_command(self, command, sender, source_id, request=None):
        document = self.store.tasks()
        if source_id in document["applied_commands"]:
            return document["applied_commands"][source_id]
        if not isinstance(command, dict):
            raise CommandError("控制命令必须是对象")
        action = command.get("action")
        tasks = document["items"]
        timestamp = now()
        if action == "list":
            result = {"ok": True, "action": "list", "tasks": [
                {k: v[k] for k in ("id", "name", "enabled", "recipients", "condition", "instruction", "version")}
                for v in tasks.values() if not v.get("deleted")]}
        elif action == "create":
            task = self.task_fields(command.get("task"), request=request)
            identity = task["id"]
            if identity in tasks:
                raise CommandError("任务 id 已使用，请使用新 id；修改已有任务请用 update")
            task.update(owner=sender, version=1, enabled=True, deleted=False, created_at=timestamp,
                        updated_at=timestamp, effective_at=timestamp)
            tasks[identity] = task
            result = {"ok": True, "action": action, "task_id": identity, "version": 1}
        elif action in {"update", "pause", "resume", "delete"}:
            identity = task_id(command.get("task_id"))
            previous = tasks.get(identity)
            if not previous or previous.get("deleted"):
                raise CommandError("任务不存在或已删除")
            if action == "update":
                updates = command.get("task")
                if not isinstance(updates, dict) or ("id" in updates and updates["id"] != identity):
                    raise CommandError("update 不允许修改任务 id")
                task = dict(previous)
                task.update(self.task_fields(updates, previous, request=request))
            else:
                task = dict(previous)
                task["enabled"] = action == "resume"
                task["deleted"] = action == "delete"
            task.update(version=previous["version"] + 1, updated_at=timestamp, owner=sender)
            # New conditions/resumption apply only after this successful operation.
            if action in {"update", "resume"}:
                task["effective_at"] = timestamp
            tasks[identity] = task
            result = {"ok": True, "action": action, "task_id": identity, "version": task["version"]}
        else:
            raise CommandError("仅支持 create/update/pause/resume/delete/list；一次控制一项任务")
        document["applied_commands"][source_id] = result
        self.store.save_tasks(document)
        return result

    def receipt(self, item, sender, result):
        key = job_key("control-receipt", item["id"])
        body = result.get('reply_body') or json.dumps(result, ensure_ascii=False, indent=2)
        for secret in (self.config.token, self.config.authorization_code, self.config.api_key):
            if secret:
                body = body.replace(secret, '[本机密钥已移除]')
        job = {"kind": "receipt", "control_sender": sender, "source_mail": item["id"],
               "recipients": [sender], "subject": "[AGENT 回执] " + ("操作完成" if result.get("ok") else "操作未完成"),
               "body": body, "message_id": new_message_id(self.config, key)}
        if self.config.control_mode == 'natural_language':
            subject = re.sub(r'[\r\n\x00]', ' ', item['parsed'].get('subject', '邮件问答'))[:180]
            job['subject'] = subject if re.match(r'^re:', subject, re.I) else 'Re: ' + subject
            original_id = item['parsed'].get('message_id', '')
            if re.fullmatch(r'<[^<>\s\x00-\x1f\x7f]{1,240}>', original_id):
                job['in_reply_to'] = original_id
        self.store.enqueue(key, job)

    def process_control(self, item):
        sender = self.control_sender(item)
        if sender is None:
            self.store.finish(item, "control_rejected_sender")
            return
        # No control payload is sent to AI until both sender and secret have been verified.
        try:
            body = item["parsed"].get("text", "")
            if len(body) > 32000:
                raise CommandError("控制邮件正文过长")
            payload = yaml.safe_load(body)
        except (yaml.YAMLError, CommandError, RecursionError):
            self.store.finish(item, "control_invalid_format")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("token"), str) or not hmac.compare_digest(
            payload["token"].encode(), self.config.token.encode()
        ):
            self.store.finish(item, "control_rejected_token")
            return
        request = None
        try:
            already_applied = self.store.tasks()["applied_commands"].get(item["id"])
            if already_applied is not None:
                self.receipt(item, sender, already_applied)
                item["control_result"] = already_applied
                self.store.finish(item, "control_processed")
                return
            if payload.get('action') == 'inquire':
                if 'request' in payload:
                    raise CommandError('采购询价使用 action: inquire，不同时使用 request')
                command = {k: v for k, v in payload.items() if k != 'token'}
                result = Procurement(self.config, self.store).submit(item, sender, command, received_time(item).isoformat())
            elif "request" in payload:
                if "action" in payload:
                    raise CommandError("request 与 action 不可同时使用")
                request = string(payload["request"], "request", 16000)
                # The secret never reaches the model, even when repeated inside the request.
                request = request.replace(self.config.token, "[控制口令已移除]")
                command = self.ai.parse_command(request)
                if command.get('action') == 'inquire':
                    raise CommandError('采购询价请明确填写 action: inquire 并附请购单，不使用AI推断')
            else:
                command = {k: v for k, v in payload.items() if k != "token"}
            if payload.get('action') != 'inquire':
                result = self.apply_command(command, sender, item["id"], request=request)
        except (CommandError, DataError) as exc:
            result = {"ok": False, "error": str(exc)}
        self.receipt(item, sender, result)
        item["control_result"] = result
        self.store.finish(item, "control_processed")

    def eligible_tasks(self, item):
        received = received_time(item)
        if received is None:
            return []
        return [task for task in self.store.tasks()["items"].values()
                if task["enabled"] and not task.get("deleted") and task["owner"] in self.config.allowed_senders
                and received >= time_value(task["effective_at"])]

    def validate_analysis(self, result, tasks):
        summary = string(result.get("summary"), "summary", 16000, empty=True)
        matches = result.get("matches")
        if not isinstance(matches, list) or len(matches) > len(tasks):
            raise CommandError("AI matches 格式有误")
        by_id, seen, valid = {v["id"]: v for v in tasks}, set(), []
        for match in matches:
            if not isinstance(match, dict):
                raise CommandError("AI match 格式有误")
            identity = match.get("task_id")
            if not isinstance(identity, str) or identity not in by_id or identity in seen:
                raise CommandError("AI 返回未知或重复任务")
            subject = string(match.get("subject"), "subject", 240)
            if "\r" in subject or "\n" in subject:
                raise CommandError("AI subject 含非法换行")
            valid.append({"task_id": identity, "reason": string(match.get("reason"), "reason", 4000),
                          "subject": subject, "body": string(match.get("body"), "body", 24000),
                          "task_version": by_id[identity]["version"], "recipients": by_id[identity]["recipients"]})
            seen.add(identity)
        return {"summary": summary, "matches": valid, "model": self.config.model, "analyzed_at": now()}

    def enqueue_matches(self, item, analysis):
        for match in analysis["matches"]:
            key = job_key("match", item["id"], match["task_id"], match["task_version"])
            self.store.enqueue(key, {"kind": "task", "task_id": match["task_id"], "task_version": match["task_version"],
                "source_mail": item["id"], "recipients": match["recipients"], "subject": match["subject"],
                "body": match["body"], "reason": match["reason"], "message_id": new_message_id(self.config, key)})

    def process_one(self, item, *, allow_ai=True):
        parsed = item["parsed"]
        if parsed.get("agent_marker") or received_time(item) is None:
            self.store.finish(item, "archived_only_outgoing_or_unverified_time")
            return
        natural = self.config.control_mode == 'natural_language'
        is_control = parsed.get("subject", "").lstrip().startswith(self.config.subject_prefix) or natural and self.control_sender(item) is not None
        if is_control:
            effective = self.store.state.get('natural_control_enabled_at', self.store.state['activated_at']) if natural else self.store.state['activated_at']
            if received_time(item) < time_value(effective):
                self.store.finish(item, "historical_control_archived_only")
            elif natural:
                sender = self.control_sender(item)
                if sender is None:
                    self.store.finish(item, 'control_rejected_sender')
                else:
                    from .natural_control import NaturalControl
                    try:
                        NaturalControl(self).process(item, sender, received_time(item).isoformat())
                    except (DataError, CommandError) as exc:
                        result={'ok':False,'reply_body':str(exc)}
                        item['control_result']=result
                        self.receipt(item,sender,result)
                        self.store.finish(item,'control_processed')
            else:
                self.process_control(item)
            return
        if self.config.address in parsed.get("from", []):
            self.store.finish(item, "archived_only_self_sent")
            return
        if Procurement(self.config, self.store).handle_reply(item, received_time(item).isoformat()):
            return
        if not allow_ai:
            return
        tasks = self.eligible_tasks(item)
        if ensure_extractions(parsed):
            self.store.save_mail(item)
        analysis = item.get("analysis")
        if analysis is None:
            analysis = self.validate_analysis(self.ai.analyze(item, tasks), tasks)
            # Commit the model result before enqueueing to prevent a restart from re-generating different mail.
            item["analysis"] = analysis
            self.store.save_mail(item)
        self.enqueue_matches(item, analysis)
        self.store.finish(item, "analyzed")

    def process_pending(self):
        # Live control messages first, then newest mail; historical summaries catch up in the background.
        info = self.store.state.get("queue_info", {})
        def order(key):
            meta = info.get(key, {})
            date = time_value(meta.get("received_at"))
            natural = self.config.control_mode == 'natural_language' and meta.get('from') in [[sender] for sender in self.config.allowed_senders]
            is_control = meta.get("subject", "").lstrip().startswith(self.config.subject_prefix) or natural
            effective=self.store.state.get('natural_control_enabled_at',self.store.state['activated_at']) if natural else self.store.state['activated_at']
            live_control = is_control and date and date >= time_value(effective)
            # Preserve command order: create followed by pause must end paused.
            if live_control:
                return (0, date.timestamp())
            procurement_reply = '[询价编号:' in meta.get('subject', '') or 'RFQ-' in meta.get('subject', '').upper()
            return (3 if is_control else 1 if procurement_reply else 2, -(date.timestamp() if date else 0))
        keys = sorted(self.store.state["queue"], key=order)
        handled, ai_handled = 0, 0
        # Load only one complete JSON at a time: attachments may be large.
        for key in keys:
            if self.stop_requested():
                break
            meta_subject = info.get(key, {}).get('subject', '')
            deterministic = meta_subject.lstrip().startswith(self.config.subject_prefix) or 'RFQ-' in meta_subject.upper()
            natural = self.config.control_mode == 'natural_language' and info.get(key,{}).get('from') in [[sender] for sender in self.config.allowed_senders]
            if ai_handled >= self.config.max_ai_per_cycle and (natural or not deterministic):
                continue
            item = self.store.mail(key)
            if item is None:
                continue
            if item["processing"]["status"] != "pending":
                if item["id"] in self.store.state["queue"]:
                    self.store.state["queue"].remove(item["id"])
                    self.store.state.get("queue_info", {}).pop(item["id"], None)
                    self.store.save_state()
                continue
            retry = time_value(item["processing"].get("retry_after"))
            if retry and retry > datetime.now(timezone.utc):
                continue
            allow_ai_attempt = ai_handled < self.config.max_ai_per_cycle
            try:
                self.process_one(item, allow_ai=allow_ai_attempt)
            except Exception as exc:
                item["processing"].update(attempts=item["processing"].get("attempts", 0) + 1,
                    last_error_type=type(exc).__name__, retry_after=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
                self.store.save_mail(item)
                LOG.warning("邮件已归档，AI/任务处理稍后重试；类型=%s", type(exc).__name__)
            if natural or item['processing']['status'] == 'analyzed' or item['processing']['status'] == 'pending' and allow_ai_attempt:
                ai_handled += 1
            handled += 1
            if handled >= 100:
                break
        return handled

    def allowed_job(self, job):
        if job["kind"] == "reply_once":
            task = read_json(self.store.root / "reply-once.json", {})
            if task.get("status") != "queued" or task.get("job_id") != job.get("id") or task.get("source_mail") != job.get("source_mail"):
                return False
            item = self.store.mail(task["source_mail"])
            expected = self.reply_once_job(item, task) if item else None
            return bool(expected and expected[0] == job["id"] and all(job.get(k) == v for k, v in expected[1].items())
                        and job.get("in_reply_to") == expected[1].get("in_reply_to"))
        if job["kind"] == "receipt":
            return job["control_sender"] in self.config.allowed_senders and job["recipients"] == [job["control_sender"]]
        task = self.store.tasks()["items"].get(job["task_id"])
        return bool(task and task["enabled"] and not task.get("deleted")
                    and task["version"] == job["task_version"] and task["owner"] in self.config.allowed_senders
                    and task["recipients"] == job["recipients"])

    def deliver(self):
        self.store.recover_outbox()
        for path, job in self.store.jobs():
            if self.stop_requested():
                break
            if job["status"] != "pending":
                continue
            retry = time_value(job.get("retry_after"))
            if retry and retry > datetime.now(timezone.utc):
                continue
            if not self.allowed_job(job):
                job.update(status="blocked", detail="任务已变更、暂停、删除或控制者已移出白名单", updated_at=now())
                atomic_json(path, job)
                continue
            if self.config.dry_run:
                job.update(status="preview", updated_at=now(), detail="预览模式，不会在改为正式模式后自动补发")
                atomic_json(path, job)
                continue
            message = build_message(self.config, self.store, job)
            client = None
            entered_send = False
            try:
                client = self.smtp_factory(self.config)
                job.update(status="sending", attempts=job["attempts"] + 1, updated_at=now())
                atomic_json(path, job)
                entered_send = True
                refused = client.send_message(message, from_addr=self.config.address, to_addrs=job["recipients"])
                job.update(status="sent_partial" if refused else "sent", sent_at=now(),
                           refused_recipients=list(refused), accepted_recipients=[v for v in job["recipients"] if v not in refused])
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                job.update(status="pending", last_error_type=type(exc).__name__, retry_after=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
            except Exception as exc:
                job.update(status="uncertain" if entered_send else "pending", last_error_type=type(exc).__name__,
                           retry_after=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
            finally:
                # Persist acceptance before QUIT. A QUIT failure cannot cause a duplicate send.
                atomic_json(path, job)
                if client is not None:
                    close_smtp(client)
        if not self.stop_requested():
            Procurement(self.config, self.store, self.smtp_factory, self.stop_requested).deliver()
