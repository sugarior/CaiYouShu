"""Offline workflow contracts for a future IMAP/SMTP adapter."""
from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime
import csv
import io
import re

from .store import new_id, now
from .tables import DataError, FIELDS, email_address, parse_table
from .templates import inquiry_attachment, supplier_instructions, parse_supplier_quote


RFQ_PATTERN = r'(?<![A-Z0-9])RFQ-\d{8}-[A-F0-9]{12}(?![A-Z0-9-])'


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise DataError('邮件服务器收信时间须带时区') from None


def csv_value(value):
    value = '' if value is None else str(value)
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value


def prepare_inquiries(db, requisition_ids, request_key, *, source_message_id=None,
                      message_domain='procurement.local'):
    ids = sorted(set(requisition_ids))
    if not ids or not request_key or not re.fullmatch(r'[A-Za-z0-9.-]+', message_domain):
        raise DataError('需要请购单编号、唯一请求键和合法 Message-ID 域名')
    con = db.connection
    with (nullcontext() if con.in_transaction else db.transaction()):
        previous = con.execute('SELECT id FROM inquiry_batches WHERE request_key=?', (request_key,)).fetchone()
        if previous:
            existing_ids = sorted(r[0] for r in con.execute('SELECT DISTINCT ii.requisition_id FROM inquiry_items ii JOIN inquiries i ON i.id=ii.inquiry_id WHERE i.batch_id=?', (previous['id'],)))
            if existing_ids != ids:
                raise DataError('同一个请求键已用于另一组请购单')
            return {'batch_id': previous['id'], 'already_prepared': True,
                    'inquiry_ids': [r[0] for r in con.execute('SELECT id FROM inquiries WHERE batch_id=? ORDER BY supplier_id', (previous['id'],))]}
        requests = []
        matches = defaultdict(list)
        for req_id in ids:
            req = con.execute('SELECT * FROM requisitions WHERE id=?', (req_id,)).fetchone()
            if not req or req['status_id'] != 10:
                raise DataError('请购单不存在或不处于待询价状态：' + req_id)
            active = con.execute("SELECT 1 FROM inquiry_items ii JOIN inquiries i ON i.id=ii.inquiry_id WHERE ii.requisition_id=? AND i.state!='cancelled'", (req_id,)).fetchone()
            if active:
                raise DataError('请购单已有询价；重复调用应使用原请求键：' + req_id)
            candidates = db.candidates([req_id])
            if not candidates:
                raise DataError('物料分组没有对应供应商：' + req_id)
            for candidate in candidates:
                email_address(candidate['email'])
                matches[candidate['supplier_id']].append((dict(req), candidate))
            requests.append(req)
        batch = new_id()
        con.execute('INSERT INTO inquiry_batches VALUES (?,?,?,?)', (batch, request_key, source_message_id, now()))
        inquiry_ids = []
        for supplier_id, pairs in sorted(matches.items()):
            inquiry = 'RFQ-' + now()[:10].replace('-', '') + '-' + new_id()[:12].upper()
            inquiry_ids.append(inquiry)
            supplier = pairs[0][1]
            subject = f'[询价编号:{inquiry}] 物料询价'
            body = [f'{supplier["salutation"] or "您好"}：', supplier_instructions(inquiry),
                    '请按以下需求填写附件报价栏：']
            for index, (req, _) in enumerate(pairs, 1):
                body.append(f'{req["id"]} | {req["material_name"]} | 型号:{req["model"] or "待确认"} | 数量:{req["quantity"]} {req["unit"] or ""} | 需求日期:{req["required_date"] or "待确认"} | 品牌:{req["brand"] or "未指定"}')
            created = now()
            con.execute('INSERT INTO inquiries VALUES (?,?,?,?,?,?,?,?)',
                        (inquiry, batch, supplier_id, supplier['email'], subject, 'queued', None, created))
            # Explicit columns keep optional future fields from changing the attachment contract.
            for req, _ in pairs:
                con.execute('INSERT INTO inquiry_items VALUES (?,?)', (inquiry, req['id']))
            con.execute('INSERT INTO inquiry_outbox (id,inquiry_id,recipient,subject,body,attachment_name,attachment_csv,internet_message_id,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (new_id(), inquiry, supplier['email'], subject, '\n\n'.join(body),
                         f'询价单_{inquiry}.csv', inquiry_attachment(inquiry, supplier_id, [req for req, _ in pairs]),
                         f'<{inquiry.lower()}@{message_domain.lower()}>', 'pending', created))
        return {'batch_id': batch, 'already_prepared': False, 'inquiry_ids': inquiry_ids,
                'requisition_count': len(requests), 'supplier_count': len(matches)}


def claim_outbox(db):
    """Claim exactly one pending record. The caller owns SMTP delivery."""
    with db.transaction():
        row = db.connection.execute("SELECT * FROM inquiry_outbox WHERE state='pending' ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return None
        db.connection.execute("UPDATE inquiry_outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
        db.connection.execute("UPDATE inquiries SET state='sending' WHERE id=?", (row['inquiry_id'],))
        return dict(row, state='sending', attempts=row['attempts'] + 1)


def mark_sent(db, outbox_id, *, sent_at=None):
    sent_at = sent_at or now()
    instant(sent_at)
    with db.transaction():
        row = db.connection.execute("SELECT * FROM inquiry_outbox WHERE id=? AND state='sending'", (outbox_id,)).fetchone()
        if not row:
            raise DataError('只有已领取的待发记录可确认发送')
        db.connection.execute("UPDATE inquiry_outbox SET state='sent',sent_at=? WHERE id=?", (sent_at, outbox_id))
        db.connection.execute("UPDATE inquiries SET state='awaiting_quote',sent_at=? WHERE id=?", (sent_at, row['inquiry_id']))
        db.connection.execute('UPDATE requisitions SET status_id=20 WHERE status_id=10 AND id IN (SELECT requisition_id FROM inquiry_items WHERE inquiry_id=?)', (row['inquiry_id'],))


def recover_sending(db):
    """After interruption, an SMTP result is uncertain and must not auto-resend."""
    with db.transaction():
        db.connection.execute("UPDATE inquiries SET state='needs_review' WHERE id IN (SELECT inquiry_id FROM inquiry_outbox WHERE state='sending')")
        return db.connection.execute("UPDATE inquiry_outbox SET state='uncertain',last_error='SMTP delivery result requires review' WHERE state='sending'").rowcount


def match_inquiry(db, sender, subject, in_reply_to, references):
    con = db.connection
    tokens = set(re.findall(RFQ_PATTERN, subject.upper()))
    subject_id = None
    if len(tokens) > 1:
        raise DataError('标题包含多个询价编号')
    if tokens:
        subject_id = next(iter(tokens))
        if not con.execute('SELECT 1 FROM inquiries WHERE id=?', (subject_id,)).fetchone():
            raise DataError('标题询价编号不存在')
    header_ids = set()
    for message_id in set(re.findall(r'<[^<>\s]+>', (in_reply_to or '') + ' ' + (references or ''))):
        header_ids.update(r[0] for r in con.execute('SELECT inquiry_id FROM inquiry_outbox WHERE internet_message_id=?', (message_id,)))
        header_ids.update(r[0] for r in con.execute('SELECT inquiry_id FROM mail_messages WHERE internet_message_id=? AND inquiry_id IS NOT NULL', (message_id,)))
    if len(header_ids) > 1 or (subject_id and header_ids and subject_id not in header_ids):
        raise DataError('标题询价编号与邮件回复关系冲突或对应多轮询价')
    selected = subject_id or next(iter(header_ids), None)
    if not selected:
        raise DataError('缺少可识别的询价编号及邮件回复关系')
    inquiry = con.execute('SELECT * FROM inquiries WHERE id=?', (selected,)).fetchone()
    if inquiry['recipient_email'].lower() != sender:
        raise DataError('发件邮箱与该询价的供应商邮箱不一致')
    if inquiry['state'] not in ('awaiting_quote', 'quoted') or not inquiry['sent_at']:
        raise DataError('该询价尚未确认发送或需要人工核对')
    return inquiry, 'subject' if subject_id else 'reply_headers'


def receive_quote(db, *, account, message_key, sender, subject, received_at,
                  data=None, filename='报价单.csv', internet_message_id=None,
                  in_reply_to='', references='', automatic=False, validation_error=None):
    """Persist one incoming event and atomically import its complete quotation table.

    Transport must supply server receipt time and a stable raw-message hash/key.
    Controller authorization and IMAP networking belong to the existing agent.
    """
    account, sender = email_address(account), email_address(sender)
    received = instant(received_at)
    if not message_key:
        raise DataError('缺少稳定邮件去重键')
    con = db.connection
    with db.transaction():
        previous = con.execute('SELECT * FROM mail_messages WHERE account=? AND message_key=?', (account, message_key)).fetchone()
        if previous:
            return {'message_id': previous['id'], 'state': previous['state'], 'duplicate': True,
                    'inquiry_id': previous['inquiry_id'], 'error': previous['error']}
        message = new_id()
        con.execute('INSERT INTO mail_messages (id,account,message_key,internet_message_id,sender,subject,in_reply_to,references_header,received_at,state) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (message, account, message_key, internet_message_id, sender, subject, in_reply_to, references, received_at, 'received'))
        if automatic:
            con.execute("UPDATE mail_messages SET state='ignored',error='自动回执或退信' WHERE id=?", (message,))
            return {'message_id': message, 'state': 'ignored'}
        con.execute('SAVEPOINT quote_import')
        try:
            inquiry, method = match_inquiry(db, sender, subject, in_reply_to, references)
            if received < instant(inquiry['sent_at']):
                raise DataError('来信早于询价发送时间')
            if validation_error:
                raise DataError(validation_error)
            if data is None:
                raise DataError('缺少报价表格附件；普通回信不视为有效报价')
            rows = parse_supplier_quote(data, filename, inquiry['id'])
            if not rows:
                raise DataError('报价表格为空')
            valid_requests = {r[0] for r in con.execute('SELECT requisition_id FROM inquiry_items WHERE inquiry_id=?', (inquiry['id'],))}
            for row in rows:
                if row['supplier_id'] != inquiry['supplier_id'] or row['requisition_id'] not in valid_requests:
                    raise DataError('报价表中的供应商或请购单不属于本次询价')
            inserted = db._insert_rows('quotations', rows, extra={'inquiry_id': inquiry['id'], 'source_message_id': message})
            con.execute("UPDATE mail_messages SET state='imported',inquiry_id=?,match_method=? WHERE id=?", (inquiry['id'], method, message))
            incomplete = con.execute('SELECT 1 FROM inquiry_items ii WHERE ii.inquiry_id=? AND NOT EXISTS (SELECT 1 FROM quotations q WHERE q.inquiry_id=ii.inquiry_id AND q.requisition_id=ii.requisition_id) LIMIT 1', (inquiry['id'],)).fetchone()
            con.execute('UPDATE inquiries SET state=? WHERE id=?', ('awaiting_quote' if incomplete else 'quoted', inquiry['id']))
            for req_id in valid_requests:
                missing = con.execute("SELECT 1 FROM inquiry_items ii JOIN inquiries i ON i.id=ii.inquiry_id WHERE ii.requisition_id=? AND i.state!='cancelled' AND NOT EXISTS (SELECT 1 FROM quotations q WHERE q.inquiry_id=ii.inquiry_id AND q.requisition_id=ii.requisition_id) LIMIT 1", (req_id,)).fetchone()
                if not missing:
                    con.execute('UPDATE requisitions SET status_id=30 WHERE id=? AND status_id=20', (req_id,))
            con.execute('RELEASE quote_import')
            return {'message_id': message, 'state': 'imported', 'inquiry_id': inquiry['id'],
                    'inserted': inserted, 'match_method': method}
        except DataError as exc:
            con.execute('ROLLBACK TO quote_import')
            con.execute('RELEASE quote_import')
            con.execute("UPDATE mail_messages SET state='needs_review',error=? WHERE id=?", (str(exc), message))
            return {'message_id': message, 'state': 'needs_review', 'error': str(exc)}
