from __future__ import annotations

import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
import yaml

from procurementdb import Database
from mailagent.config import Config
from tenant_platform import PlatformError, TenantPlatform


class TenantPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = TenantPlatform(Path(self.temp.name) / 'data', probe_mail=lambda config: None,
                                       probe_model=lambda key: None, start_supervisor=False,
                                       launch_agents=False, legacy_config=Path(self.temp.name) / 'missing-config.yml')
        self.platform.setup_model('demo-provider-key-12345')

    def register(self, index):
        return self.platform.register(
            login_email=f'owner{index}@example.com', password=f'demo-password-{index}',
            agent_email=f'agent{index}@example.com', authorization_code=f'app-code-{index}',
            controllers=[f'controller{index}@example.com'],
        )

    def test_registration_creates_isolated_seed_and_hashes_password(self):
        first = self.register(1)
        second = self.register(2)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.platform.authenticate('owner1@example.com', 'demo-password-1')['id'], first['id'])
        with self.assertRaises(PlatformError):
            self.platform.authenticate('owner1@example.com', 'wrong-password')
        with closing(self.platform._connect()) as con:
            record = con.execute('SELECT password_salt,password_hash FROM users WHERE id=?', (first['id'],)).fetchone()
            self.assertNotIn(b'demo-password-1', record['password_hash'])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM users').fetchone()[0], 2)
        for user in (first, second):
            db_path = self.platform.data_root(user['id']) / 'procurement.sqlite3'
            with Database(db_path, readonly=True) as db:
                self.assertEqual(db.summary()['suppliers'], 100)
                self.assertEqual(db.summary()['material_groups'], 32)
                self.assertEqual(db.summary()['supplier_groups'], 133)
                self.assertEqual(db.summary()['requisitions'], 180)
                self.assertEqual(db.summary()['inquiry_outbox'], 0)
                emails = {row[0] for row in db.connection.execute('SELECT DISTINCT email FROM suppliers')}
                self.assertEqual(emails, {'xyinchina@163.com'})
                owner = db.connection.execute("SELECT value FROM runtime_metadata WHERE key='mail_account'").fetchone()[0]
                self.assertEqual(owner, user['agent_email'])

    def test_registration_limit_and_distinct_mailboxes(self):
        self.register(1)
        self.register(2)
        self.register(3)
        with self.assertRaisesRegex(PlatformError, '最多注册 3 个'):
            self.register(4)

    def test_mail_settings_reverify_and_update_all_supplier_records(self):
        user = self.register(1)
        updated = self.platform.change_mail(user['id'], agent_email='agent1-new@example.com',
                                            authorization_code='new-code',
                                            controllers=['controller1-new@example.com'],
                                            supplier_email='rfq-test@example.com')
        self.assertEqual(updated['agent_email'], 'agent1-new@example.com')
        self.assertEqual(updated['controllers'], ['controller1-new@example.com'])
        self.assertEqual(updated['supplier_email'], 'rfq-test@example.com')
        config = self.platform.tenants / user['id'] / 'config.yml'
        self.assertEqual(self.platform.account_id(user['id']), Config.load(config).account_id)
        with Database(self.platform.data_root(user['id']) / 'procurement.sqlite3', readonly=True) as db:
            self.assertEqual({row[0] for row in db.connection.execute('SELECT DISTINCT email FROM suppliers')},
                             {'rfq-test@example.com'})
            self.assertEqual(db.connection.execute('SELECT COUNT(*) FROM data_update_requests').fetchone()[0], 1)

    def test_registration_rejects_same_agent_and_control_address(self):
        with self.assertRaisesRegex(PlatformError, '不能与 Agent'):
            self.platform.register(login_email='owner@example.com', password='a-long-demo-password',
                                   agent_email='agent@example.com', authorization_code='app-code',
                                   controllers=['agent@example.com'])
        self.assertEqual(self.platform.account_count(), 0)

    def test_existing_config_is_preserved_and_original_mailbox_can_be_claimed(self):
        legacy_path = Path(self.temp.name) / 'legacy' / 'config.yml'
        legacy_path.parent.mkdir()
        document = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config.example.yml').read_text(encoding='utf-8'))
        document['mail']['address'] = 'original-agent@example.com'
        document['mail']['authorization_code'] = 'original-app-code'
        document['control']['allowed_senders'] = ['original-controller@example.com']
        document['deepseek']['api_key'] = 'original-provider-key-12345'
        document['deepseek']['model'] = 'original-model-name'
        source_bytes = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode('utf-8')
        legacy_path.write_bytes(source_bytes)
        probed = []
        platform = TenantPlatform(Path(self.temp.name) / 'migrated-data', legacy_config=legacy_path,
                                  probe_mail=lambda config: probed.append(config.address),
                                  probe_model=lambda key: None, start_supervisor=False,
                                  launch_agents=False)
        self.addCleanup(platform.close)
        claimable = platform.claimable_original()
        self.assertEqual(claimable['agent_email'], 'original-agent@example.com')
        self.assertEqual(claimable['controllers'], ['original-controller@example.com'])
        self.assertFalse(claimable['claimed'])
        migrated_config = platform.tenants / claimable['id'] / 'config.yml'
        loaded = Config.load(migrated_config)
        self.assertEqual(loaded.authorization_code, 'original-app-code')
        self.assertEqual(loaded.model, 'original-model-name')
        self.assertEqual(legacy_path.read_bytes(), source_bytes)
        claimed = platform.claim_original('new-local-demo-password')
        self.assertTrue(claimed['claimed'])
        self.assertEqual(probed, ['original-agent@example.com'])
        self.assertEqual(platform.authenticate('original-agent@example.com', 'new-local-demo-password')['id'], claimed['id'])
        with Database(platform.data_root(claimed['id']) / 'procurement.sqlite3', readonly=True) as db:
            self.assertEqual(db.summary()['requisitions'], 180)
            self.assertEqual(db.summary()['inquiries'], 0)
            self.assertEqual(db.summary()['quotations'], 0)


if __name__ == '__main__':
    unittest.main()
