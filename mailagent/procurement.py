"""Deterministic procurement adapter for the existing authenticated mail agent."""
from __future__ import annotations

import base64
import csv
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import smtplib

from procurementdb import Database, DataError
from procurementdb.store import new_id
from procurementdb.tables import dump, parse_table
from procurementdb.workflow import RFQ_PATTERN, mark_sent, recover_sending, instant
from .mailio import close_smtp, smtp_connection, new_message_id
from .store import atomic_json, now, read_json
from .tabular import ensure_extractions, table_kind


LOG = logging.getLogger('email-agent')


def settings_path(store):
    return store.root / 'procurement.json'


def settings(config, store):
    if config.procurement_enabled is False:
        return None
    value = read_json(settings_path(store), {})
    if config.procurement_enabled is True and (not value.get('enabled') or value.get('account') != config.address):
        value = enable(config, store)
    if not value.get('enabled') or value.get('account') != config.address:
        return None
    return dict(value, max_sends_per_cycle=config.procurement_max_sends) if config.procurement_enabled is not None else value


def enable(config, store):
    if config.dry_run:
        raise DataError('请先关闭 delivery.dry_run，采购询价不会自动补发预览任务')
    path = config.storage / 'procurement.sqlite3'
    if not path.exists():
        raise DataError('请先初始化采购数据库并导入供应商等基础数据')
    with Database(path) as db:
        db.initialize()
        with db.transaction():
            owner = db.connection.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()
            if owner and owner[0] != config.address:
                raise DataError('采购数据库已绑定其他邮箱，请使用独立数据库')
            db.connection.execute("INSERT OR IGNORE INTO runtime_metadata VALUES ('mail_account',?)", (config.address,))
        if not db.summary()['suppliers'] or not db.summary()['statuses']:
            raise DataError('采购数据库缺少供应商或状态基础数据')
    existing = read_json(settings_path(store), {})
    value = existing if existing.get('enabled') and existing.get('account') == config.address else {
        'enabled': True, 'account': config.address, 'enabled_at': now(), 'max_sends_per_cycle': config.procurement_max_sends}
    atomic_json(settings_path(store), value)
    return value


def disable(config, store):
    value = read_json(settings_path(store), {})
    value.update(enabled=False, disabled_at=now())
    atomic_json(settings_path(store), value)


def snapshot(config):
    options = read_json(config.storage/'accounts'/config.account_id/'procurement.json', {})
    if config.procurement_enabled is False or not options.get('enabled') or options.get('account') != config.address:
        return {'enabled': False}
    with Database(config.storage/'procurement.sqlite3', readonly=True) as db:
        return {'enabled': True, 'enabled_at': options['enabled_at'], 'counts': db.summary(),
                'inquiry_delivery': {r['state']: r['n'] for r in db.connection.execute('SELECT state,COUNT(*) AS n FROM inquiry_outbox GROUP BY state')}}


def attachment_csv(parsed, selected_name=None):
    """Reconstruct a complete table from the existing archive's extracted cells.

    This works with keep_originals=false: no need to retain binary attachments.
    """
    ensure_extractions(parsed)
    candidates = [v for v in parsed.get('attachments', []) if table_kind(v) in {'csv', 'xlsx'}
                  and (selected_name is None or v.get('filename') == selected_name)]
    if len(candidates) != 1:
        raise DataError('请附上唯一一份 CSV/XLSX 表格；有多个附件时，请在正文写明要使用的完整文件名')
    attachment = candidates[0]
    extraction = attachment.get('extraction', {})
    if extraction.get('status') != 'ok':
        raise DataError(extraction.get('error', '附件未完整提取'))
    sheets = [[row for row in sheet['rows'] if any(str(v).strip() for v in row)]
              for sheet in extraction.get('sheets', [])]
    sheets = [rows for rows in sheets if rows]
    if len(sheets) != 1:
        raise DataError('业务附件须包含一个非空工作表')
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows(sheets[0])
    return stream.getvalue().encode('utf-8-sig'), (attachment.get('filename') or '表格') + '.csv'


def headers(parsed):
    result = {}
    for key, value in parsed.get('headers', []):
        result.setdefault(key.lower(), []).append(value.strip())
    return result


def ordinary_sender(config, item):
    parsed = item['parsed']
    if parsed.get('agent_marker') or parsed.get('parse_error') or parsed.get('parse_defects'):
        return None
    senders = parsed.get('from', [])
    if parsed.get('from_header_count') != 1 or len(senders) != 1 or senders[0] == config.address:
        return None
    hdr = headers(parsed)
    if any(v.lower() != 'no' for v in hdr.get('auto-submitted', [])):
        return None
    if any(k in hdr for k in ('list-id', 'list-unsubscribe', 'x-auto-response-suppress')):
        return None
    if any(v.lower() in {'bulk', 'list', 'junk'} for v in hdr.get('precedence', [])):
        return None
    if '<>' in hdr.get('return-path', []) or any('multipart/report' in v.lower() for v in hdr.get('content-type', [])):
        return None
    if senders[0].split('@', 1)[0] in {'mailer-daemon', 'postmaster', 'noreply', 'no-reply', 'do-not-reply'}:
        return None
    return senders[0]


class Procurement:
    def __init__(self, config, store, smtp_factory=smtp_connection, stop_requested=lambda: False):
        self.config, self.store = config, store
        self.smtp_factory, self.stop_requested = smtp_factory, stop_requested
        self.options = settings(config, store)
        self.path = config.storage / 'procurement.sqlite3'

    @property
    def enabled(self):
        return bool(self.options and self.path.exists())

    def open(self):
        db = Database(self.path)
        owner = db.connection.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()
        if not owner or owner[0] != self.config.address:
            db.__exit__()
            raise DataError('采购数据库邮箱绑定不一致')
        return db

    def submit(self, item, sender, command, received_at):
        if not self.enabled:
            raise DataError('采购自动询价尚未启用')
        if self.config.dry_run:
            raise DataError('当前是预览模式，不创建自动询价')
        if set(command) - {'action', 'attachment'} or command.get('action') != 'inquire':
            raise DataError('询价命令仅接受 action: inquire 和可选 attachment 文件名')
        if ordinary_sender(self.config, item) != sender or sender not in self.config.allowed_senders:
            raise DataError('控制来信不是有效的普通邮件')
        if instant(received_at) < instant(self.options['enabled_at']):
            raise DataError('采购启用之前的控制邮件不会触发询价，请重新发送')
        attachment = command.get('attachment')
        if attachment is not None and (not isinstance(attachment, str) or len(attachment) > 240):
            raise DataError('attachment 须为附件完整文件名')
        data, filename = attachment_csv(item['parsed'], attachment)
        rows = parse_table(data, filename, 'requisitions')
        if not rows or any(row['status_id'] != 10 for row in rows):
            raise DataError('请购单须至少有一行，状态唯一编号全部填写10（待询价）')
        ids = sorted(row['id'] for row in rows)
        if len(ids) != len(set(ids)):
            raise DataError('请购单唯一编号重复，每个物料需求行须有独立编号')
        request_key = hashlib.sha256(dump([self.config.address, ids]).encode()).hexdigest()
        with self.open() as db, db.transaction():
            source = db.connection.execute('SELECT id FROM mail_messages WHERE account=? AND message_key=?',
                                           (self.config.address, item['id'])).fetchone()
            source_id = source[0] if source else new_id()
            if not source:
                db.connection.execute('INSERT INTO mail_messages (id,account,message_key,internet_message_id,sender,subject,received_at,state) VALUES (?,?,?,?,?,?,?,?)',
                                      (source_id, self.config.address, item['id'], item['parsed'].get('message_id'),
                                       sender, item['parsed']['subject'], received_at, 'received'))
            imported = db._import_file('requisitions', data, filename, rows, extra={'source_message_id': source_id})
            prepared = db.prepare_inquiries(ids, request_key, source_message_id=source_id,
                                            message_domain=self.config.address.rsplit('@', 1)[1])
            db.connection.execute("UPDATE mail_messages SET state='imported' WHERE id=?", (source_id,))
            total = len(prepared['inquiry_ids'])
        return {'ok': True, 'action': 'inquire', 'batch_id': prepared['batch_id'],
                '请购单行数': len(rows), '新增请购单': imported['inserted'],
                '供应商询价封数': total, '重复请求': prepared['already_prepared'],
                '说明': '已按供应商生成询价单，后台将自动分批发送并等待报价；无需另发发送命令。'}

    def notice(self, key, sender, subject, body):
        if sender not in self.config.allowed_senders:
            return
        identity = hashlib.sha256(('procurement-notice:' + key + ':' + sender).encode()).hexdigest()
        self.store.enqueue(identity, {'kind': 'receipt', 'control_sender': sender, 'source_mail': key,
                                     'recipients': [sender], 'subject': subject, 'body': body,
                                     'message_id': new_message_id(self.config, identity)})

    def handle_reply(self, item, received_at):
        if not self.enabled or instant(received_at) < instant(self.options['enabled_at']):
            return False
        sender = ordinary_sender(self.config, item)
        if not sender:
            return False
        parsed, hdr = item['parsed'], headers(item['parsed'])
        subject = parsed.get('subject', '')
        in_reply_to = ' '.join(hdr.get('in-reply-to', []))
        references = ' '.join(hdr.get('references', []))
        with self.open() as db:
            token = bool(re.search(RFQ_PATTERN, subject.upper()))
            replied = any(db.connection.execute('SELECT 1 FROM inquiry_outbox WHERE internet_message_id=?', (identity,)).fetchone()
                          for identity in re.findall(r'<[^<>\s]+>', in_reply_to + ' ' + references))
            invited = db.connection.execute("SELECT 1 FROM inquiries WHERE recipient_email=? AND sent_at IS NOT NULL", (sender,)).fetchone()
            if not (token or replied or invited):
                return False
            error, data, filename = None, None, '报价.csv'
            try:
                data, filename = attachment_csv(parsed)
            except DataError as exc:
                error = str(exc)
            result = db.receive_quote(account=self.config.address, message_key=item['id'], sender=sender,
                                      subject=subject, received_at=received_at, data=data, filename=filename,
                                      internet_message_id=parsed.get('message_id'), in_reply_to=in_reply_to,
                                      references=references, validation_error=error)
            inquiry_id = result.get('inquiry_id')
            owner = db.connection.execute('SELECT m.sender FROM inquiries i JOIN inquiry_batches b ON b.id=i.batch_id JOIN mail_messages m ON m.id=b.source_message_id WHERE i.id=?', (inquiry_id,)).fetchone() if inquiry_id else None
            recipients = [owner[0]] if owner else self.config.allowed_senders
            for controller in recipients:
                self.notice(item['id'], controller, '[采购回执] ' + ('报价已入库' if result['state'] == 'imported' else '报价待核对'),
                            '供应商来信：' + sender + '\n主题：' + subject + '\n' + json.dumps(result, ensure_ascii=False, indent=2))
        item['procurement_result'] = result
        self.store.finish(item, 'procurement_' + result['state'])
        return True

    def deliver(self):
        if not self.enabled or self.config.dry_run:
            return {'sent': 0}
        sent, failed, client = 0, 0, None
        with self.open() as db:
            recover_sending(db)
            rows = db.connection.execute("SELECT o.*,m.sender AS controller FROM inquiry_outbox o JOIN inquiries i ON i.id=o.inquiry_id JOIN inquiry_batches b ON b.id=i.batch_id JOIN mail_messages m ON m.id=b.source_message_id WHERE o.state='pending' ORDER BY o.created_at,o.id LIMIT ?", (self.options.get('max_sends_per_cycle', 10),)).fetchall()
            try:
                for row in rows:
                    if self.stop_requested():
                        break
                    if row['controller'] not in self.config.allowed_senders:
                        with db.transaction():
                            db.connection.execute("UPDATE inquiry_outbox SET state='cancelled',last_error='Controller is no longer allowed' WHERE id=?", (row['id'],))
                            db.connection.execute("UPDATE inquiries SET state='cancelled' WHERE id=?", (row['inquiry_id'],))
                        continue
                    entered = False
                    try:
                        if client is None:
                            client = self.smtp_factory(self.config)
                        message = EmailMessage(policy=SMTP)
                        message['From'], message['To'] = self.config.address, row['recipient']
                        message['Subject'], message['Message-ID'] = row['subject'], row['internet_message_id']
                        message['Date'] = formatdate(localtime=False)
                        message['X-Email-Agent-Instance'] = self.store.state['instance']
                        message['X-Procurement-Inquiry'] = row['inquiry_id']
                        message.set_content(row['body'])
                        message.add_attachment(row['attachment_csv'], maintype='text', subtype='csv', filename=row['attachment_name'])
                        with db.transaction():
                            changed = db.connection.execute("UPDATE inquiry_outbox SET state='sending',attempts=attempts+1 WHERE id=? AND state='pending'", (row['id'],)).rowcount
                            if changed != 1:
                                continue
                            db.connection.execute("UPDATE inquiries SET state='sending' WHERE id=?", (row['inquiry_id'],))
                        entered = True
                        refused = client.send_message(message, from_addr=self.config.address, to_addrs=[row['recipient']])
                        if refused:
                            raise smtplib.SMTPRecipientsRefused(refused)
                        mark_sent(db, row['id'])
                        sent += 1
                    except Exception as exc:
                        definite = isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError))
                        state = 'failed' if definite else 'uncertain' if entered else 'pending'
                        with db.transaction():
                            db.connection.execute('UPDATE inquiry_outbox SET state=?,last_error=? WHERE id=?', (state, type(exc).__name__, row['id']))
                            if state != 'pending':
                                db.connection.execute("UPDATE inquiries SET state='needs_review' WHERE id=?", (row['inquiry_id'],))
                        self.notice(row['id'], row['controller'], '[采购回执] 询价发送异常',
                                    f'询价编号：{row["inquiry_id"]}\n供应商邮箱：{row["recipient"]}\n状态：{state}\n错误类型：{type(exc).__name__}\n结果不确定时不会自动重发，请核对。')
                        LOG.warning('采购询价发送未完成；类型=%s；状态=%s', type(exc).__name__, state)
                        failed += 1
                        if client is not None:
                            close_smtp(client)
                            client = None
                        if state == 'pending':
                            break
            finally:
                if client is not None:
                    close_smtp(client)
            batches = db.connection.execute('SELECT DISTINCT b.id,m.sender FROM inquiry_batches b JOIN mail_messages m ON m.id=b.source_message_id').fetchall()
            for batch in batches:
                counts = {r['state']: r['n'] for r in db.connection.execute('SELECT o.state,COUNT(*) AS n FROM inquiry_outbox o JOIN inquiries i ON i.id=o.inquiry_id WHERE i.batch_id=? GROUP BY o.state', (batch['id'],))}
                if counts and not (counts.get('pending') or counts.get('sending')):
                    self.notice('batch:' + batch['id'], batch['sender'], '[采购回执] 询价批次发送完成',
                                '批次：' + batch['id'] + '\n发送结果：' + json.dumps(counts, ensure_ascii=False) + '\n后台将持续接收供应商回复，并校验报价入库。')
        return {'sent': sent, 'failed': failed}
