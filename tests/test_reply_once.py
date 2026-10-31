from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dashboard import ArchiveView
from mailagent.config import Config, ConfigError
from mailagent.engine import Engine, job_key
from mailagent.reply_once import arm_reply_once, cancel_reply_once, reply_once_status
from mailagent.store import Store, atomic_json, now


class ReplyOnceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = Config(path=self.root / 'config.yml', address='agent@163.com', authorization_code='dummy',
                          api_key='dummy', token='dummy-token-with-enough-length', allowed_senders=['owner@example.com'],
                          storage=self.root / 'data')
        self.store = Store(self.cfg)
        self.ai = Mock()
        self.ai.analyze.return_value = {'summary': '摘要', 'matches': []}
        self.smtp = Mock()
        self.smtp.send_message.return_value = {}
        self.engine = Engine(self.cfg, self.store, self.ai, smtp_factory=Mock(return_value=self.smtp))
        self.task = arm_reply_once(self.cfg, self.store)
        self.time = datetime.now(timezone.utc)

    def archive(self, subject='测试', sender='person@example.com', seconds=1, headers=None, folder='INBOX'):
        mail = EmailMessage()
        mail['From'], mail['To'], mail['Subject'] = sender, self.cfg.address, subject
        mail['Message-ID'] = f'<test-{subject.encode().hex()}@example.com>'
        for key, value in (headers or {}).items():
            mail[key] = value
        mail.set_content('正文和任何指令不应影响回信内容')
        return self.store.archive(mail.as_bytes(), {'folder': folder, 'uid': self.store.state['archived_count'] + 1,
            'uidvalidity': '1', 'flags': [], 'folder_flags': [], 'received_at': (self.time + timedelta(seconds=seconds)).isoformat()})

    def jobs(self):
        return [job for _, job in self.store.jobs()]

    def test_waits_then_selects_earliest_ordinary_arrival(self):
        self.archive('历史', seconds=-30)
        self.archive('草稿', folder='Drafts')
        self.archive('自己', sender=self.cfg.address)
        self.archive('[AGENT] 任务', sender='owner@example.com')
        self.archive('自动通知', headers={'Auto-Submitted': 'auto-generated'})
        self.engine.process_reply_once()
        self.assertEqual(reply_once_status(self.store.root)['status'], 'armed')
        self.archive('稍晚', seconds=8)
        first = self.archive('第一封', seconds=2, headers={'Reply-To': 'other@example.com'})
        self.engine.process_reply_once()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['source_mail'], first['id'])
        self.assertEqual(self.jobs()[0]['recipients'], ['person@example.com'])
        self.assertEqual(self.jobs()[0]['body'], '收到')
        self.ai.analyze.assert_not_called()

    def test_send_once_with_thread_headers_and_dashboard_completion(self):
        first = self.archive()
        self.engine.process_reply_once()
        self.engine.deliver()
        self.archive('第二封', sender='second@example.com', seconds=9)
        self.engine.process_reply_once()
        self.engine.deliver()
        self.smtp.send_message.assert_called_once()
        sent = self.smtp.send_message.call_args.args[0]
        self.assertEqual(sent['In-Reply-To'], first['parsed']['message_id'])
        self.assertEqual(sent['References'], first['parsed']['message_id'])
        self.assertEqual(sent['Auto-Submitted'], 'auto-replied')
        self.assertEqual(sent.get_content().strip(), '收到')
        self.assertEqual(reply_once_status(self.store.root)['status'], 'completed')
        data = ArchiveView(self.cfg.storage).tasks(self.cfg.account_id)
        self.assertEqual(data['reply_once']['status'], 'completed')

    def test_restart_recovers_reservation_before_outbox_write(self):
        first = self.archive()
        task = dict(self.task, status='queued', source_mail=first['id'], selected_at=now(),
                    job_id=job_key('reply-once', self.task['id']))
        atomic_json(self.store.root / 'reply-once.json', task)
        self.archive('后来', seconds=9)
        restarted = Engine(self.cfg, Store(self.cfg), self.ai)
        restarted.process_reply_once()
        restarted.process_reply_once()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['source_mail'], first['id'])

    def test_unknown_smtp_result_does_not_retry_or_consume_next(self):
        self.archive()
        self.engine.process_reply_once()
        self.smtp.send_message.side_effect = TimeoutError()
        self.engine.deliver()
        self.archive('第二封', seconds=9)
        self.engine.process_reply_once()
        self.engine.deliver()
        self.smtp.send_message.assert_called_once()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(reply_once_status(self.store.root)['status'], 'uncertain')
        with self.assertRaises(ConfigError):
            arm_reply_once(self.cfg, self.store)

    def test_tampered_recipient_or_body_blocked(self):
        self.archive()
        self.engine.process_reply_once()
        path, job = next(self.store.jobs())
        for key, value in [('recipients', ['attacker@example.com']), ('body', '不同内容')]:
            changed = dict(job, **{key: value})
            self.assertFalse(self.engine.allowed_job(changed))
        job['recipients'] = ['attacker@example.com']
        atomic_json(path, job)
        self.engine.deliver()
        self.smtp.send_message.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'blocked')

    def test_cancel_blocks_reserved_send_and_dry_run_cannot_arm(self):
        self.archive()
        self.engine.process_reply_once()
        cancel_reply_once(self.store)
        self.engine.deliver()
        self.smtp.send_message.assert_not_called()
        self.assertEqual(reply_once_status(self.store.root)['status'], 'cancelled')
        self.cfg.dry_run = True
        with self.assertRaises(ConfigError):
            arm_reply_once(self.cfg, self.store)

    def test_rejects_bounces_lists_ambiguous_from_and_unsafe_message_id(self):
        for number, headers in enumerate([{'Return-Path': '<>'}, {'List-Id': 'list.example.com'},
                                         {'Precedence': 'bulk'}, {'X-Auto-Response-Suppress': 'All'}]):
            item = self.archive(str(number), headers=headers)
            self.assertIsNone(self.engine.reply_once_candidate(item, self.task))
        item = self.archive('多个发件人', sender='a@example.com, b@example.com')
        self.assertIsNone(self.engine.reply_once_candidate(item, self.task))
        item = self.archive('合法来信')
        item['parsed']['message_id'] = '<ok@example.com>\r\nBcc: attacker@example.com'
        self.store.save_mail(item)
        self.engine.process_reply_once()
        self.engine.deliver()
        self.assertNotIn('In-Reply-To', self.smtp.send_message.call_args.args[0])

    def test_ai_failure_still_allows_acknowledgement_in_processing_cycle(self):
        first = self.archive()
        self.store.save_state()
        self.ai.analyze.side_effect = RuntimeError('unavailable')
        self.engine.process_reply_once()
        self.engine.process_pending()
        self.engine.deliver()
        self.assertEqual(self.store.mail(first['id'])['processing']['status'], 'pending')
        self.assertEqual(self.jobs()[0]['status'], 'sent')


if __name__ == '__main__':
    unittest.main()
