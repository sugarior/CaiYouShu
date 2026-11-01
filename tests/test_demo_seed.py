from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from demo_seed import (GROUPS, STATUSES, SUPPLIERS, SEED_VERSION, _legacy_requisitions,
                       initialize_demo_database, load_snapshot, migrate_demo_database,
                       snapshot_from_database)
from procurementdb import Database, DataError
from procurementdb.tables import BUSINESS_TABLES, FIELDS, dump
from procurementdb.updates import apply_updates


def legacy_database(path, supplier_email='old-test@example.com', agent_email='agent@example.com'):
    with Database(path) as db:
        db.initialize()
        with db.transaction():
            for identity, name in GROUPS:
                db.connection.execute('INSERT INTO material_groups VALUES(?,?,?)', (identity, name, dump({'demo': True})))
            for identity, name in STATUSES:
                db.connection.execute('INSERT INTO statuses VALUES(?,?,?)', (identity, name, dump({'demo': True})))
            for identity, company, salutation, groups in SUPPLIERS:
                db.connection.execute('INSERT INTO suppliers VALUES(?,?,?,?,?)',
                                      (identity, company, supplier_email, salutation, dump({'demo': True})))
                for group in groups:
                    db.connection.execute('INSERT INTO supplier_groups VALUES(?,?,?)', (identity, group, dump({'demo': True})))
            db._insert_rows('requisitions', list(_legacy_requisitions()))
            db.connection.execute("INSERT INTO runtime_metadata VALUES('demo_seed','v1')")
            db.connection.execute("INSERT INTO runtime_metadata VALUES('mail_account',?)", (agent_email,))


class DemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'procurement.sqlite3'

    def test_new_database_preserves_every_original_business_field_except_test_email(self):
        initialize_demo_database(self.path, 'demo@example.com', 'agent@example.com')
        snapshot = load_snapshot()
        with Database(self.path, readonly=True) as db:
            for table in BUSINESS_TABLES:
                for source in snapshot['tables'][table]:
                    expected = dict(source)
                    if table == 'suppliers':
                        expected['email'] = 'demo@example.com'
                    keys = ['supplier_id', 'group_id'] if table == 'supplier_groups' else ['id']
                    row = db.connection.execute('SELECT * FROM ' + table + ' WHERE ' +
                                                ' AND '.join(key + '=?' for key in keys),
                                                [source[key] for key in keys]).fetchone()
                    self.assertEqual({key: row[key] for key in expected}, expected)
            self.assertEqual(db.summary()['suppliers'], 100)
            self.assertEqual(db.summary()['material_groups'], 32)
            self.assertEqual(db.summary()['supplier_groups'], 133)
            self.assertEqual(db.summary()['requisitions'], 180)
            for table in ('inquiries', 'inquiry_outbox', 'mail_messages', 'import_files'):
                self.assertEqual(db.summary()[table], 0)
            self.assertEqual(db.check()['foreign_key_errors'], [])
            self.assertEqual(db.check()['integrity'], ['ok'])
            self.assertEqual(db.connection.execute('SELECT count(*) FROM requisitions WHERE source_message_id IS NOT NULL').fetchone()[0], 0)

    def test_original_requisition_ids_can_prepare_without_sending_or_copying_history(self):
        initialize_demo_database(self.path, 'demo@example.com', 'agent@example.com')
        with Database(self.path) as db:
            ids = [row[0] for row in db.connection.execute('SELECT id FROM requisitions WHERE status_id=10 ORDER BY id')]
            self.assertEqual(len(ids), 180)
            prepared = db.prepare_inquiries(ids, 'offline-seed-test')
            self.assertEqual(prepared['supplier_count'], 94)
            self.assertEqual(db.summary()['inquiries'], 94)
            self.assertEqual({row[0] for row in db.connection.execute('SELECT state FROM inquiry_outbox')}, {'pending'})
            self.assertEqual({row[0] for row in db.connection.execute('SELECT recipient FROM inquiry_outbox')}, {'demo@example.com'})

    def test_v1_migration_backs_up_replaces_unmodified_fixtures_and_is_idempotent(self):
        legacy_database(self.path)
        report = migrate_demo_database(self.path, 'old-test@example.com', 'agent@example.com')
        self.assertTrue(report['migrated'])
        self.assertEqual(report['removed_unchanged_legacy']['requisitions'], 8)
        with Database(report['backup'], readonly=True) as backup:
            self.assertEqual(backup.summary()['requisitions'], 8)
            self.assertEqual(backup.summary()['suppliers'], 3)
        with Database(self.path, readonly=True) as db:
            self.assertEqual(db.summary()['suppliers'], 100)
            self.assertEqual(db.summary()['material_groups'], 32)
            self.assertEqual(db.summary()['supplier_groups'], 133)
            self.assertEqual(db.summary()['requisitions'], 180)
            self.assertEqual(db.connection.execute('SELECT name FROM statuses WHERE id=40').fetchone()[0], '待交付')
            self.assertEqual(db.connection.execute("SELECT value FROM runtime_metadata WHERE key='demo_seed'").fetchone()[0], SEED_VERSION)
        self.assertEqual(migrate_demo_database(self.path, 'old-test@example.com', 'agent@example.com'), {'migrated': False})
        self.assertEqual(len(list(self.path.parent.glob('seed-backups/*.sqlite3'))), 1)

    def test_migration_preserves_live_workflows_edits_audit_and_uniform_mail_update(self):
        legacy_database(self.path)
        with Database(self.path) as db:
            inquiry = db.prepare_inquiries(['PR-DEMO-001'], 'existing-inquiry')['inquiry_ids'][0]
            apply_updates(db, account='agent@example.com', controller='owner@example.com',
                          message_key='prior-bulk-edit', table='suppliers',
                          changes=[{'key': {'id': supplier[0]}, 'values': {'email': 'new-test@example.com'}}
                                   for supplier in SUPPLIERS], max_rows=1000)
            db.connection.execute("UPDATE requisitions SET brand='user edited' WHERE id='PR-DEMO-003'")
            db.connection.execute("UPDATE suppliers SET company='user edited' WHERE id='S-DEMO-03'")
            db.connection.execute("UPDATE statuses SET name='user status' WHERE id=90")
        migrate_demo_database(self.path, 'stale-settings@example.com', 'agent@example.com')
        with Database(self.path, readonly=True) as db:
            self.assertEqual(db.connection.execute('SELECT request_key FROM inquiry_batches').fetchone()[0], 'existing-inquiry')
            self.assertEqual(db.connection.execute('SELECT inquiry_id FROM inquiry_outbox').fetchone()[0], inquiry)
            self.assertEqual(db.summary()['inquiry_outbox'], 1)
            self.assertEqual(db.summary()['data_update_requests'], 1)
            self.assertEqual(db.summary()['data_update_changes'], 3)
            self.assertEqual(db.connection.execute("SELECT brand FROM requisitions WHERE id='PR-DEMO-003'").fetchone()[0], 'user edited')
            self.assertEqual(db.connection.execute("SELECT company FROM suppliers WHERE id='S-DEMO-03'").fetchone()[0], 'user edited')
            self.assertEqual(db.connection.execute('SELECT name FROM statuses WHERE id=90').fetchone()[0], 'user status')
            self.assertEqual({r[0] for r in db.connection.execute('SELECT email FROM suppliers')}, {'new-test@example.com'})
            self.assertEqual(db.check()['foreign_key_errors'], [])

    def test_prior_bulk_email_audit_does_not_leave_obsolete_demo_suppliers(self):
        legacy_database(self.path)
        with Database(self.path) as db:
            apply_updates(db, account='agent@example.com', controller='owner@example.com',
                          message_key='email-only-edit', table='suppliers',
                          changes=[{'key': {'id': supplier[0]}, 'values': {'email': 'new-test@example.com'}}
                                   for supplier in SUPPLIERS], max_rows=1000)
        migrate_demo_database(self.path, 'old-test@example.com', 'agent@example.com')
        with Database(self.path, readonly=True) as db:
            self.assertEqual(db.summary()['suppliers'], 100)
            self.assertEqual(db.summary()['material_groups'], 32)
            self.assertEqual(db.summary()['supplier_groups'], 133)
            self.assertEqual(db.summary()['data_update_requests'], 1)
            self.assertEqual(db.summary()['data_update_changes'], 3)
            self.assertEqual({r[0] for r in db.connection.execute('SELECT email FROM suppliers')}, {'new-test@example.com'})

    def test_export_copies_quotes_and_orders_but_excludes_operational_references(self):
        initialize_demo_database(self.path, 'private-supplier@example.com', 'agent@example.com')
        with Database(self.path) as db:
            req = db.connection.execute('SELECT requisition_id,supplier_id FROM v_requisition_suppliers LIMIT 1').fetchone()
            db.prepare_inquiries([req['requisition_id']], 'history-not-for-export')
            quotation = {'id': 'Q-SEED', 'requisition_id': req['requisition_id'], 'supplier_id': req['supplier_id'],
                         'model': 'test-model', 'price': '12.3', 'quantity': '5', 'delivery_date': '2027-01-01',
                         'min_order_quantity': '1', 'currency': 'CNY', 'tax_included': 1, 'tax_rate': '0.13', 'source_json': '{}'}
            db._insert_rows('quotations', [quotation])
            db._insert_rows('orders', [{'id': 'O-SEED', 'requisition_id': req['requisition_id'],
                                       'supplier_id': req['supplier_id'], 'quotation_id': 'Q-SEED', 'model': 'test-model',
                                       'quantity': '5', 'delivery_date': '2027-01-01', 'unit_price': '12.3',
                                       'currency': 'CNY', 'tax_included': 1, 'tax_rate': '0.13', 'source_json': '{}'}])
            db.connection.execute('UPDATE requisitions SET status_id=20')
        snapshot = snapshot_from_database(self.path)
        self.assertEqual(set(snapshot['tables']), set(BUSINESS_TABLES))
        self.assertNotIn('private-supplier@example.com', json.dumps(snapshot))
        waiting = [row['id'] for row in snapshot['tables']['requisitions'] if row['status_id'] == 20]
        self.assertEqual(waiting, [req['requisition_id']])
        self.assertEqual(snapshot['reset_waiting_requisitions'], 179)
        self.assertEqual(set(snapshot['tables']['quotations'][0]), {column for _, column in FIELDS['quotations']})
        snapshot_path = Path(self.temp.name) / 'snapshot.json'
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
        copied = Path(self.temp.name) / 'copied.sqlite3'
        initialize_demo_database(copied, 'demo@example.com', 'other-agent@example.com', snapshot_path=snapshot_path)
        with Database(copied, readonly=True) as db:
            self.assertEqual(db.summary()['quotations'], 1)
            self.assertEqual(db.summary()['orders'], 1)
            self.assertEqual(db.summary()['inquiries'], 0)
            self.assertEqual(db.summary()['inquiry_outbox'], 0)
            self.assertEqual(db.summary()['mail_messages'], 0)
            self.assertEqual(db.check()['foreign_key_errors'], [])

    def test_platform_restart_upgrades_seed_and_keeps_login_and_config(self):
        from tenant_platform import TenantPlatform
        root = Path(self.temp.name) / 'platform'
        options = dict(legacy_config=root / 'absent.yml', probe_mail=lambda config: None,
                       probe_model=lambda key: None, start_supervisor=False, launch_agents=False)
        platform = TenantPlatform(root, **options)
        platform.setup_model('demo-provider-key-12345')
        user = platform.register(login_email='owner@example.com', password='long-demo-password',
                                 agent_email='agent@example.com', authorization_code='preserved-auth-code',
                                 controllers=['controller@example.com'])
        path = platform.data_root(user['id']) / 'procurement.sqlite3'
        config = platform.tenants / user['id'] / 'config.yml'
        original_config = config.read_bytes()
        platform.close()
        path.unlink()
        legacy_database(path, 'xyinchina@163.com')
        restarted = TenantPlatform(root, **options)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.authenticate('owner@example.com', 'long-demo-password')['id'], user['id'])
        self.assertEqual(config.read_bytes(), original_config)
        with Database(path, readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'], 180)
            self.assertEqual(db.summary()['inquiry_outbox'], 0)

    def test_snapshot_tamper_is_rejected(self):
        snapshot = load_snapshot()
        snapshot['tables']['material_groups'][0]['name'] = 'changed'
        path = Path(self.temp.name) / 'changed.json'
        path.write_text(json.dumps(snapshot), encoding='utf-8')
        with self.assertRaisesRegex(DataError, '校验失败'):
            load_snapshot(path)


if __name__ == '__main__':
    unittest.main()
