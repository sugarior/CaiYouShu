from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import io
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock

from mailagent.config import Config
from mailagent.engine import Engine
from mailagent.procurement import Procurement, enable, disable
from mailagent.store import Store, now
from procurementdb import Database
from procurementdb.tables import FIELDS, parse_table
from procurementdb.templates import INQUIRY_HEADERS, csv_data


def table_data(table, rows):
    return csv_data([label for label, _ in FIELDS[table]], rows)


class ProcurementMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = Config(path=root/'config.yml', storage=root/'data', address='agent@163.com',
                          authorization_code='dummy', api_key='dummy-key',
                          allowed_senders=['controller@example.com'], token='local-test-token-1234567890')
        self.store = Store(self.cfg)
        self.path = self.cfg.storage/'procurement.sqlite3'
        with Database(self.path) as db:
            db.initialize()
            with db.transaction():
                fixtures = {'material_groups': [('G1','电机'),('G2','无供应商组')],
                            'suppliers': [('S1','甲公司','supplier1@example.com','张先生'),
                                          ('S2','乙公司','supplier2@example.com','李女士')],
                            'supplier_groups': [('S1','G1'),('S2','G1')],
                            'statuses': [(10,'待询价'),(20,'待报价'),(30,'待下单')]}
                for table, rows in fixtures.items():
                    db._insert_rows(table, parse_table(table_data(table, rows), 'fixture.csv', table))
        enable(self.cfg,self.store)
        self.ai = Mock()
        self.ai.analyze.side_effect = RuntimeError('AI deliberately offline')
        self.smtp = Mock()
        self.smtp.send_message.return_value = {}
        self.factory = Mock(return_value=self.smtp)
        self.engine = Engine(self.cfg,self.store,self.ai,self.factory)
        self.nonce = 0

    def req(self, identifier='R1', group='G1'):
        return [identifier,10,group,'电机' if group=='G1' else '无供应商组','项目A','2026-10-31',
                '06.04.0047','伺服电机','M-01','件','10','2027-05-01','品牌A','']

    def archive(self, body, subject, sender, data=None, filename='请购单.csv', received=None, reply_id=None):
        self.nonce += 1
        msg = EmailMessage()
        msg['From'],msg['To'],msg['Subject'] = sender,self.cfg.address,subject
        msg['Message-ID'] = f'<test-{self.nonce}@example.com>'
        if reply_id:
            msg['In-Reply-To'] = reply_id
        msg.set_content(body)
        if data is not None:
            msg.add_attachment(data,maintype='text',subtype='csv',filename=filename)
        item=self.store.archive(msg.as_bytes(),{'folder':'INBOX','uidvalidity':'1','uid':self.nonce,
                                               'received_at':received or now(),'folder_flags':[],'flags':[]})
        self.store.save_state()
        return item

    def control(self, rows=None, token=None, sender='controller@example.com', **kwargs):
        body=f'token: "{token or self.cfg.token}"\naction: inquire\n'
        return self.archive(body,'[AGENT] 发起采购询价',sender,
                            table_data('requisitions',rows or [self.req()]),**kwargs)

    def submit(self, rows=None):
        item=self.control(rows)
        self.engine.process_one(item)
        self.assertTrue(item['control_result']['ok'],item.get('control_result'))
        return item

    def inquiries(self):
        return [call.args[0] for call in self.smtp.send_message.call_args_list
                if call.args[0].get('X-Procurement-Inquiry')]

    def filled(self, message, *, missing=None, wrong_inquiry=False):
        rows=list(csv.reader(io.StringIO(next(message.iter_attachments()).get_payload(decode=True).decode('utf-8-sig'))))
        self.assertEqual(rows[0],INQUIRY_HEADERS)
        for row in rows[1:]:
            row[12:] = ['M-01','128.50','10','2027-04-30','1','CNY','1','13%']
            if missing:
                row[rows[0].index(missing)]=''
            if wrong_inquiry:
                row[0]='RFQ-20261031-AAAAAAAAAAAA'
        return csv_data(rows[0],rows[1:])

    def test_authenticated_control_prepares_template_and_sends_without_ai(self):
        self.submit()
        self.ai.analyze.assert_not_called()
        self.ai.parse_command.assert_not_called()
        self.engine.deliver()
        messages=self.inquiries()
        self.assertEqual(len(messages),2)
        for message in messages:
            self.assertIn('询价编号:',str(message['Subject']))
            self.assertIn('不构成下单承诺',message.get_body().get_content())
            self.assertIn('报价见附件',message.get_body().get_content())
            self.assertIn('询价单_',next(message.iter_attachments()).get_filename())
            rows=list(csv.reader(io.StringIO(next(message.iter_attachments()).get_payload(decode=True).decode('utf-8-sig'))))
            self.assertEqual(rows[1][6],'伺服电机')
            self.assertEqual(rows[1][12:],['']*8)
        self.engine.deliver()
        self.assertEqual(len(self.inquiries()),2)
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT status_id FROM requisitions').fetchone()[0],20)

    def test_wrong_token_or_non_controller_cannot_import_or_send(self):
        for item in (self.control(token='bad'),self.control(sender='stranger@example.com')):
            self.engine.process_one(item)
        self.engine.deliver()
        self.factory.assert_not_called()
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'],0)

    def test_duplicate_new_control_message_does_not_create_new_inquiries(self):
        self.submit()
        second=self.submit()
        self.assertTrue(second['control_result']['重复请求'])
        self.engine.deliver()
        self.assertEqual(len(self.inquiries()),2)

    def test_partial_bad_request_rolls_back_import_and_queue(self):
        item=self.control([self.req(),self.req('R2','G2')])
        self.engine.process_one(item)
        self.assertFalse(item['control_result']['ok'])
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'],0)
            self.assertEqual(db.summary()['inquiries'],0)

    def test_old_control_and_disabled_procurement_do_not_import(self):
        item=self.control(received=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
        self.engine.process_one(item)
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'],0)
        disable(self.cfg,self.store)
        current=self.control()
        self.engine.process_one(current)
        self.assertFalse(current['control_result']['ok'])

    def test_combined_inquiry_reply_enters_quote_table_and_notifies_controller(self):
        self.submit()
        self.engine.deliver()
        for index,message in enumerate(self.inquiries()):
            item=self.archive('报价见附件','Re: '+str(message['Subject']),str(message['To']),
                              self.filled(message),filename='已填询价单.csv',reply_id=str(message['Message-ID']))
            self.engine.process_one(item)
            self.assertEqual(item['processing']['status'],'procurement_imported')
        self.ai.analyze.assert_not_called()
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['quotations'],2)
            self.assertEqual(db.connection.execute('SELECT status_id FROM requisitions').fetchone()[0],30)
            prices=[r[0] for r in db.connection.execute('SELECT price FROM quotations')]
            self.assertEqual(prices,['128.5','128.5'])
            self.assertFalse(db.check()['foreign_key_errors'])
        self.assertTrue(any('报价已入库' in job['subject'] for _,job in self.store.jobs()))

    def test_quote_requires_tax_delivery_and_original_inquiry_id(self):
        self.submit()
        self.engine.deliver()
        message=self.inquiries()[0]
        for data in (self.filled(message,missing='税率'),self.filled(message,wrong_inquiry=True)):
            item=self.archive('报价见附件','Re: '+str(message['Subject']),str(message['To']),data)
            self.engine.process_one(item)
            self.assertEqual(item['processing']['status'],'procurement_needs_review')
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['quotations'],0)

    def test_wrong_supplier_sender_never_imports_quote(self):
        self.submit()
        self.engine.deliver()
        message=self.inquiries()[0]
        item=self.archive('报价见附件','Re: '+str(message['Subject']),'another@example.com',self.filled(message))
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'procurement_needs_review')

    def test_extracted_cells_work_without_retaining_original_attachment(self):
        self.cfg.keep_originals=False
        self.store=Store(self.cfg)
        self.engine=Engine(self.cfg,self.store,self.ai,self.factory)
        item=self.submit()
        self.assertNotIn('content_base64',item['parsed']['attachments'][0])
        self.engine.deliver()
        message=self.inquiries()[0]
        item=self.archive('报价见附件','Re: '+str(message['Subject']),str(message['To']),self.filled(message))
        self.assertNotIn('content_base64',item['parsed']['attachments'][0])
        self.engine.process_one(item)
        self.assertEqual(item['processing']['status'],'procurement_imported')

    def test_removed_controller_cancels_unsent_inquiries(self):
        self.submit()
        self.cfg.allowed_senders=[]
        self.engine.deliver()
        self.factory.assert_not_called()
        with Database(self.path,readonly=True) as db:
            self.assertEqual([r[0] for r in db.connection.execute('SELECT state FROM inquiry_outbox')],['cancelled','cancelled'])

    def test_smtp_ambiguity_is_uncertain_and_never_resends(self):
        self.submit()
        # Controller receipt is first; ambiguous result is only simulated for supplier mail.
        def send(message,**kwargs):
            if message.get('X-Procurement-Inquiry'):
                raise TimeoutError()
            return {}
        self.smtp.send_message.side_effect=send
        self.engine.deliver()
        self.assertEqual(len(self.inquiries()),2)
        self.engine.deliver()
        self.assertEqual(len(self.inquiries()),2)
        with Database(self.path,readonly=True) as db:
            self.assertEqual([r[0] for r in db.connection.execute('SELECT state FROM inquiry_outbox')],['uncertain','uncertain'])

    def test_quit_failure_does_not_repeat_successful_supplier_mail(self):
        self.submit()
        self.smtp.quit.side_effect=OSError('quit failed')
        self.engine.deliver()
        self.engine.deliver()
        self.assertEqual(len(self.inquiries()),2)

    def test_dry_run_refuses_new_inquiry(self):
        self.cfg.dry_run=True
        item=self.control()
        self.engine.process_one(item)
        self.assertFalse(item['control_result']['ok'])
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['inquiries'],0)

    def test_control_payload_cannot_change_supplier_recipient(self):
        item=self.control()
        item['parsed']['text'] += 'recipients: [stranger@example.com]\n'
        self.engine.process_one(item)
        self.assertFalse(item['control_result']['ok'])
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'],0)

    def test_definite_smtp_refusal_is_failed_not_uncertain(self):
        self.submit()
        def send(message,**kwargs):
            if message.get('X-Procurement-Inquiry'):
                raise smtplib.SMTPRecipientsRefused({str(message['To']):(550,b'rejected')})
            return {}
        self.smtp.send_message.side_effect=send
        self.engine.deliver()
        with Database(self.path,readonly=True) as db:
            self.assertEqual([r[0] for r in db.connection.execute('SELECT state FROM inquiry_outbox')],['failed','failed'])

    def test_quote_is_deduplicated_after_archive_processing_retry(self):
        self.submit()
        self.engine.deliver()
        message=self.inquiries()[0]
        item=self.archive('报价见附件','Re: '+str(message['Subject']),str(message['To']),self.filled(message))
        self.engine.process_one(item)
        item['processing']['status']='pending'
        self.engine.process_one(item)
        with Database(self.path,readonly=True) as db:
            self.assertEqual(db.summary()['quotations'],1)
        self.assertTrue(item['procurement_result']['duplicate'])

    def test_rfqs_still_process_when_general_ai_backlog_fails(self):
        self.submit()
        self.engine.deliver()
        self.cfg.max_ai_per_cycle=1
        message=self.inquiries()[0]
        quote=self.archive('报价见附件','Re: '+str(message['Subject']),str(message['To']),self.filled(message))
        self.archive('普通来信','普通主题','person@example.com')
        self.archive('普通来信2','普通主题2','person2@example.com')
        self.engine.process_pending()
        self.assertEqual(self.store.mail(quote['id'])['processing']['status'],'procurement_imported')
        self.assertEqual(self.ai.analyze.call_count,1)


if __name__=='__main__':
    unittest.main()
