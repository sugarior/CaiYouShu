from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import smtplib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import Service
from mailagent.ai import DeepSeek, AIError
from mailagent.config import Config, ConfigError
from mailagent.engine import CommandError, Engine
from mailagent.mailio import archive_folders
from mailagent.store import Store, atomic_json, now, read_json


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(path=self.root / 'config.yml', address='agent@163.com', authorization_code='dummy',
                          api_key='dummy-key', allowed_senders=['owner@example.com'], token='secret-control-token-1234567890',
                          storage=self.root / 'data', max_ai_per_cycle=10)
        self.store = Store(self.cfg)
        self.store.state['activated_at'] = '2020-01-01T00:00:00+00:00'
        self.store.save_state()
        self.ai = Mock()
        self.ai.analyze.return_value = {'summary': '摘要', 'matches': []}
        self.engine = Engine(self.cfg, self.store, self.ai)

    def archive(self, body='需要补充发票材料', subject='发票', sender='person@example.com', date=None, folder='INBOX', flags=None):
        message = EmailMessage()
        message['From'] = sender
        message['To'] = self.cfg.address
        message['Subject'] = subject
        message.set_content(body)
        source = {'folder': folder, 'uidvalidity': '1', 'uid': len(self.store.state['queue']) + 1,
                  'received_at': date or now(), 'folder_flags': [], 'flags': flags or []}
        item = self.store.archive(message.as_bytes(), source)
        self.store.save_state()
        return item

    def create_task(self, identity='invoice_alert'):
        self.engine.apply_command({'action': 'create', 'task': {
            'id': identity, 'condition': '发票且需补充材料', 'recipients': ['target@example.com']}},
            'owner@example.com', 'create-' + identity)
        return self.store.tasks()['items'][identity]

    def control(self, payload, sender='owner@example.com', date=None):
        return self.archive(payload, '[AGENT] 管理任务', sender, date=date)

    def jobs(self):
        return [job for _, job in self.store.jobs()]

    def test_config_missing_no_network_and_no_secret_error(self):
        path = self.root / 'config.yml'
        path.write_text('mail: {}\ndeepseek: {}\ncontrol: {}\n', encoding='utf-8')
        cfg = Config.load(path)
        self.assertIn('mail.address', cfg.missing())
        service = Service(path)
        with patch('agent.archive_folders') as sync:
            service.run(once=True)
        sync.assert_not_called()
        self.assertIn('deepseek.api_key', read_json(self.root / 'data/service-status.json')['missing'])
        path.write_text('mail: ["secret-value"', encoding='utf-8')
        with self.assertRaises(ConfigError) as error:
            Config.load(path)
        self.assertNotIn('secret-value', str(error.exception))

    def test_config_reload_while_running(self):
        path = self.root / 'config.yml'
        path.write_text('mail: {}', encoding='utf-8')
        service = Service(path)
        empty = Config(path=path, storage=self.root / 'data')
        count = [0]
        def wait(_):
            count[0] += 1
            if count[0] == 2:
                service.stop.set()
        with patch('agent.Config.load', side_effect=[empty, self.cfg, self.cfg]), \
             patch('agent.archive_folders', return_value={'fetched': 0, 'errors': []}) as sync, \
             patch('agent.Engine') as engine, patch.object(service, 'wait', side_effect=wait):
            engine.return_value.process_pending.return_value = 0
            service.run()
        self.assertEqual(sync.call_count, 1)

    def test_archive_preserves_raw_attachment_and_seen_mail(self):
        message = EmailMessage()
        message['From'] = 'person@example.com'
        message['Subject'] = '中文主题'
        message.set_content('完整正文' * 10000)
        message.add_attachment(b'\x00\x01hello', maintype='application', subtype='octet-stream', filename='../../test.bin')
        raw = message.as_bytes()
        source = {'folder': 'INBOX', 'uidvalidity': '1', 'uid': 1, 'received_at': now(), 'flags': ['\\Seen']}
        item = self.store.archive(raw, source)
        self.assertEqual(base64.b64decode(item['raw_base64']), raw)
        self.assertEqual(item['parsed']['subject'], '中文主题')
        self.assertGreater(len(item['parsed']['text']), self.cfg.max_body_chars)
        self.assertEqual(base64.b64decode(item['parsed']['attachments'][0]['content_base64']), b'\x00\x01hello')
        self.assertFalse((self.root / 'test.bin').exists())

    def test_attached_email_is_attachment_not_main_body(self):
        nested = EmailMessage()
        nested.set_content('附件里的正文')
        message = EmailMessage()
        message.set_content('实际正文')
        message.add_attachment(nested, filename='forwarded.eml')
        source = {'folder': 'INBOX', 'uidvalidity': '1', 'uid': 1, 'received_at': now()}
        parsed = self.store.archive(message.as_bytes(), source)['parsed']
        self.assertIn('实际正文', parsed['text'])
        self.assertNotIn('附件里的正文', parsed['text'])
        self.assertEqual(parsed['attachments'][0]['filename'], 'forwarded.eml')

    def test_official_deepseek_request_and_sanitized_errors(self):
        with patch('mailagent.ai.httpx.Client') as client:
            transport = client.return_value.__enter__.return_value
            transport.post.return_value = httpx.Response(200, json={'choices': [
                {'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})
            self.assertEqual(DeepSeek(self.cfg).request('test json', {'test': True}), {'ok': True})
            call = transport.post.call_args
            self.assertEqual(call.args[0], 'https://api.deepseek.com/chat/completions')
            self.assertEqual(call.kwargs['json']['response_format'], {'type': 'json_object'})
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer dummy-key')
            transport.post.return_value = httpx.Response(401, text='dummy-key secret-response')
            with self.assertRaises(AIError) as error:
                DeepSeek(self.cfg).request('test json', {})
            self.assertNotIn('dummy-key', str(error.exception))
            self.assertNotIn('secret-response', str(error.exception))

    def test_credentials_in_quoted_mail_are_redacted_from_ai_payload(self):
        with patch('mailagent.ai.httpx.Client') as client:
            transport = client.return_value.__enter__.return_value
            transport.post.return_value = httpx.Response(200, json={'choices': [
                {'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})
            DeepSeek(self.cfg).request('json', {'text': self.cfg.token + ' ' + self.cfg.api_key,
                                              'nested': [self.cfg.authorization_code]})
            payload = json.dumps(transport.post.call_args.kwargs['json'])
            for secret in (self.cfg.token, self.cfg.authorization_code, self.cfg.api_key):
                self.assertNotIn(secret, payload)

    def test_imap_connection_failure_still_processes_existing_archive(self):
        item = self.archive()
        service = Service(self.cfg.path)
        factory = lambda cfg, store, **kwargs: Engine(cfg, store, ai=self.ai, **kwargs)
        with patch('agent.Config.load', return_value=self.cfg), \
             patch('agent.archive_folders', side_effect=OSError('offline')), \
             patch('agent.Engine', side_effect=factory):
            service.run(once=True)
        self.assertEqual(self.store.mail(item['id'])['processing']['status'], 'analyzed')

    def fake_box(self, raw, fail_fetch=False):
        box = Mock()
        folder = Mock()
        folder.name, folder.flags = 'INBOX', ()
        box.folder.list.return_value = [folder]
        box.folder.status.return_value = {'UIDVALIDITY': 42}
        def uid(command, *args):
            if command == 'SEARCH':
                return 'OK', [b'1 2']
            if fail_fetch:
                return 'NO', []
            return 'OK', [(f'1 (UID {args[0]} INTERNALDATE "31-Oct-2026 08:00:00 +0800" FLAGS (\\Seen)'.encode(), raw), b')']
        box.client.uid.side_effect = uid
        @contextmanager
        def connection(_):
            yield box
        return box, connection

    def test_sync_all_including_seen_with_persistent_uid_and_dedup(self):
        original = self.archive()
        raw = base64.b64decode(original['raw_base64'])
        box, connect = self.fake_box(raw)
        result = archive_folders(self.cfg, self.store, connect)
        self.assertEqual(result['fetched'], 2)
        self.assertEqual(self.store.state['folders']['INBOX']['last_uid'], 2)
        box.folder.set.assert_called_with('INBOX', readonly=True)
        fetches = [c for c in box.client.uid.call_args_list if c.args[0] == 'FETCH']
        self.assertTrue(all('BODY.PEEK[]' in c.args[2] for c in fetches))
        self.assertTrue(all('UNSEEN' not in str(c) for c in box.client.uid.call_args_list))
        self.assertEqual(self.store.state['archived_count'], 1)
        restarted = Store(self.cfg)
        result = archive_folders(self.cfg, restarted, connect)
        self.assertEqual(result['fetched'], 0)

    def test_failed_fetch_does_not_advance_cursor(self):
        _, connect = self.fake_box(b'body', fail_fetch=True)
        result = archive_folders(self.cfg, self.store, connect)
        self.assertEqual(result['fetched'], 0)
        self.assertTrue(result['errors'])
        self.assertNotIn('INBOX', self.store.state['folders'])

    def test_uidvalidity_reset_causes_resync(self):
        self.store.state['folders']['INBOX'] = {'uidvalidity': 'old', 'last_uid': 999}
        _, connect = self.fake_box(b'From: a@b.com\n\nbody')
        self.assertEqual(archive_folders(self.cfg, self.store, connect)['fetched'], 2)

    def test_archive_queue_recovers_after_interruption(self):
        item = self.archive()
        self.store.state['queue'] = []
        self.store.save_state()
        restarted = Store(self.cfg)
        self.assertIn(item['id'], restarted.state['queue'])
        self.assertEqual(restarted.state['archived_count'], 1)

    def test_unlisted_sender_cannot_control_or_call_ai(self):
        item = self.control('token: "' + self.cfg.token + '"\nrequest: 创建任务', sender='stranger@example.com')
        self.engine.process_one(item)
        self.assertEqual(self.store.mail(item['id'])['processing']['status'], 'control_rejected_sender')
        self.ai.parse_command.assert_not_called()
        self.ai.analyze.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_spoofed_allowlisted_from_without_token_rejected(self):
        item = self.control('token: wrong\naction: list')
        self.engine.process_one(item)
        self.assertEqual(self.store.mail(item['id'])['processing']['status'], 'control_rejected_token')
        self.ai.parse_command.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_multiple_from_headers_rejected(self):
        item = self.control('token: "' + self.cfg.token + '"\naction: list')
        item['parsed']['from_header_count'] = 2
        self.engine.process_one(item)
        self.assertEqual(self.store.mail(item['id'])['processing']['status'], 'control_rejected_sender')

    def test_token_stripped_from_natural_command(self):
        self.ai.parse_command.return_value = {'action': 'list'}
        item = self.control('token: "' + self.cfg.token + '"\nrequest: "列出任务，' + self.cfg.token + '"')
        self.engine.process_one(item)
        self.assertNotIn(self.cfg.token, self.ai.parse_command.call_args.args[0])
        self.assertEqual(self.jobs()[0]['recipients'], ['owner@example.com'])

    def test_structured_control_does_not_need_ai_and_is_idempotent(self):
        item = self.control('token: "' + self.cfg.token + '"\naction: create\ntask:\n  id: test\n  condition: 发票\n  recipients: [target@example.com]')
        self.engine.process_one(item)
        self.engine.process_one(self.store.mail(item['id']))
        self.assertEqual(self.store.tasks()['items']['test']['version'], 1)
        self.assertEqual(len(self.jobs()), 1)
        self.ai.parse_command.assert_not_called()

    def test_control_commands_execute_in_received_order(self):
        self.control('token: "' + self.cfg.token + '"\naction: create\ntask:\n  id: ordered\n  condition: 发票\n  recipients: [target@example.com]',
                     date='2026-01-01T00:00:00+00:00')
        self.control('token: "' + self.cfg.token + '"\naction: pause\ntask_id: ordered',
                     date='2026-01-01T00:00:01+00:00')
        self.engine.process_pending()
        task = self.store.tasks()['items']['ordered']
        self.assertFalse(task['enabled'])
        self.assertEqual(task['version'], 2)

    def test_task_crud_and_command_idempotency(self):
        self.create_task()
        cmd = {'action': 'update', 'task_id': 'invoice_alert', 'task': {'condition': '新的条件'}}
        self.engine.apply_command(cmd, 'owner@example.com', 'update')
        self.engine.apply_command(cmd, 'owner@example.com', 'update')
        self.assertEqual(self.store.tasks()['items']['invoice_alert']['version'], 2)
        for action in ['pause', 'resume', 'delete']:
            self.engine.apply_command({'action': action, 'task_id': 'invoice_alert'}, 'owner@example.com', action)
        self.assertTrue(self.store.tasks()['items']['invoice_alert']['deleted'])
        self.assertEqual(self.engine.apply_command({'action': 'list'}, 'owner@example.com', 'list')['tasks'], [])

    def test_natural_command_cannot_invent_recipient(self):
        with self.assertRaises(CommandError):
            self.engine.apply_command({'action': 'create', 'task': {'id': 'x', 'condition': '发票', 'recipients': ['other@example.com']}},
                                      'owner@example.com', 'fake-ai', request='请给 target@example.com 发提醒')
        self.assertEqual(self.store.tasks()['items'], {})

    def test_historical_control_is_archive_only(self):
        self.store.state['activated_at'] = now()
        item = self.control('token: "' + self.cfg.token + '"\naction: list', date='2019-01-01T00:00:00+00:00')
        self.engine.process_one(item)
        self.assertEqual(self.store.mail(item['id'])['processing']['status'], 'historical_control_archived_only')
        self.assertEqual(self.jobs(), [])

    def test_history_summarized_without_retroactive_task(self):
        self.create_task()
        item = self.archive(date='2019-01-01T00:00:00+00:00')
        self.engine.process_one(item)
        self.assertEqual(self.ai.analyze.call_args.args[1], [])
        self.assertEqual(self.store.mail(item['id'])['analysis']['summary'], '摘要')

    def matched_item(self):
        self.create_task()
        item = self.archive(date=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
        self.ai.analyze.return_value = {'summary': '匹配', 'matches': [{
            'task_id': 'invoice_alert', 'reason': '要求补充发票材料', 'subject': '补充材料提醒', 'body': '请准备材料',
            'recipients': ['attacker@example.com']}]}
        self.engine.process_one(item)
        return item

    def test_ai_cannot_override_recipient_and_cross_folder_dedup(self):
        item = self.matched_item()
        self.assertEqual(self.jobs()[0]['recipients'], ['target@example.com'])
        source = dict(item['sources'][0], folder='Archive', uid=50)
        again = self.store.archive(base64.b64decode(item['raw_base64']), source)
        self.assertEqual(again['processing']['status'], 'analyzed')
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(again['sources']), 2)

    def test_ai_failure_keeps_complete_archive_and_retries(self):
        item = self.archive()
        self.ai.analyze.side_effect = RuntimeError('dummy failure')
        self.engine.process_pending()
        saved = self.store.mail(item['id'])
        self.assertEqual(saved['processing']['status'], 'pending')
        self.assertEqual(saved['processing']['attempts'], 1)
        self.assertTrue(saved['raw_base64'])

    def test_pause_or_whitelist_removal_blocks_pending_send(self):
        self.matched_item()
        self.cfg.allowed_senders = []
        smtp = Mock()
        self.engine.smtp_factory = smtp
        self.engine.deliver()
        smtp.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'blocked')

    def test_task_version_change_blocks_queued_old_message(self):
        self.matched_item()
        self.engine.apply_command({'action': 'pause', 'task_id': 'invoice_alert'}, 'owner@example.com', 'pause')
        self.engine.deliver()
        self.assertEqual(self.jobs()[0]['status'], 'blocked')

    def test_send_success_and_quit_failure_never_duplicate(self):
        self.matched_item()
        smtp = Mock()
        smtp.send_message.return_value = {}
        smtp.quit.side_effect = OSError('QUIT disconnect')
        self.engine.smtp_factory = Mock(return_value=smtp)
        self.engine.deliver()
        self.engine.deliver()
        self.assertEqual(self.jobs()[0]['status'], 'sent')
        smtp.send_message.assert_called_once()
        sent = smtp.send_message.call_args
        self.assertEqual(sent.kwargs['to_addrs'], ['target@example.com'])
        self.assertTrue(sent.args[0]['X-Email-Agent-Instance'])

    def test_uncertain_smtp_never_automatically_retries(self):
        self.matched_item()
        smtp = Mock()
        smtp.send_message.side_effect = TimeoutError('connection lost after data')
        self.engine.smtp_factory = Mock(return_value=smtp)
        self.engine.deliver()
        self.engine.deliver()
        self.assertEqual(self.jobs()[0]['status'], 'uncertain')
        smtp.send_message.assert_called_once()

    def test_crash_during_send_recovers_to_uncertain(self):
        self.matched_item()
        path, job = next(self.store.jobs())
        job['status'] = 'sending'
        atomic_json(path, job)
        Store(self.cfg).recover_outbox()
        self.assertEqual(read_json(path)['status'], 'uncertain')

    def test_partial_recipients_recorded_without_retrying_accepted(self):
        self.matched_item()
        document = self.store.tasks()
        document['items']['invoice_alert']['recipients'].append('second@example.com')
        self.store.save_tasks(document)
        path, job = next(self.store.jobs())
        job['recipients'].append('second@example.com')
        atomic_json(path, job)
        smtp = Mock()
        smtp.send_message.return_value = {'target@example.com': (550, b'denied')}
        self.engine.smtp_factory = Mock(return_value=smtp)
        self.engine.deliver()
        self.assertEqual(self.jobs()[0]['status'], 'sent_partial')
        self.assertEqual(self.jobs()[0]['accepted_recipients'], ['second@example.com'])

    def test_preview_never_sends_after_mode_switch(self):
        self.matched_item()
        self.cfg.dry_run = True
        self.engine.deliver()
        self.cfg.dry_run = False
        smtp = Mock()
        self.engine.smtp_factory = smtp
        self.engine.deliver()
        smtp.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'preview')

    def test_outgoing_and_agent_receipts_do_not_loop(self):
        for folder in ['Sent', '草稿箱']:
            item = self.archive(subject=folder, folder=folder)
            self.engine.process_one(item)
        item = self.archive(subject='agent output')
        item['parsed']['agent_marker'] = 'instance'
        self.engine.process_one(item)
        self.ai.analyze.assert_not_called()


if __name__ == '__main__':
    unittest.main()
