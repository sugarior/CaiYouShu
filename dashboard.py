"""Read-only local dashboard over the agent's existing JSON archive."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import threading
from urllib.parse import parse_qs, quote, urlsplit

from filelock import FileLock, Timeout

from mailagent.config import ROOT
from mailagent.reply_once import reply_once_status
from mailagent.store import atomic_json, now, read_json

ACCOUNT = re.compile(r"[0-9a-f]{24}\Z")
MAIL = re.compile(r"[0-9a-f]{64}\Z")
WEB = ROOT / "web"


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0


class ArchiveView:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.cache = {}
        self.lock = threading.Lock()

    def account_root(self, identity):
        if not ACCOUNT.fullmatch(identity):
            raise LookupError("account_not_found")
        root = self.data / "accounts" / identity
        if not root.is_dir() or root.is_symlink():
            raise LookupError("account_not_found")
        return root

    def accounts(self):
        result = []
        for root in sorted((self.data / "accounts").glob("*")):
            if not ACCOUNT.fullmatch(root.name) or not root.is_dir() or root.is_symlink():
                continue
            state = read_json(root / "state.json", {})
            result.append({"id": root.name, "address": state.get("account_address", root.name)})
        return result

    def overview(self):
        status = read_json(self.data / "service-status.json", {})
        accounts = self.accounts()
        active = status.get("account_id")
        if active not in {a["id"] for a in accounts}:
            active = accounts[0]["id"] if accounts else ""
        # Only explicitly public status fields are returned; config.yml is never opened.
        service = {k: status.get(k) for k in ("state", "heartbeat_at", "last_cycle_at", "missing", "last_error", "pending_analysis")}
        age = datetime.now(timezone.utc).timestamp() - timestamp(status.get("heartbeat_at"))
        service["online"] = bool(status and 0 <= age < 30 and status.get("state") != "stopped")
        sync = status.get("sync") or {}
        service["sync_errors"] = sync.get("errors", [])
        return {"accounts": accounts, "active_account": active, "service": service}

    def mail_rows(self, account):
        root = self.account_root(account)
        own = read_json(root / "state.json", {}).get("account_address", "").lower()
        with self.lock:
            cache = self.cache.setdefault(account, {})
            existing = set()
            for path in (root / "mail").glob("*.json"):
                if not MAIL.fullmatch(path.stem) or path.is_symlink():
                    continue
                existing.add(path.stem)
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if path.stem in cache and cache[path.stem][0] == signature:
                    continue
                item = read_json(path)
                if item is None:
                    continue
                parsed = item.get("parsed", {})
                processing = item.get("processing", {})
                analysis = item.get("analysis") or {}
                state = processing.get("status", "pending")
                senders = parsed.get("from", [])
                if parsed.get("agent_marker"):
                    kind = "agent"
                elif "control" in state or "control_result" in item:
                    kind = "control"
                elif own in senders:
                    kind = "outgoing"
                else:
                    kind = "incoming"
                text = parsed.get("text", "")
                summary = analysis.get("summary", "")
                row = {"id": item["id"], "subject": parsed.get("subject") or "（无主题）", "from": senders,
                       "to": parsed.get("to", []), "received_at": item.get("received_at") or item.get("archived_at"),
                       "folders": list(dict.fromkeys(s["folder"] for s in item.get("sources", []))),
                       "snippet": " ".join(text.split())[:150], "summary": summary[:250], "status": state,
                       "kind": kind, "attachments": len(parsed.get("attachments", [])),
                       "matches": len(analysis.get("matches", []))}
                searchable = "\n".join([row["subject"], " ".join(senders), " ".join(row["to"]), text, summary]).casefold()
                cache[path.stem] = (signature, row, searchable)
            for stale in set(cache) - existing:
                del cache[stale]
            return list(cache.values())

    def listing(self, account, query="", folder="", kind="all", page=1, page_size=30):
        entries = self.mail_rows(account)
        rows = [entry[1] for entry in entries]
        folders = sorted({v for row in rows for v in row["folders"]}, key=lambda v: (v != "INBOX", v))
        counts = {"all": len(rows), "incoming": sum(v["kind"] == "incoming" for v in rows),
                  "analyzed": sum(v["status"] == "analyzed" for v in rows),
                  "matched": sum(v["matches"] > 0 for v in rows)}
        selected = [row for _, row, searchable in entries
                    if (not query or query.casefold() in searchable)
                    and (not folder or folder in row["folders"]) and (kind == "all" or row["kind"] == kind)]
        selected.sort(key=lambda v: timestamp(v["received_at"]), reverse=True)
        total = len(selected)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        return {"items": selected[(page - 1) * page_size:page * page_size], "page": page, "pages": pages,
                "total": total, "counts": counts, "folders": folders}

    def mail(self, account, identity):
        if not MAIL.fullmatch(identity):
            raise LookupError("mail_not_found")
        path = self.account_root(account) / "mail" / f"{identity}.json"
        if path.is_symlink():
            raise LookupError("mail_not_found")
        item = read_json(path)
        if item is None:
            raise LookupError("mail_not_found")
        return item

    def detail(self, account, identity):
        item = self.mail(account, identity)
        parsed = item.get("parsed", {})
        fields = {k: parsed.get(k) for k in ("subject", "from", "to", "date_header", "message_id", "text", "parse_error")}
        fields["attachments"] = [dict({k: v.get(k) for k in ("filename", "content_type", "size", "sha256", "extraction")},
                                      original_available='content_base64' in v)
                                 for v in parsed.get("attachments", [])]
        return {"id": item["id"], "received_at": item.get("received_at"), "archived_at": item.get("archived_at"),
                "original_available": 'raw_base64' in item,
                "sources": item.get("sources", []), "parsed": fields, "analysis": item.get("analysis"),
                "processing": item.get("processing"), "control_result": item.get("control_result")}

    def tasks(self, account):
        document = read_json(self.account_root(account) / "tasks.json", {"items": {}})
        items = [v for v in document["items"].values() if not v.get("deleted")]
        items.sort(key=lambda v: (not v.get("enabled"), v.get("name", "")))
        return {"items": items, "total": len(items), "enabled": sum(bool(v.get("enabled")) for v in items),
                "reply_once": reply_once_status(self.account_root(account))}

    def outbox(self, account):
        items = []
        for path in (self.account_root(account) / "outbox").glob("*.json"):
            if MAIL.fullmatch(path.stem) and not path.is_symlink():
                job = read_json(path)
                if job is not None:
                    items.append(job)
        items.sort(key=lambda v: timestamp(v.get("created_at")), reverse=True)
        # No attachment data or credentials are present in the outbox schema.
        return {"items": items, "total": len(items)}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, view):
        self.view = view
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "Mailroom/1.0"

    def log_message(self, *_):
        pass

    def send_data(self, data, content_type="application/json; charset=utf-8", status=200, filename=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.end_headers()
        self.wfile.write(data)

    def trusted_request(self):
        port = self.server.server_port
        if self.headers.get("Host", "").lower() not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def do_GET(self):
        if not self.trusted_request():
            self.send_data({"error": "local_requests_only"}, status=403)
            return
        try:
            self.route()
        except LookupError:
            self.send_data({"error": "not_found"}, status=404)
        except (ValueError, TypeError):
            self.send_data({"error": "invalid_request"}, status=400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_data({"error": "archive_temporarily_unavailable"}, status=503)

    def route(self):
        url = urlsplit(self.path)
        if url.path in {"/", "/index.html", "/app.js", "/styles.css", "/favicon.svg"}:
            name = "index.html" if url.path == "/" else url.path[1:]
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[Path(name).suffix]
            self.send_data((WEB / name).read_bytes(), mime + "; charset=utf-8")
            return
        view = self.server.view
        if url.path == "/api/overview":
            self.send_data(view.overview())
            return
        match = re.fullmatch(r"/api/accounts/([0-9a-f]{24})/(mail|tasks|outbox)(?:/([0-9a-f]{64}))?(?:/(raw|attachments/\d+))?", url.path)
        if not match:
            raise LookupError()
        account, resource, identity, download = match.groups()
        if resource == "mail" and identity:
            if download:
                item = view.mail(account, identity)
                if download == "raw":
                    if 'raw_base64' not in item:
                        raise LookupError()
                    self.send_data(base64.b64decode(item["raw_base64"]), "message/rfc822", filename=f"mail-{identity[:12]}.eml")
                else:
                    index = int(download.rsplit("/", 1)[-1])
                    attachments = item.get("parsed", {}).get("attachments", [])
                    if index >= len(attachments):
                        raise LookupError()
                    attachment = attachments[index]
                    if 'content_base64' not in attachment:
                        raise LookupError()
                    # Never use a sender-supplied filename as a local path or MIME header.
                    filename = str(attachment.get("filename") or f"attachment-{index + 1}").replace("\\", "/").rsplit("/", 1)[-1]
                    filename = "".join(c for c in filename if ord(c) >= 32)[:180] or "attachment.bin"
                    self.send_data(base64.b64decode(attachment["content_base64"]), "application/octet-stream", filename=filename)
            else:
                self.send_data(view.detail(account, identity))
        elif resource == "mail" and not download:
            params = parse_qs(url.query)
            query, folder, kind = (params.get(k, [d])[0] for k, d in (("q", ""), ("folder", ""), ("kind", "all")))
            if len(query) > 256 or len(folder) > 256 or kind not in {"all", "incoming", "control", "agent", "outgoing"}:
                raise ValueError()
            self.send_data(view.listing(account, query, folder, kind, int(params.get("page", ["1"])[0])))
        elif resource == "tasks" and not identity and not download:
            self.send_data(view.tasks(account))
        elif resource == "outbox" and not identity and not download:
            self.send_data(view.outbox(account))
        else:
            raise LookupError()


def main():
    parser = argparse.ArgumentParser(description="本地邮件工作台，只读展示 JSON 归档")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    data = args.data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(data / "dashboard.lock"), timeout=0):
            server = DashboardServer(("127.0.0.1", args.port), ArchiveView(data))
            stopped = threading.Event()
            status = {"pid": os.getpid(), "started_at": now(), "state": "listening", "port": server.server_port,
                      "url": f"http://127.0.0.1:{server.server_port}"}
            stop_path = data / "dashboard-stop.request"
            stop_path.unlink(missing_ok=True)
            def monitor():
                while not stopped.is_set():
                    atomic_json(data / "dashboard-status.json", dict(status, heartbeat_at=now()))
                    if stop_path.exists():
                        server.shutdown()
                        return
                    stopped.wait(2)
            worker = threading.Thread(target=monitor, daemon=True)
            worker.start()
            def stop_signal(*_):
                stop_path.touch()
            signal.signal(signal.SIGINT, stop_signal)
            signal.signal(signal.SIGTERM, stop_signal)
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                stopped.set()
                worker.join(timeout=3)
                server.server_close()
                atomic_json(data / "dashboard-status.json", dict(status, state="stopped", heartbeat_at=now()))
                stop_path.unlink(missing_ok=True)
    except Timeout:
        print("Dashboard is already running.")
        return 2
    except OSError:
        print("Dashboard could not start. Check port availability and data permissions.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
