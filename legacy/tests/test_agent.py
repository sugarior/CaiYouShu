from contextlib import contextmanager
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import smtplib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from imap_tools import MailMessage

import agent


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = agent.Config(
            username="me@example.com", password="secret", imap_host="imap.example.com",
            smtp_host="smtp.example.com", contacts={"owner": "owner@example.com"}, llm_key="test",
        )
        self.service = agent.Agent(self.config, self.path)

    def row(self, key="test"):
        with self.service.connect() as db:
            return db.execute("SELECT state, error FROM outbox WHERE key=?", (key,)).fetchone()

    def enqueue(self, key="test", subject="subject", body="body"):
        with self.service.connect() as db:
            self.service.enqueue(db, key, "owner", subject, body)

    def fake_mailbox(self, uids):
        box = MagicMock()
        box.client.response.return_value = ("UIDVALIDITY", [b"10"])
        box.uids.return_value = uids

        def messages(**kwargs):
            for uid in kwargs["uid_list"]:
                yield SimpleNamespace(uid=uid, from_="sender@example.com", subject="Deadline",
                                      headers={}, text="Please confirm by Friday", html="")
        box.fetch.side_effect = messages
        context = MagicMock()
        context.__enter__.return_value = box
        return box, context

    def test_dry_run_saves_preview_without_smtp_and_deduplicates(self):
        self.enqueue()
        self.enqueue()
        with patch.object(agent, "smtp_connection") as smtp:
            self.service.flush()
            self.service.flush()
        smtp.assert_not_called()
        previews = list((self.path / "previews").glob("*.eml"))
        self.assertEqual(len(previews), 1)
        message = BytesParser(policy=policy.default).parsebytes(previews[0].read_bytes())
        self.assertEqual(message["To"], "owner@example.com")
        self.assertIsNone(message["Cc"])
        self.assertEqual(self.row()[0], "preview")

    def test_preview_and_live_have_separate_state(self):
        self.enqueue()
        live = agent.Agent(replace(self.config, dry_run=False), self.path)
        with live.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_uncertain_delivery_is_not_automatically_retried(self):
        self.service = agent.Agent(replace(self.config, dry_run=False), self.path)
        self.enqueue()
        connection = MagicMock()
        connection.__enter__.return_value.send_message.side_effect = OSError("connection lost after DATA")
        with patch.object(agent, "smtp_connection", return_value=connection):
            self.service.flush()
            self.service.flush()
        self.assertEqual(self.row()[0], "uncertain")
        connection.__enter__.return_value.send_message.assert_called_once()

    def test_process_restart_recovers_interrupted_send_as_uncertain(self):
        self.enqueue()
        self.service.state("test", "sending")
        self.service = agent.Agent(self.config, self.path)
        self.assertEqual(self.row()[0], "uncertain")

    def test_removed_recipient_is_blocked(self):
        self.enqueue()
        self.service.config = replace(self.config, contacts={"owner": "new-owner@example.com"})
        with patch.object(agent, "smtp_connection") as smtp:
            self.service.flush()
        smtp.assert_not_called()
        self.assertEqual(self.row()[0], "blocked")

    def test_smtp_quit_failure_does_not_undo_accepted_send(self):
        self.service = agent.Agent(replace(self.config, dry_run=False), self.path)
        self.enqueue()
        client = MagicMock()
        client.send_message.return_value = {}
        client.quit.side_effect = OSError("QUIT failed")
        with patch.object(agent.smtplib, "SMTP_SSL", return_value=client):
            self.service.flush()
        self.assertEqual(self.row()[0], "sent")
        client.close.assert_called_once()

    def test_smtp_login_failure_keeps_pending_without_delivery_attempt(self):
        self.service = agent.Agent(replace(self.config, dry_run=False), self.path)
        self.enqueue()
        client = MagicMock()
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad password")
        with patch.object(agent.smtplib, "SMTP_SSL", return_value=client):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                self.service.flush()
        self.assertEqual(self.row()[0], "pending")
        client.send_message.assert_not_called()

    def test_pending_delivery_rechecks_allowlist_after_manual_reconciliation(self):
        self.enqueue()
        self.service.state("test", "uncertain")
        self.service.resolve("test", "retry")
        self.assertEqual(self.row()[0], "pending")
        self.service.state("test", "uncertain")
        self.service.resolve("test", "sent")
        self.assertEqual(self.row()[0], "sent")
        with self.assertRaises(agent.ConfigError):
            self.service.resolve("test", "retry")

    def test_unread_batch_limit_is_applied_after_processed_filter(self):
        self.service.config = replace(self.config, max_per_poll=1)
        with self.service.connect() as db:
            for uid in ("1", "2"):
                key = agent.mail_key(self.config, "10", uid)
                db.execute("INSERT INTO mail VALUES (?, '2026-01-01', '', '', '', 0)", (key,))
        box, context = self.fake_mailbox(["1", "2", "3"])
        with patch.object(agent, "inbox", return_value=context), patch.object(
            agent, "request_json", return_value={"important": True, "summary": "Confirm by Friday"}
        ):
            self.service.scan()
        box.fetch.assert_called_once_with(uid_list=["3"], mark_seen=False, bulk=False)
        self.assertTrue(self.service.processed(agent.mail_key(self.config, "10", "3")))

    def test_repeated_unread_mail_does_not_generate_a_second_alert(self):
        box, context = self.fake_mailbox(["1"])
        with patch.object(agent, "inbox", return_value=context), patch.object(
            agent, "request_json", return_value={"important": True, "summary": "Reply needed"}
        ) as model:
            self.service.scan()
            self.service.scan()
        model.assert_called_once()
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_failed_enqueue_rolls_back_processed_record(self):
        _, context = self.fake_mailbox(["1"])
        with patch.object(agent, "inbox", return_value=context), patch.object(
            agent, "request_json", return_value={"important": True, "summary": "Reply needed"}
        ), patch.object(self.service, "enqueue", side_effect=ValueError("invalid body")):
            with self.assertRaises(RuntimeError):
                self.service.scan()
        self.assertFalse(self.service.processed(agent.mail_key(self.config, "10", "1")))

    def test_mailbox_identity_includes_uidvalidity(self):
        self.assertNotEqual(agent.mail_key(self.config, "10", "1"), agent.mail_key(self.config, "11", "1"))

    def test_same_daily_digest_is_generated_once(self):
        with patch.object(agent, "request_json") as model:
            self.service.digest()
            self.service.digest()
        model.assert_not_called()
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_subject_line_breaks_cannot_inject_headers(self):
        self.enqueue(subject="Title\r\nBcc: outsider@example.com")
        self.service.flush()
        path = next((self.path / "previews").glob("*.eml"))
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        self.assertIsNone(message["Bcc"])
        self.assertEqual(message["To"], "owner@example.com")

    def test_actual_library_decodes_mail_and_provides_lowercase_headers(self):
        raw = (b"From: sender@example.com\r\nSubject: Hello\r\nAuto-Submitted: auto-generated\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nHello\r\n")
        message = MailMessage.from_bytes(raw)
        self.assertIn("auto-submitted", message.headers)
        self.assertEqual(message.headers["auto-submitted"][0], "auto-generated")
        self.assertEqual(agent.plain_text(message, 5), "Hello")


class ModelTests(unittest.TestCase):
    def test_bad_boolean_type_is_rejected_and_model_cannot_supply_recipient(self):
        config = agent.Config(username="me@example.com", password="secret", imap_host="imap.example.com",
                              smtp_host="smtp.example.com", contacts={"owner": "owner@example.com"}, llm_key="test")
        bad_values = [{"important": "false", "summary": "text"},
                      {"important": True, "summary": "text", "to": "outsider@example.com"}]
        for value in bad_values:
            response = httpx.Response(200, request=httpx.Request("POST", "https://api.example.com"), json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]
            })
            client = MagicMock()
            client.__enter__.return_value.post.return_value = response
            with patch.object(agent.httpx, "Client", return_value=client):
                with self.assertRaises(ValueError):
                    agent.request_json(config, "instruction", {}, {"important": bool, "summary": str})


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_json_booleans_and_cli_status_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "MAIL_USERNAME=me@example.com\nMAIL_PASSWORD=secret\n"
                "IMAP_HOST=imap.example.com\nSMTP_HOST=smtp.example.com\n"
                "CONTACTS_JSON='{\"owner\":\"owner@example.com\"}'\n"
                "DRY_RUN=true\nDIGEST_ENABLED=false\n",
                encoding="utf-8",
            )
            with patch.object(agent, "ROOT", root), patch.dict(agent.os.environ, {}, clear=True), \
                 patch("sys.argv", ["agent.py", "status"]), \
                 patch.object(agent, "inbox") as imap, patch.object(agent, "smtp_connection") as smtp, \
                 patch.object(agent, "request_json") as llm:
                self.assertEqual(agent.main(), 0)
                imap.assert_not_called()
                smtp.assert_not_called()
                llm.assert_not_called()
                self.assertTrue((root / "data" / "preview.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
