from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from procurementdb import Database, DataError
from procurementdb.store import now
from procurementdb.tables import FIELDS, FILES, normalize, parse_table
from procurementdb.workflow import claim_outbox, mark_sent, recover_sending


def csv_bytes(table, rows, encoding='utf-8-sig'):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow([label for label, _ in FIELDS[table]])
    writer.writerows(rows)
    return stream.getvalue().encode(encoding)


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'db.sqlite3')
        self.addCleanup(self.db.__exit__)
        self.db.initialize()
        with self.db.transaction():
            fixtures = {
                'material_groups': [('G1', '电机'), ('G2', '阀门'), ('G3', '无供应商组')],
                'suppliers': [('S1', '甲公司', 'first@example.com', '张先生'),
                              ('S2', '乙公司', 'second@example.com', '李女士')],
                'supplier_groups': [('S1', 'G1'), ('S1', 'G2'), ('S2', 'G2')],
                'statuses': [(10, '待询价'), (20, '待报价'), (30, '待下单')],
            }
            for table, rows in fixtures.items():
                self.db._insert_rows(table, parse_table(csv_bytes(table, rows), 'fixture.csv', table))

    def req(self, identifier='R1', group='G1', quantity='2.50'):
        group_name = {'G1': '电机', 'G2': '阀门', 'G3': '无供应商组'}[group]
        return [identifier, 10, group, group_name, '项目A', '2026-10-30 16:44:00',
                '06.04.0047', '测试物料', 'M-01', '件', quantity, '2027-05-25', '品牌A', '']

    def ingest(self, rows=None):
        return self.db.import_requisitions(csv_bytes('requisitions', rows or [self.req()]), '请购单.csv')

    def prepare(self, ids=None, key='control-message-1'):
        self.ingest([self.req(), self.req('R2', 'G2')])
        return self.db.prepare_inquiries(ids or ['R1', 'R2'], key)

    def send_all(self):
        jobs = []
        while job := claim_outbox(self.db):
            mark_sent(self.db, job['id'])
            jobs.append(job)
        return jobs

    def quote(self, identifier='Q1', req='R1', supplier='S1', price='0.10'):
        return [identifier, req, supplier, 'M-01', price, '2.5', '2027-04-01', '1', 'CNY', '是', '13%']

    def receive(self, job, rows=None, **overrides):
        args = dict(account='agent@example.com', message_key='mail-1', sender=job['recipient'],
                    subject='Re: ' + job['subject'], received_at=now(),
                    data=csv_bytes('quotations', rows or [self.quote()]), filename='报价.csv',
                    internet_message_id='<reply@example.com>')
        args.update(overrides)
        return self.db.receive_quote(**args)

    def test_original_columns_have_exact_chinese_views(self):
        for table, filename in FILES.items():
            view = Path(filename).stem
            columns = [r[1] for r in self.db.connection.execute(f'PRAGMA table_info("{view}")')]
            self.assertEqual(columns, [label for label, _ in FIELDS[table]])
        self.assertEqual(self.db.check()['integrity'], ['ok'])

    def test_init_excludes_requisitions_and_reimport_is_idempotent(self):
        from openpyxl import Workbook
        folder = Path(self.temp.name) / 'sources'
        folder.mkdir()
        fixtures = {'material_groups': [('G4', 'PLC')], 'suppliers': [('S3', '丙公司', 'third@example.com', '')],
                    'supplier_groups': [('S3', 'G4')], 'statuses': [(90, '已完成')],
                    'requisitions': [self.req()], 'quotations': [], 'orders': []}
        for table, rows in fixtures.items():
            path = folder / FILES[table]
            if path.suffix == '.csv':
                path.write_bytes(csv_bytes(table, rows, 'gb18030'))
            else:
                wb = Workbook()
                wb.active.append([label for label, _ in FIELDS[table]])
                for row in rows:
                    wb.active.append(row)
                wb.save(path)
                wb.close()
        report = self.db.import_sources(folder)
        self.assertEqual(report['supplier_groups']['inserted'], 1)
        self.assertEqual(self.db.summary()['requisitions'], 0)
        second = self.db.import_sources(folder)
        self.assertTrue(second['suppliers']['already_imported'])

    def test_decimal_dates_and_leading_zero_codes_preserved(self):
        self.ingest()
        row = self.db.connection.execute('SELECT * FROM requisitions').fetchone()
        self.assertEqual(row['quantity'], '2.5')
        self.assertEqual(row['material_code'], '06.04.0047')
        self.assertEqual(row['approved_at'], '2026-10-30T16:44:00')
        self.assertEqual(row['required_date'], '2027-05-25')

    def test_duplicate_requisition_noop_and_conflict_rolls_back(self):
        self.ingest()
        self.assertEqual(self.ingest()['inserted'], 0)
        with self.assertRaises(DataError):
            self.ingest([self.req('R2'), self.req(quantity='5')])
        self.assertEqual(self.db.summary()['requisitions'], 1)

    def test_bad_header_formula_or_nonfinite_number_rejected(self):
        with self.assertRaises(DataError):
            self.db.import_requisitions(b'wrong\n1\n', 'bad.csv')
        row = self.req()
        row[8] = '=HYPERLINK("https://example.com")'
        with self.assertRaises(DataError):
            self.ingest([row])
        for quantity in ('NaN', '-2', '0', '1e100'):
            with self.subTest(quantity=quantity), self.assertRaises(DataError):
                self.ingest([self.req(quantity=quantity)])

    def test_foreign_keys_enabled(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute("INSERT INTO supplier_groups VALUES ('missing','G1','{}')")

    def test_missing_requisition_group_reports_exact_key_and_rolls_back(self):
        missing = self.req('R2')
        missing[2] = 'MG013'
        with self.assertRaisesRegex(DataError, r'R2.*物料分组编号不存在：MG013'):
            self.ingest([self.req(), missing])
        self.assertEqual(self.db.summary()['requisitions'], 0)
        self.assertEqual(self.db.summary()['import_files'], 0)

    def test_missing_requisition_status_reports_exact_key(self):
        missing = self.req()
        missing[1] = 999
        with self.assertRaisesRegex(DataError, r'R1.*状态编号不存在：999'):
            self.ingest([missing])

    def test_candidates_and_per_supplier_grouping(self):
        result = self.prepare()
        self.assertEqual(len(self.db.candidates()), 3)
        self.assertEqual(result['supplier_count'], 2)
        inquiry = self.db.connection.execute("SELECT id FROM inquiries WHERE supplier_id='S2'").fetchone()[0]
        self.assertEqual([r[0] for r in self.db.connection.execute('SELECT requisition_id FROM inquiry_items WHERE inquiry_id=?', (inquiry,))], ['R2'])
        job = self.db.connection.execute('SELECT * FROM inquiry_outbox WHERE inquiry_id=?', (inquiry,)).fetchone()
        self.assertIn(inquiry, job['subject'])
        self.assertNotIn('R1', job['attachment_csv'].decode('utf-8-sig'))
        self.assertEqual(job['state'], 'pending')
        self.assertEqual(self.db.connection.execute('SELECT status_id FROM requisitions WHERE id="R1"').fetchone()[0], 10)

    def test_prepare_retry_idempotent_and_key_reuse_rejected(self):
        first = self.prepare()
        retry = self.db.prepare_inquiries(['R2', 'R1'], 'control-message-1')
        self.assertEqual(first['inquiry_ids'], retry['inquiry_ids'])
        with self.assertRaises(DataError):
            self.db.prepare_inquiries(['R1'], 'control-message-1')
        with self.assertRaises(DataError):
            self.db.prepare_inquiries(['R1'], 'different-key')

    def test_no_supplier_rolls_back_whole_batch(self):
        self.ingest([self.req(), self.req('R3', 'G3')])
        with self.assertRaises(DataError):
            self.db.prepare_inquiries(['R1', 'R3'], 'new')
        self.assertEqual(self.db.summary()['inquiries'], 0)

    def test_sent_state_and_recovery_do_not_duplicate_delivery(self):
        self.prepare()
        job = claim_outbox(self.db)
        self.assertEqual(recover_sending(self.db), 1)
        self.assertEqual(recover_sending(self.db), 0)
        self.assertEqual(self.db.connection.execute('SELECT state FROM inquiry_outbox WHERE id=?', (job['id'],)).fetchone()[0], 'uncertain')
        other = claim_outbox(self.db)
        self.assertNotEqual(other['id'], job['id'])
        mark_sent(self.db, other['id'])
        self.assertIsNone(claim_outbox(self.db))

    def test_re_subject_imports_exact_decimal_and_duplicate_mail_is_noop(self):
        self.prepare(['R1'])
        job = self.send_all()[0]
        result = self.receive(job)
        self.assertEqual(result['state'], 'imported')
        self.assertEqual(result['match_method'], 'subject')
        row = self.db.connection.execute('SELECT * FROM quotations').fetchone()
        self.assertEqual((row['price'], row['tax_rate'], row['tax_included']), ('0.1', '0.13', 1))
        self.assertTrue(self.receive(job)['duplicate'])
        self.assertEqual(self.db.summary()['quotations'], 1)
        self.assertEqual(self.db.connection.execute('SELECT status_id FROM requisitions WHERE id="R1"').fetchone()[0], 30)

    def test_reply_headers_work_without_subject_number(self):
        self.prepare(['R1'])
        job = self.send_all()[0]
        result = self.receive(job, subject='Re: 物料询价', in_reply_to=job['internet_message_id'])
        self.assertEqual(result['state'], 'imported')
        self.assertEqual(result['match_method'], 'reply_headers')

    def test_wrong_sender_unmarked_reply_or_old_mail_needs_review(self):
        self.prepare(['R1'])
        job = self.send_all()[0]
        for i, override in enumerate([{'sender': 'other@example.com'}, {'subject': '报价'},
                                      {'received_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()},
                                      {'data': None}]):
            with self.subTest(override=list(override)):
                result = self.receive(job, message_key=f'm{i}', **override)
                self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(self.db.summary()['quotations'], 0)

    def test_unconfirmed_send_does_not_accept_quote(self):
        self.prepare(['R1'])
        job = claim_outbox(self.db)
        self.assertEqual(self.receive(job)['state'], 'needs_review')

    def test_conflicting_or_multiple_rfq_numbers_need_review(self):
        self.prepare()
        first, second = self.send_all()
        result = self.receive(first, in_reply_to=second['internet_message_id'])
        self.assertEqual(result['state'], 'needs_review')
        result = self.receive(first, message_key='another', subject=first['subject'] + second['subject'])
        self.assertEqual(result['state'], 'needs_review')

    def test_invalid_quote_line_rolls_back_other_lines(self):
        self.prepare(['R1'])
        job = self.send_all()[0]
        result = self.receive(job, [self.quote(), self.quote('Q2', req='foreign-request')])
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(self.db.summary()['quotations'], 0)
        self.assertEqual(self.db.summary()['mail_messages'], 1)

    def test_partial_and_all_supplier_quotes_update_status(self):
        self.prepare()
        first, second = sorted(self.send_all(), key=lambda job: job['recipient'])
        self.assertEqual(self.receive(first)['state'], 'imported')
        self.assertEqual(self.db.connection.execute('SELECT state FROM inquiries WHERE id=?', (first['inquiry_id'],)).fetchone()[0], 'awaiting_quote')
        self.assertEqual(self.receive(first, [self.quote('Q2', req='R2')], message_key='m2')['state'], 'imported')
        self.assertEqual(self.db.connection.execute('SELECT status_id FROM requisitions WHERE id="R2"').fetchone()[0], 20)
        self.assertEqual(self.receive(second, [self.quote('Q3', req='R2', supplier='S2')], message_key='m3')['state'], 'imported')
        self.assertEqual(self.db.connection.execute('SELECT status_id FROM requisitions WHERE id="R2"').fetchone()[0], 30)

    def test_automatic_reply_ignored_and_backup_is_consistent(self):
        self.prepare(['R1'])
        job = self.send_all()[0]
        self.assertEqual(self.receive(job, automatic=True)['state'], 'ignored')
        path = Path(self.temp.name) / 'backup.sqlite3'
        self.db.backup(path)
        with Database(path, readonly=True) as copied:
            self.assertEqual(copied.summary(), self.db.summary())
            self.assertEqual(copied.check()['foreign_key_errors'], [])


if __name__ == '__main__':
    unittest.main()
