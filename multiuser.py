"""Loopback-only registration and workspace for three isolated demo tenants."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit

from dashboard import ArchiveView
from tenant_platform import MAX_TENANTS, PlatformError, TenantPlatform


WEB = Path(__file__).resolve().parent / "web"
SESSION_SECONDS = 12 * 60 * 60


class Sessions:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def create(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.values[token] = (user_id, time.time() + SESSION_SECONDS)
        return token

    def user(self, token):
        if not token:
            return None
        with self.lock:
            value = self.values.get(token)
            if not value:
                return None
            if value[1] < time.time():
                del self.values[token]
                return None
            return value[0]

    def remove(self, token):
        with self.lock:
            self.values.pop(token, None)


class PlatformServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, platform, public_origin=None):
        self.platform = platform
        self.public_origin = (public_origin if public_origin is not None else os.environ.get("CAIYOUSHU_PUBLIC_ORIGIN", "")).rstrip("/")
        self.sessions = Sessions()
        self.failures = {}
        self.failure_lock = threading.Lock()
        super().__init__(address, PlatformHandler)


class PlatformHandler(BaseHTTPRequestHandler):
    server_version = "CaiYouShuDemo/1.0"

    def log_message(self, *_):
        pass

    def trusted_request(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
        if self.server.public_origin:
            allowed_origins.add(self.server.public_origin.lower())
        return not origin or origin.lower() in allowed_origins

    def session_cookie(self, token, max_age=SESSION_SECONDS):
        secure = "; Secure" if self.server.public_origin.lower().startswith("https://") else ""
        return f"caiyoushu_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def send_data(self, data, *, status=200, content_type="application/json; charset=utf-8", cookie=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == "caiyoushu_session":
                return value
        return ""

    def current_user(self):
        return self.server.sessions.user(self.token())

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise PlatformError("请求格式无效") from None
        if not 1 <= length <= 32768:
            raise PlatformError("请求内容为空或超过大小限制")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PlatformError("请求内容必须是 JSON") from None
        if not isinstance(value, dict):
            raise PlatformError("请求内容格式无效")
        return value

    def require_user(self):
        user_id = self.current_user()
        if not user_id:
            self.send_data({"error": "请先登录"}, status=401)
        return user_id

    def do_GET(self):
        if not self.trusted_request():
            self.send_data({"error": "仅允许本机访问"}, status=403)
            return
        try:
            self.route_get()
        except PlatformError as exc:
            self.send_data({"error": str(exc)}, status=404)
        except LookupError:
            self.send_data({"error": "未找到"}, status=404)
        except (ValueError, TypeError):
            self.send_data({"error": "请求参数无效"}, status=400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_data({"error": "服务暂不可用"}, status=503)

    def do_POST(self):
        if not self.trusted_request():
            self.send_data({"error": "仅允许本机访问"}, status=403)
            return
        try:
            self.route_post()
        except PlatformError as exc:
            self.send_data({"error": str(exc)}, status=400)
        except (ValueError, TypeError):
            self.send_data({"error": "请求参数无效"}, status=400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_data({"error": "服务暂不可用"}, status=503)

    def route_get(self):
        url = urlsplit(self.path)
        if url.path in {"/", "/portal.html", "/portal.js", "/portal.css", "/favicon.svg"}:
            name = "portal.html" if url.path == "/" else url.path[1:]
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[Path(name).suffix]
            self.send_data((WEB / name).read_bytes(), content_type=mime + "; charset=utf-8")
            return
        user_id = self.current_user()
        if url.path == "/api/bootstrap":
            user = None
            status = None
            if user_id:
                from contextlib import closing
                with closing(self.server.platform._connect()) as con:
                    row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if row:
                    user = self.server.platform.public_user(row)
                    status = self.server.platform.status(user_id)
            self.send_data({"configured": self.server.platform.configured,
                            "registered": self.server.platform.account_count(), "limit": MAX_TENANTS,
                            "user": user, "agent": status,
                            "claimable": self.server.platform.claimable_original() if not user else None})
            return
        if not user_id:
            self.send_data({"error": "请先登录"}, status=401)
            return
        platform = self.server.platform
        view = ArchiveView(platform.data_root(user_id))
        account_id = platform.account_id(user_id)
        if url.path == "/api/inbox":
            params = parse_qs(url.query)
            query = params.get("q", [""])[0]
            folder = params.get("folder", [""])[0]
            kind = params.get("kind", ["all"])[0]
            page = int(params.get("page", ["1"])[0])
            self.send_data(view.listing(account_id, query, folder, kind, page))
            return
        match = re.fullmatch(r"/api/mail/([0-9a-f]{64})", url.path)
        if match:
            self.send_data(view.detail(account_id, match.group(1)))
            return
        if url.path == "/api/tasks":
            self.send_data(view.tasks(account_id))
            return
        if url.path == "/api/outbox":
            self.send_data(view.outbox(account_id))
            return
        if url.path == "/api/status":
            self.send_data(platform.status(user_id))
            return
        raise LookupError()

    def route_post(self):
        path = urlsplit(self.path).path
        data = self.body()
        platform = self.server.platform
        if path == "/api/setup":
            if platform.configured or platform.account_count():
                raise PlatformError("本机 AI 服务已完成设置")
            platform.setup_model(data.get("api_key"))
            self.send_data({"ok": True})
            return
        if path == "/api/login":
            key = self.client_address[0]
            with self.server.failure_lock:
                failures, until = self.server.failures.get(key, (0, 0))
                if until > time.time():
                    raise PlatformError("登录失败次数过多，请稍后重试")
            try:
                user = platform.authenticate(data.get("login_email"), data.get("password"))
            except PlatformError:
                with self.server.failure_lock:
                    failures += 1
                    self.server.failures[key] = (0, time.time() + 60) if failures >= 8 else (failures, 0)
                raise
            with self.server.failure_lock:
                self.server.failures.pop(key, None)
            token = self.server.sessions.create(user["id"])
            self.send_data({"ok": True, "user": user}, cookie=self.session_cookie(token))
            return
        if path == "/api/register":
            if not platform.configured:
                raise PlatformError("请先完成本机 AI 服务设置")
            user = platform.register(login_email=data.get("login_email"), password=data.get("password"),
                                     agent_email=data.get("agent_email"), authorization_code=data.get("authorization_code"),
                                     controllers=data.get("controllers"), supplier_email=data.get("supplier_email", "xyinchina@163.com"))
            token = self.server.sessions.create(user["id"])
            self.send_data({"ok": True, "user": user}, status=201,
                           cookie=self.session_cookie(token))
            return
        if path == "/api/claim-original":
            user = platform.claim_original(data.get("password"))
            token = self.server.sessions.create(user["id"])
            self.send_data({"ok": True, "user": user}, status=200, cookie=self.session_cookie(token))
            return
        if path == "/api/logout":
            self.server.sessions.remove(self.token())
            self.send_data({"ok": True}, cookie=self.session_cookie("", max_age=0))
            return
        user_id = self.require_user()
        if not user_id:
            return
        if path == "/api/mail-settings":
            user = platform.change_mail(user_id, agent_email=data.get("agent_email"),
                                        authorization_code=data.get("authorization_code", ""),
                                        controllers=data.get("controllers"),
                                        supplier_email=data.get("supplier_email"))
            self.send_data({"ok": True, "user": user, "agent": platform.status(user_id)})
            return
        if path == "/api/agent/start":
            platform.start_agent(user_id)
            self.send_data({"ok": True, "agent": platform.status(user_id)})
            return
        if path == "/api/agent/stop":
            platform.stop_agent(user_id)
            self.send_data({"ok": True, "agent": platform.status(user_id)})
            return
        raise LookupError()


def main():
    parser = argparse.ArgumentParser(description="本地三账号邮件 Agent 演示平台")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "demo-data")
    args = parser.parse_args()
    platform = TenantPlatform(args.data)
    try:
        server = PlatformServer(("127.0.0.1", args.port), platform)
    except OSError:
        platform.close()
        raise SystemExit("端口无法启动，请尝试 --port 8789")
    stopped = threading.Event()

    def stop(*_):
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"本地演示平台：http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        platform.close()


if __name__ == "__main__":
    main()
