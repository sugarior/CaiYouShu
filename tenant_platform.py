"""Local three-tenant demo account store and isolated Agent lifecycle."""
from __future__ import annotations

from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import signal
import subprocess
import sys
import threading
import time
import uuid

import yaml

from demo_seed import initialize_demo_database, migrate_demo_database
from mailagent.config import Config, ConfigError, ROOT, address
from mailagent.mailio import close_smtp, imap_connection, smtp_connection
from mailagent.store import Store, now, read_json


MAX_TENANTS = 3
PASSWORD_ITERATIONS = 450_000


class PlatformError(ValueError):
    pass


class TenantPlatform:
    def __init__(self, data=None, *, legacy_config=None, probe_mail=None, probe_model=None,
                 start_supervisor=True, launch_agents=True):
        self.data = Path(data or ROOT / "data").resolve()
        self.tenants = self.data / "tenants"
        self.database = self.data / "platform.sqlite3"
        self.settings_path = self.data / "platform-settings.json"
        self.legacy_config = Path(legacy_config or ROOT / "config.yml")
        self.tenants.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.processes = {}
        self.paused = set()
        self.launch_agents = launch_agents
        self.probe_mail = probe_mail or self._probe_mail
        self.probe_model = probe_model or self._probe_model
        self.settings = self._load_settings()
        self._initialize()
        self._bootstrap_original_account()
        self._upgrade_demo_databases()
        self.supervisor = None
        if start_supervisor:
            self.supervisor = threading.Thread(target=self._supervise, daemon=True, name="tenant-agent-supervisor")
            self.supervisor.start()

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                login_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                agent_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                controller_emails TEXT NOT NULL,
                supplier_email TEXT NOT NULL,
                created_at TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'registered',
                claimed INTEGER NOT NULL DEFAULT 1
            )""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
            if "origin" not in columns:
                con.execute("ALTER TABLE users ADD COLUMN origin TEXT NOT NULL DEFAULT 'registered'")
            if "claimed" not in columns:
                con.execute("ALTER TABLE users ADD COLUMN claimed INTEGER NOT NULL DEFAULT 1")
            con.commit()
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    def _load_settings(self):
        try:
            result = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            result = {"api_key": os.environ.get("DEEPSEEK_API_KEY", "")}
        except (OSError, json.JSONDecodeError):
            raise PlatformError("平台设置文件无法读取") from None
        if not isinstance(result, dict):
            result = {"api_key": ""}
        if not result.get("api_key") and self.legacy_config.is_file():
            try:
                legacy = yaml.safe_load(self.legacy_config.read_text(encoding="utf-8-sig")) or {}
                deepseek = legacy.get("deepseek", {})
                if isinstance(deepseek, dict) and deepseek.get("api_key"):
                    result["api_key"] = deepseek["api_key"]
                    result["deepseek"] = {k: v for k, v in deepseek.items() if k in {"api_key", "base_url", "model"}}
            except (OSError, yaml.YAMLError, AttributeError):
                pass
        if result.get("api_key") or not self.settings_path.exists():
            self._save_settings(result)
        return result

    def _save_settings(self, settings):
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.settings_path)

    @property
    def configured(self):
        return bool(self.settings.get("api_key"))

    def setup_model(self, api_key):
        if not isinstance(api_key, str) or not 12 <= len(api_key.strip()) <= 512:
            raise PlatformError("DeepSeek API Key 格式无效")
        api_key = api_key.strip()
        self.probe_model(api_key)
        self.settings["api_key"] = api_key
        self.settings.setdefault("deepseek", {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"})
        self.settings["deepseek"]["api_key"] = api_key
        self._save_settings(self.settings)

    def _probe_model(self, api_key):
        from mailagent.ai import DeepSeek
        from dataclasses import replace
        cfg = Config(path=ROOT / "config.example.yml", api_key=api_key)
        result = DeepSeek(cfg).request('输出 JSON {"ok":true}', {"test": True})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise PlatformError("AI 服务连通检查未通过")

    def _probe_mail(self, config):
        try:
            with imap_connection(config) as box:
                box.folder.list()
            client = smtp_connection(config)
            close_smtp(client)
        except Exception as exc:
            raise PlatformError("邮箱连接测试失败：" + type(exc).__name__) from None

    @staticmethod
    def _email(value, field):
        try:
            return address(value.strip() if isinstance(value, str) else value)
        except ConfigError:
            raise PlatformError(field + " 格式无效") from None

    @classmethod
    def _controllers(cls, values, agent_email):
        if not isinstance(values, list) or not 1 <= len(values) <= 3:
            raise PlatformError("控制邮箱须配置 1 到 3 个")
        result = [cls._email(value, "控制邮箱") for value in values]
        if len(set(result)) != len(result):
            raise PlatformError("控制邮箱不能重复")
        if agent_email in result:
            raise PlatformError("控制邮箱不能与 Agent 收件邮箱相同")
        return result

    @staticmethod
    def _password(password):
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise PlatformError("登录密码长度须为 10 到 256 个字符")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
        return salt, digest

    def _config_document(self, agent_email, authorization_code, controllers, supplier_email, api_key):
        deepseek = dict(self.settings.get("deepseek") or {})
        deepseek.update({"api_key": api_key})
        deepseek.setdefault("base_url", "https://api.deepseek.com")
        deepseek.setdefault("model", "deepseek-flash")
        return {
            "mail": {"address": agent_email, "authorization_code": authorization_code,
                     "imap_host": "imap.163.com", "imap_port": 993,
                     "smtp_host": "smtp.163.com", "smtp_port": 465, "folders": ["*"]},
            "deepseek": deepseek,
            "control": {"allowed_senders": controllers, "mode": "natural_language",
                        "actions": ["inquire", "query", "answer", "update_data"],
                        "update_tables": ["suppliers", "material_groups", "supplier_groups", "statuses",
                                          "requisitions", "quotations", "orders"],
                        "update_max_rows": 1000, "query_limit": 50, "answer_chars": 12000,
                        "subject_prefix": "[AGENT]"},
            "procurement": {"enabled": True, "max_sends_per_cycle": 10},
            "service": {"poll_seconds": 30, "max_mail_per_folder": 100,
                        "max_ai_per_cycle": 5, "max_body_chars": 16000},
            "delivery": {"dry_run": False},
            "storage": {"directory": "data", "keep_originals": False},
            "demo": {"supplier_inbox": supplier_email},
        }

    @staticmethod
    def _write_config(path, document):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    def _tenant(self, user_id):
        if not isinstance(user_id, str) or len(user_id) != 32 or any(c not in "0123456789abcdef" for c in user_id):
            raise PlatformError("账号不存在")
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise PlatformError("账号不存在")
        return row

    @staticmethod
    def public_user(row):
        return {"id": row["id"], "login_email": row["login_email"], "agent_email": row["agent_email"],
                "controllers": json.loads(row["controller_emails"]), "supplier_email": row["supplier_email"],
                "created_at": row["created_at"], "auth_configured": True,
                "claimed": bool(row["claimed"]), "origin": row["origin"]}

    def claimable_original(self):
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM users WHERE origin='legacy' AND claimed=0 ORDER BY created_at LIMIT 1").fetchone()
        return self.public_user(row) if row else None

    def account_count(self):
        with closing(self._connect()) as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def _upgrade_demo_databases(self):
        # Runs before supervision starts, so no managed Agent can send mail
        # while its old synthetic seed is being migrated.
        with closing(self._connect()) as con:
            users = con.execute("SELECT id,agent_email,supplier_email FROM users").fetchall()
        for user in users:
            migrate_demo_database(self.tenants / user["id"] / "data" / "procurement.sqlite3",
                                  user["supplier_email"], user["agent_email"])

    def _bootstrap_original_account(self):
        if self.account_count() or not self.legacy_config.is_file() or not self.settings.get("api_key"):
            return
        tenant_root = None
        try:
            legacy = yaml.safe_load(self.legacy_config.read_text(encoding="utf-8-sig")) or {}
            config = Config.load(self.legacy_config)
            if config.missing() or len(config.allowed_senders) > 3:
                return
            user_id = uuid.uuid4().hex
            tenant_root = self.tenants / user_id
            tenant_root.mkdir(parents=True, exist_ok=False)
            document = copy.deepcopy(legacy)
            document.setdefault("storage", {})["directory"] = "data"
            supplier_email = "xyinchina@163.com"
            document.setdefault("demo", {})["supplier_inbox"] = supplier_email
            config_path = tenant_root / "config.yml"
            self._write_config(config_path, document)
            tenant_config = Config.load(config_path)
            Store(tenant_config)
            initialize_demo_database(tenant_root / "data" / "procurement.sqlite3", supplier_email, config.address)
            salt, digest = self._password(secrets.token_urlsafe(32))
            with closing(self._connect()) as con:
                con.execute("INSERT INTO users(id,login_email,password_salt,password_hash,agent_email,controller_emails,supplier_email,created_at,origin,claimed) VALUES(?,?,?,?,?,?,?,?,?,0)",
                            (user_id, config.address, salt, digest, config.address,
                             json.dumps(config.allowed_senders, ensure_ascii=False), supplier_email, now(), "legacy"))
                con.commit()
        except Exception:
            if tenant_root is not None and tenant_root.exists():
                self._remove_tenant(tenant_root)

    def claim_original(self, password):
        user = self.claimable_original()
        if not user:
            raise PlatformError("没有待认领的原邮箱账号")
        salt, digest = self._password(password)
        config_path = self.tenants / user["id"] / "config.yml"
        self.probe_mail(Config.load(config_path))
        with closing(self._connect()) as con:
            changed = con.execute("UPDATE users SET password_salt=?,password_hash=?,claimed=1 WHERE id=? AND origin='legacy' AND claimed=0",
                                  (salt, digest, user["id"])).rowcount
            con.commit()
        if not changed:
            raise PlatformError("原邮箱账号已被认领")
        if self.launch_agents:
            self.start_agent(user["id"])
        return self.public_user(self._tenant(user["id"]))

    def register(self, *, login_email, password, agent_email, authorization_code, controllers, supplier_email="xyinchina@163.com"):
        if not self.configured:
            raise PlatformError("请先完成本机 AI 服务设置")
        login_email = self._email(login_email, "登录邮箱")
        agent_email = self._email(agent_email, "Agent 邮箱")
        supplier_email = self._email(supplier_email, "演示供应商收件邮箱")
        controllers = self._controllers(controllers, agent_email)
        if login_email != login_email.strip().lower():
            raise PlatformError("登录邮箱格式无效")
        if not isinstance(authorization_code, str) or not 1 <= len(authorization_code.strip()) <= 256:
            raise PlatformError("请输入邮箱客户端授权码")
        salt, digest = self._password(password)
        if self.account_count() >= MAX_TENANTS:
            raise PlatformError("演示环境最多注册 3 个账号")
        user_id = uuid.uuid4().hex
        tenant_root = self.tenants / user_id
        tenant_root.mkdir(parents=True, exist_ok=False)
        config_path = tenant_root / "config.yml"
        document = self._config_document(agent_email, authorization_code.strip(), controllers,
                                         supplier_email, self.settings["api_key"])
        try:
            self._write_config(config_path, document)
            config = Config.load(config_path)
            if config.missing():
                raise PlatformError("邮箱配置缺少必填项")
            self.probe_mail(config)
            Store(config)
            initialize_demo_database(tenant_root / "data" / "procurement.sqlite3", supplier_email, agent_email)
            with closing(self._connect()) as con:
                con.execute("BEGIN IMMEDIATE")
                count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                if count >= MAX_TENANTS:
                    raise PlatformError("演示环境最多注册 3 个账号")
                con.execute("INSERT INTO users(id,login_email,password_salt,password_hash,agent_email,controller_emails,supplier_email,created_at,origin,claimed) VALUES(?,?,?,?,?,?,?,?,?,1)",
                            (user_id, login_email, salt, digest, agent_email,
                             json.dumps(controllers, ensure_ascii=False), supplier_email, now(), "registered"))
                con.commit()
        except sqlite3.IntegrityError:
            self._remove_tenant(tenant_root)
            raise PlatformError("登录邮箱或 Agent 邮箱已注册") from None
        except BaseException:
            self._remove_tenant(tenant_root)
            raise
        if self.launch_agents:
            self.start_agent(user_id)
        return self.public_user(self._tenant(user_id))

    def authenticate(self, login_email, password):
        login_email = self._email(login_email, "登录邮箱")
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM users WHERE login_email=? COLLATE NOCASE", (login_email,)).fetchone()
        if not row or not isinstance(password, str):
            raise PlatformError("邮箱或密码不正确")
        if not row["claimed"]:
            raise PlatformError("请先为原邮箱账号设置工作台密码")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), row["password_salt"], PASSWORD_ITERATIONS)
        if not hmac.compare_digest(candidate, row["password_hash"]):
            raise PlatformError("邮箱或密码不正确")
        return self.public_user(row)

    def change_mail(self, user_id, *, agent_email, authorization_code, controllers, supplier_email):
        old = self._tenant(user_id)
        agent_email = self._email(agent_email, "Agent 邮箱")
        supplier_email = self._email(supplier_email, "演示供应商收件邮箱")
        controllers = self._controllers(controllers, agent_email)
        path = self.tenants / user_id / "config.yml"
        current_document = yaml.safe_load(path.read_text(encoding="utf-8"))
        old_config = Config.load(path)
        if not isinstance(authorization_code, str):
            raise PlatformError("授权码格式无效")
        authorization_code = authorization_code.strip() or old_config.authorization_code
        if agent_email != old["agent_email"] and not authorization_code:
            raise PlatformError("更换 Agent 邮箱时须填写新邮箱授权码")
        candidate = copy.deepcopy(current_document)
        candidate.setdefault("mail", {}).update({"address": agent_email, "authorization_code": authorization_code})
        candidate.setdefault("control", {})["allowed_senders"] = controllers
        candidate.setdefault("demo", {})["supplier_inbox"] = supplier_email
        self.stop_agent(user_id)
        temporary = path.with_name("config.pending.yml")
        try:
            self._write_config(temporary, candidate)
            new_config = Config.load(temporary)
            self.probe_mail(new_config)
            with closing(self._connect()) as con:
                con.execute("BEGIN IMMEDIATE")
                try:
                    current = con.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
                    if not current:
                        raise PlatformError("账号不存在")
                    duplicate = con.execute("SELECT 1 FROM users WHERE agent_email=? COLLATE NOCASE AND id<>?",
                                            (agent_email, user_id)).fetchone()
                    if duplicate:
                        raise PlatformError("Agent 邮箱已被其他演示账号使用")
                    from procurementdb.store import Database
                    from procurementdb.updates import apply_updates
                    db_path = self.tenants / user_id / "data" / "procurement.sqlite3"
                    with Database(db_path) as db:
                        ids = [r[0] for r in db.connection.execute("SELECT id FROM suppliers ORDER BY id")]
                        changes = [{"key": {"id": identity}, "values": {"email": supplier_email}} for identity in ids]
                        if changes:
                            apply_updates(db, account=old["agent_email"], controller="local-demo-settings",
                                          message_key="settings-" + uuid.uuid4().hex, table="suppliers",
                                          changes=changes, max_rows=1000)
                        owner = db.connection.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()
                        if owner and owner[0] != agent_email:
                            db.connection.execute("UPDATE runtime_metadata SET value=? WHERE key='mail_account'", (agent_email,))
                        elif not owner:
                            db.connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('mail_account',?)", (agent_email,))
                    con.execute("UPDATE users SET agent_email=?,controller_emails=?,supplier_email=? WHERE id=?",
                                (agent_email, json.dumps(controllers, ensure_ascii=False), supplier_email, user_id))
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
            self._write_config(path, candidate)
        except sqlite3.IntegrityError:
            raise PlatformError("Agent 邮箱已被其他演示账号使用") from None
        except BaseException:
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if self.launch_agents:
                self.start_agent(user_id)
        return self.public_user(self._tenant(user_id))

    def _remove_tenant(self, path):
        import shutil
        if path.exists() and path.parent == self.tenants:
            shutil.rmtree(path)

    def start_agent(self, user_id):
        self._tenant(user_id)
        with self.lock:
            self.paused.discard(user_id)
            running = self.processes.get(user_id)
            if running and running.poll() is None:
                return
            config_path = self.tenants / user_id / "config.yml"
            state = read_json(self.tenants / user_id / "data" / "service-status.json", {}) or {}
            if state.get("state") not in {None, "stopped"} and state.get("pid"):
                try:
                    os.kill(int(state["pid"]), 0)
                    heartbeat = datetime.fromisoformat(state.get("heartbeat_at", "")).timestamp()
                    if 0 <= time.time() - heartbeat < 90:
                        return
                except (ValueError, OSError, OverflowError):
                    pass
            agent = ROOT / "agent.py"
            stdout = open(self.tenants / user_id / "data" / "agent-stdout.log", "ab")
            stderr = open(self.tenants / user_id / "data" / "agent-stderr.log", "ab")
            process = subprocess.Popen([sys.executable, "-u", str(agent), "--config", str(config_path), "run"],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stdout.close()
            stderr.close()
            self.processes[user_id] = process

    def stop_agent(self, user_id):
        with self.lock:
            self.paused.add(user_id)
            process = self.processes.get(user_id)
            state = read_json(self.tenants / user_id / "data" / "service-status.json", {}) or {}
            pid = process.pid if process and process.poll() is None else None
            if not pid and state.get("state") != "stopped" and state.get("pid"):
                try:
                    heartbeat = datetime.fromisoformat(state.get("heartbeat_at", "")).timestamp()
                    if 0 <= time.time() - heartbeat < 90:
                        pid = int(state["pid"])
                except (ValueError, TypeError, OverflowError):
                    pass
            if pid:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
            if process and process.poll() is None:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            elif pid:
                deadline = time.time() + 12
                while time.time() < deadline:
                    try:
                        os.kill(int(pid), 0)
                    except OSError:
                        break
                    time.sleep(0.2)
            self.processes.pop(user_id, None)

    def _supervise(self):
        while True:
            with self.lock:
                with closing(self._connect()) as con:
                    ids = [row[0] for row in con.execute("SELECT id FROM users WHERE claimed=1")]
                for user_id in ids:
                    if user_id in self.paused:
                        continue
                    process = self.processes.get(user_id)
                    if process is None or process.poll() is not None:
                        try:
                            self.start_agent(user_id)
                        except Exception:
                            pass
            time.sleep(3)

    def status(self, user_id):
        self._tenant(user_id)
        data = self.tenants / user_id / "data"
        state = read_json(data / "service-status.json", {}) or {}
        heartbeat = state.get("heartbeat_at")
        online = False
        if heartbeat:
            try:
                timestamp = datetime.fromisoformat(heartbeat).timestamp()
                online = 0 <= time.time() - timestamp < 90 and state.get("state") != "stopped"
            except (ValueError, TypeError, OverflowError):
                pass
        return {"online": online, "state": state.get("state", "starting"),
                "heartbeat_at": heartbeat, "last_cycle_at": state.get("last_cycle_at"),
                "last_error": state.get("last_error"), "archived_count": state.get("archived_count", 0),
                "pending_analysis": state.get("pending_analysis", 0),
                "procurement": state.get("procurement", {}).get("counts", {})}

    def data_root(self, user_id):
        self._tenant(user_id)
        return self.tenants / user_id / "data"

    def account_id(self, user_id):
        row = self._tenant(user_id)
        config = Config.load(self.tenants / user_id / "config.yml")
        return config.account_id

    def close(self):
        with self.lock:
            for user_id in list(self.processes):
                self.stop_agent(user_id)
