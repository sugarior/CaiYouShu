from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import imaplib
import logging
import re
import smtplib
import ssl

from imap_tools import MailBox

LOG = logging.getLogger("email-agent")


@contextmanager
def imap_connection(config):
    box = MailBox(config.imap_host, port=config.imap_port, timeout=40,
                  ssl_context=ssl.create_default_context())
    try:
        box.login(config.address, config.authorization_code, initial_folder=None)
        _, capabilities = box.client.capability()
        if b"ID" in b" ".join(capabilities or []).upper().split():
            typ, _ = box.client.xatom("ID", '("name" "EmailAgentLite" "version" "1.0" "vendor" "Local")')
            if typ != "OK":
                raise RuntimeError("IMAP_ID_rejected")
        yield box
    finally:
        try:
            box.logout()
        except Exception:
            pass


def internal_date(metadata):
    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if not match:
        return None
    try:
        return datetime.strptime(match[1].decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z").isoformat()
    except (ValueError, UnicodeError):
        return None


def archive_folders(config, store, connect=imap_connection, stop_requested=lambda: False):
    count, errors = 0, []
    with connect(config) as box:
        listed = box.folder.list()
        info = {v.name: v for v in listed}
        names = [v.name for v in listed if not any(flag.lower() == "\\noselect" for flag in v.flags)] if config.folders == ["*"] else config.folders
        # Folder order is stable across polls, with an independent batch budget for each.
        for folder in names:
            if stop_requested():
                break
            try:
                box.folder.set(folder, readonly=True)
                validity = str(box.folder.status(folder, ("UIDVALIDITY",))["UIDVALIDITY"])
                cursor = store.state["folders"].get(folder, {})
                last = cursor.get("last_uid", 0) if cursor.get("uidvalidity") == validity else 0
                typ, result = box.client.uid("SEARCH", None, f"UID {last + 1}:*")
                if typ != "OK":
                    raise RuntimeError("IMAP_SEARCH_failed")
                uids = sorted({int(v) for v in (result[0] or b"").split() if int(v) > last})
                folder_flags = list(info[folder].flags) if folder in info else []
                for uid in uids[:config.max_mail_per_folder]:
                    if stop_requested():
                        break
                    typ, parts = box.client.uid("FETCH", str(uid), "(UID INTERNALDATE FLAGS BODY.PEEK[])")
                    if typ != "OK":
                        raise RuntimeError("IMAP_FETCH_failed")
                    literal = next((v for v in parts if isinstance(v, tuple) and isinstance(v[1], bytes)), None)
                    if literal is None:
                        # A message can be expunged between SEARCH and FETCH. Do not advance past an unexplained failure.
                        raise RuntimeError("IMAP_message_missing")
                    metadata, raw = literal
                    source = {"folder": folder, "folder_flags": folder_flags, "uidvalidity": validity,
                              "uid": uid, "flags": [v.decode("ascii", errors="replace") for v in imaplib.ParseFlags(metadata)],
                              "received_at": internal_date(metadata)}
                    store.archive(raw, source)
                    store.state["folders"][folder] = {"uidvalidity": validity, "last_uid": uid}
                    store.save_state()
                    count += 1
            except Exception as exc:
                errors.append({"folder": folder, "error_type": type(exc).__name__})
                LOG.warning("文件夹同步失败，下一轮重试；类型=%s", type(exc).__name__)
    return {"fetched": count, "folders": names, "errors": errors}


def smtp_connection(config):
    client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=40,
                             context=ssl.create_default_context())
    try:
        client.ehlo()
        client.login(config.address, config.authorization_code)
    except Exception:
        client.close()
        raise
    return client


def close_smtp(client):
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def build_message(config, store, job):
    message = EmailMessage(policy=SMTP)
    message["From"] = config.address
    message["To"] = ", ".join(job["recipients"])
    message["Subject"] = job["subject"]
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = job["message_id"]
    message["X-Email-Agent-Instance"] = store.state["instance"]
    message["X-Email-Agent-Job"] = job["id"]
    if job["kind"] in {"reply_once", "receipt"}:
        message["Auto-Submitted"] = "auto-replied"
        if job.get("in_reply_to"):
            message["In-Reply-To"] = job["in_reply_to"]
            message["References"] = job["in_reply_to"]
    message.set_content(job["body"])
    return message


def new_message_id(config, job_id):
    # Stable across attempts, but SMTP itself does not guarantee exactly-once delivery.
    domain = config.address.rsplit("@", 1)[-1]
    return f"<email-agent.{job_id}@{domain}>"
