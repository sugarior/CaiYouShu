from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Messages must contain field names only, never configuration values."""


def update_config_section(path, section, values):
    """Atomically update local settings without echoing credentials."""
    path = Path(path).resolve()
    Config.load(path)
    raw = yaml.safe_load(path.read_text(encoding='utf-8-sig')) or {}
    raw.setdefault(section, {}).update(values)
    descriptor, temporary = tempfile.mkstemp(prefix='.config-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
            yaml.safe_dump(raw,stream,allow_unicode=True,sort_keys=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def address(value):
    if not isinstance(value, str) or not value.isascii() or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
    ):
        raise ConfigError("邮箱地址必须是完整的单个地址，不包含显示名称")
    return value.lower()


@dataclass
class Config:
    path: Path
    address: str = ""
    authorization_code: str = field(default="", repr=False)
    imap_host: str = "imap.163.com"
    imap_port: int = 993
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    folders: list = field(default_factory=lambda: ["*"])
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    allowed_senders: list = field(default_factory=list)
    token: str = field(default="", repr=False)
    subject_prefix: str = "[AGENT]"
    control_mode: str = 'structured'
    control_actions: list = field(default_factory=lambda: ['inquire', 'query', 'answer'])
    control_query_limit: int = 50
    control_answer_chars: int = 12000
    control_update_tables: list = field(default_factory=lambda: ['suppliers','material_groups','supplier_groups','statuses','requisitions','quotations','orders'])
    control_update_max_rows: int = 1000
    procurement_enabled: bool | None = None
    procurement_max_sends: int = 10
    poll_seconds: int = 60
    max_mail_per_folder: int = 100
    max_ai_per_cycle: int = 5
    max_body_chars: int = 16000
    dry_run: bool = False
    keep_originals: bool = True
    storage: Path = field(default_factory=lambda: ROOT / "data")

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        except FileNotFoundError:
            raise ConfigError("缺少 config.yml") from None
        except (yaml.YAMLError, UnicodeError):
            raise ConfigError("config.yml 的 YAML 格式有误，请检查缩进和引号") from None
        if not isinstance(raw, dict):
            raise ConfigError("config.yml 顶层必须是映射")
        for section in ("mail", "deepseek", "control", "service", "delivery", "storage", "procurement"):
            if section in raw and not isinstance(raw[section], dict):
                raise ConfigError(f"{section} 必须是映射")
        def get(section, key, default):
            return raw.get(section, {}).get(key, default)
        cfg = cls(
            path=path,
            address=get("mail", "address", ""),
            authorization_code=get("mail", "authorization_code", ""),
            imap_host=get("mail", "imap_host", "imap.163.com"),
            imap_port=get("mail", "imap_port", 993),
            smtp_host=get("mail", "smtp_host", "smtp.163.com"),
            smtp_port=get("mail", "smtp_port", 465),
            folders=get("mail", "folders", ["*"]),
            api_key=get("deepseek", "api_key", ""),
            base_url=get("deepseek", "base_url", "https://api.deepseek.com"),
            model=get("deepseek", "model", "deepseek-flash"),
            allowed_senders=get("control", "allowed_senders", []),
            token=get("control", "token", ""),
            subject_prefix=get("control", "subject_prefix", "[AGENT]"),
            control_mode=get('control', 'mode', 'structured'),
            control_actions=get('control', 'actions', ['inquire', 'query', 'answer']),
            control_query_limit=get('control', 'query_limit', 50),
            control_answer_chars=get('control', 'answer_chars', 12000),
            control_update_tables=get('control','update_tables',['suppliers','material_groups','supplier_groups','statuses','requisitions','quotations','orders']),
            control_update_max_rows=get('control','update_max_rows',1000),
            procurement_enabled=get('procurement', 'enabled', None),
            procurement_max_sends=get('procurement', 'max_sends_per_cycle', 10),
            poll_seconds=get("service", "poll_seconds", 60),
            max_mail_per_folder=get("service", "max_mail_per_folder", 100),
            max_ai_per_cycle=get("service", "max_ai_per_cycle", 5),
            max_body_chars=get("service", "max_body_chars", 16000),
            dry_run=get("delivery", "dry_run", False),
            keep_originals=get("storage", "keep_originals", True),
        )
        for key in ("address", "authorization_code", "api_key", "token", "imap_host", "smtp_host", "base_url", "model", "subject_prefix", 'control_mode'):
            if not isinstance(getattr(cfg, key), str):
                raise ConfigError(f"{key} 必须是字符串，请使用引号")
        if cfg.address:
            cfg.address = address(cfg.address.strip())
        if not isinstance(cfg.allowed_senders, list):
            raise ConfigError("control.allowed_senders 必须是地址列表")
        cfg.allowed_senders = [address(v) for v in cfg.allowed_senders]
        if cfg.control_mode not in {'structured', 'natural_language'}:
            raise ConfigError('control.mode 须为 structured 或 natural_language')
        if not isinstance(cfg.control_actions, list) or not cfg.control_actions or any(
            not isinstance(v,str) or v not in {'inquire', 'query', 'answer', 'update_data', 'create', 'update', 'pause', 'resume', 'delete', 'list'}
            for v in cfg.control_actions):
            raise ConfigError('control.actions 包含不支持的动作')
        if not isinstance(cfg.control_update_tables,list) or any(not isinstance(v,str) or v not in {
            'suppliers','material_groups','supplier_groups','statuses','requisitions','quotations','orders'}
            for v in cfg.control_update_tables):
            raise ConfigError('control.update_tables 包含不支持的业务表')
        for key, minimum, maximum in (('control_query_limit', 1, 100), ('control_answer_chars', 500, 24000),
                                      ('procurement_max_sends', 1, 100),('control_update_max_rows',1,5000)):
            if type(getattr(cfg, key)) is not int or not minimum <= getattr(cfg, key) <= maximum:
                raise ConfigError(key + ' 超出允许范围')
        if cfg.procurement_enabled is not None and type(cfg.procurement_enabled) is not bool:
            raise ConfigError('procurement.enabled 须为 true 或 false')
        if not isinstance(cfg.folders, list) or not cfg.folders or any(not isinstance(v, str) or not v for v in cfg.folders):
            raise ConfigError("mail.folders 必须是非空文件夹列表")
        if "*" in cfg.folders and cfg.folders != ["*"]:
            raise ConfigError("mail.folders 的 * 必须单独使用")
        for key in ("imap_port", "smtp_port", "poll_seconds", "max_mail_per_folder", "max_ai_per_cycle", "max_body_chars"):
            if type(getattr(cfg, key)) is not int or getattr(cfg, key) <= 0:
                raise ConfigError(f"{key} 必须是正整数")
        if not 1 <= cfg.imap_port <= 65535 or not 1 <= cfg.smtp_port <= 65535:
            raise ConfigError("邮箱端口超出范围")
        if type(cfg.dry_run) is not bool:
            raise ConfigError("delivery.dry_run 必须是 true 或 false")
        if type(cfg.keep_originals) is not bool:
            raise ConfigError("storage.keep_originals 必须是 true 或 false")
        if cfg.base_url.rstrip("/") not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
            raise ConfigError("deepseek.base_url 需使用 DeepSeek 官方 HTTPS 地址")
        cfg.base_url = cfg.base_url.rstrip("/")
        if not cfg.subject_prefix or len(cfg.subject_prefix) > 80:
            raise ConfigError("control.subject_prefix 长度必须为 1–80")
        storage = get("storage", "directory", "data")
        if not isinstance(storage, str) or not storage:
            raise ConfigError("storage.directory 必须是路径字符串")
        cfg.storage = (path.parent / storage).resolve()
        # Startup scripts and status use this fixed service directory.
        if cfg.storage != path.parent / "data":
            raise ConfigError("此安装版本请保持 storage.directory: data")
        return cfg

    def missing(self):
        missing = []
        for key, value in (("mail.address", self.address), ("mail.authorization_code", self.authorization_code),
                           ("deepseek.api_key", self.api_key), ("control.allowed_senders", self.allowed_senders)):
            if not value:
                missing.append(key)
        if self.control_mode == 'structured' and (len(self.token) < 24 or self.token.startswith("REPLACE_")):
            missing.append("control.token（至少 24 个字符）")
        return missing

    @property
    def account_id(self):
        return hashlib.sha256(f"{self.imap_host}:{self.imap_port}:{self.address}".encode()).hexdigest()[:24]
