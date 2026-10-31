from __future__ import annotations

from datetime import datetime, timezone
import base64
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import tempfile
import uuid

from .tabular import ensure_extractions


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_mail(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    headers = [[str(k), str(v)] for k, v in message.items()]
    text, html, attachments = [], [], []
    def visit(part):
        kind = part.get_content_type()
        filename = part.get_filename()
        is_attachment = part.get_content_disposition() == "attachment" or filename or kind == "message/rfc822"
        if part.is_multipart() and not is_attachment:
            for child in part.iter_parts():
                visit(child)
            return
        data = part.get_payload(decode=True)
        if data is None:
            nested = part.get_payload()
            data = b"\n".join(child.as_bytes(policy=policy.default) for child in nested) if isinstance(nested, list) else b""
        if is_attachment or kind not in {"text/plain", "text/html"}:
            attachments.append({"filename": filename, "content_type": kind, "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "content_base64": base64.b64encode(data).decode("ascii")})
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                value = data.decode(charset, errors="replace")
            except LookupError:
                value = data.decode("utf-8", errors="replace")
            (html if kind == "text/html" else text).append(value)
    visit(message)
    ensure_extractions({'attachments': attachments})
    html_value = "\n".join(html)
    plain = "\n".join(text)
    if not plain and html_value:
        parser = HTMLText()
        parser.feed(html_value)
        plain = "".join(parser.parts)
    senders = [addr.lower() for _, addr in getaddresses(message.get_all("From", []))]
    return {"subject": str(message.get("Subject", "")), "from": senders,
            "to": [addr.lower() for _, addr in getaddresses(message.get_all("To", []))],
            "message_id": str(message.get("Message-ID", "")), "date_header": str(message.get("Date", "")),
            "headers": headers, "text": plain, "html": html_value, "attachments": attachments,
            "from_header_count": len(message.get_all("From", [])),
            "agent_marker": str(message.get("X-Email-Agent-Instance", "")),
            "parse_defects": [type(d).__name__ for d in message.defects]}


class Store:
    def __init__(self, config):
        self.keep_originals = config.keep_originals
        self.root = config.storage / "accounts" / config.account_id
        for folder in ("mail", "outbox"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.state = read_json(self.root / "state.json", {
            "schema": 1, "activated_at": now(), "instance": uuid.uuid4().hex,
            "account_address": config.address, "folders": {}, "queue": [], "archived_count": 0,
        })
        # Recover an archive written just before a process interruption, even if the remote copy was deleted.
        self.state["queue"] = []
        self.state["queue_info"] = {}
        count = 0
        for path in (self.root / "mail").glob("*.json"):
            count += 1
            item = read_json(path)
            if item["processing"]["status"] == "pending":
                self.add_pending(item)
        self.state["archived_count"] = count
        self.save_state()
        if not (self.root / "tasks.json").exists():
            self.save_tasks({"schema": 1, "items": {}, "applied_commands": {}})

    def save_state(self):
        atomic_json(self.root / "state.json", self.state)

    def tasks(self):
        return read_json(self.root / "tasks.json")

    def save_tasks(self, tasks):
        atomic_json(self.root / "tasks.json", tasks)

    def mail(self, key):
        return read_json(self.root / "mail" / f"{key}.json")

    def save_mail(self, item):
        atomic_json(self.root / "mail" / f"{item['id']}.json", item)

    def archive(self, raw, source):
        key = hashlib.sha256(raw).hexdigest()
        item = self.mail(key)
        if item is None:
            try:
                parsed = parse_mail(raw)
            except Exception as exc:
                parsed = {"subject": "", "from": [], "text": "", "headers": [], "attachments": [],
                          "parse_error": type(exc).__name__, "from_header_count": 0, "agent_marker": ""}
            item = {"schema": 1, "id": key, "archived_at": now(), "sources": [source],
                    "received_at": source["received_at"], "raw_base64": base64.b64encode(raw).decode("ascii"),
                    "parsed": parsed, "processing": {"status": "pending", "attempts": 0}}
            if not self.keep_originals:
                item.pop('raw_base64', None)
                item['original_stored'] = False
                for attachment in parsed.get('attachments', []):
                    attachment.pop('content_base64', None)
            self.save_mail(item)
            # Cursor, queue and count are committed together by the caller.
            self.state["archived_count"] += 1
        else:
            identity = (source["folder"], source["uidvalidity"], source["uid"])
            known = {(v["folder"], v["uidvalidity"], v["uid"]) for v in item["sources"]}
            if identity not in known:
                item["sources"].append(source)
                self.save_mail(item)
        if item["processing"]["status"] == "pending":
            self.add_pending(item)
        return item

    def add_pending(self, item):
        if item["id"] not in self.state["queue"]:
            self.state["queue"].append(item["id"])
        self.state.setdefault("queue_info", {})[item["id"]] = {
            "subject": item["parsed"].get("subject", ""), "received_at": item.get("received_at"),
            "from": item['parsed'].get('from', [])}

    def finish(self, item, status):
        item["processing"].update(status=status, finished_at=now())
        self.save_mail(item)
        if item["id"] in self.state["queue"]:
            self.state["queue"].remove(item["id"])
        self.state.get("queue_info", {}).pop(item["id"], None)
        self.save_state()

    def enqueue(self, key, job):
        path = self.root / "outbox" / f"{key}.json"
        existing = read_json(path)
        if existing is not None:
            return existing
        job.update(id=key, status="pending", created_at=now(), attempts=0)
        atomic_json(path, job)
        return job

    def jobs(self):
        for path in sorted((self.root / "outbox").glob("*.json")):
            yield path, read_json(path)

    def recover_outbox(self):
        for path, job in self.jobs():
            if job["status"] == "sending":
                job.update(status="uncertain", detail="进程中断，SMTP 是否已接受无法确定；不会自动重发", updated_at=now())
                atomic_json(path, job)
