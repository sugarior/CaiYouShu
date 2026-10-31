from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import ArchiveView, DashboardServer
from mailagent.store import atomic_json, now

ACCOUNT = "a" * 24


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.account = self.data / "accounts" / ACCOUNT
        atomic_json(self.account / "state.json", {"account_address": "agent@163.com"})
        atomic_json(self.account / "tasks.json", {"items": {}})
        self.view = ArchiveView(self.data)

    def mail(self, number=1, **changes):
        identity = f"{number:064x}"
        item = {"id": identity, "received_at": f"2026-10-31T11:{number % 60:02d}:00+00:00", "archived_at": now(),
                "raw_base64": base64.b64encode(b"From: demo@example.com\r\n\r\nHello").decode(),
                "sources": [{"folder": "INBOX", "uid": number, "flags": []}],
                "parsed": {"subject": "材料确认", "from": ["demo@example.com"], "to": ["agent@163.com"],
                           "text": "请确认项目发票材料。", "html": '<script>alert("unsafe")</script>',
                           "attachments": [{"filename": "../../报告\r\n.txt", "content_type": "text/html", "size": 5,
                                            "sha256": "b" * 64, "content_base64": "aGVsbG8="}]},
                "processing": {"status": "analyzed"}, "analysis": {"summary": "一封需要确认材料的来信", "matches": []}}
        item.update(changes)
        atomic_json(self.account / "mail" / f"{identity}.json", item)
        return item

    def start_server(self):
        self.server = DashboardServer(("127.0.0.1", 0), self.view)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path, headers=None, method="GET"):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            client.request(method, path, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_empty_archive_and_waiting_configuration(self):
        atomic_json(self.data / "service-status.json", {"state": "waiting_configuration", "heartbeat_at": now(),
                    "missing": ["deepseek.api_key"], "private": "never-return-this"})
        overview = self.view.overview()
        self.assertTrue(overview["service"]["online"])
        self.assertEqual(overview["service"]["state"], "waiting_configuration")
        self.assertNotIn("private", json.dumps(overview))
        self.assertEqual(self.view.listing(ACCOUNT)["total"], 0)
        self.assertEqual(self.view.tasks(ACCOUNT)["total"], 0)
        self.assertEqual(self.view.outbox(ACCOUNT)["total"], 0)

    def test_no_account_and_stale_heartbeat(self):
        empty = ArchiveView(self.data / "empty")
        self.assertEqual(empty.overview()["accounts"], [])
        atomic_json(self.data / "service-status.json", {"state": "running", "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()})
        self.assertFalse(self.view.overview()["service"]["online"])

    def test_search_filters_and_pagination(self):
        for number in range(1, 34):
            self.mail(number)
        first = self.view.listing(ACCOUNT)
        self.assertEqual((first["total"], len(first["items"]), first["pages"]), (33, 30, 2))
        second = self.view.listing(ACCOUNT, page=9)
        self.assertEqual((second["page"], len(second["items"])), (2, 3))
        self.assertEqual(self.view.listing(ACCOUNT, query="DEMO@EXAMPLE")["total"], 33)
        self.assertEqual(self.view.listing(ACCOUNT, query="项目发票")["total"], 33)
        self.assertEqual(self.view.listing(ACCOUNT, query="需要确认材料")["total"], 33)
        self.assertEqual(self.view.listing(ACCOUNT, folder="已发送")["total"], 0)
        self.assertEqual(self.view.listing(ACCOUNT, query="不存在")["total"], 0)

    def test_cache_observes_archive_updates_and_deletions(self):
        item = self.mail()
        self.assertEqual(self.view.listing(ACCOUNT)["counts"]["matched"], 0)
        item["analysis"]["matches"] = [{"task_id": "demo"}]
        atomic_json(self.account / "mail" / f"{item['id']}.json", item)
        self.assertEqual(self.view.listing(ACCOUNT)["counts"]["matched"], 1)
        (self.account / "mail" / f"{item['id']}.json").unlink()
        self.assertEqual(self.view.listing(ACCOUNT)["total"], 0)

    def test_kind_filters(self):
        incoming = self.mail(1)
        parsed = dict(incoming["parsed"], **{"from": ["agent@163.com"]})
        self.mail(2, parsed=parsed)
        self.mail(3, processing={"status": "control_processed"})
        self.mail(4, parsed=dict(parsed, agent_marker="instance"))
        for kind in ("incoming", "outgoing", "control", "agent"):
            result = self.view.listing(ACCOUNT, kind=kind)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["kind"], kind)

    def test_detail_omits_embedded_binary_and_html(self):
        item = self.mail()
        result = self.view.detail(ACCOUNT, item["id"])
        self.assertEqual(result["parsed"]["text"], item["parsed"]["text"])
        self.assertNotIn("base64", json.dumps(result))
        self.assertNotIn("html", result["parsed"])
        self.assertEqual(result["parsed"]["attachments"][0]["size"], 5)

    def test_tasks_hide_deleted_and_outbox_sort(self):
        atomic_json(self.account / "tasks.json", {"items": {"one": {"id": "one", "enabled": True}, "two": {"id": "two", "deleted": True}}})
        self.assertEqual(self.view.tasks(ACCOUNT)["enabled"], 1)
        self.assertEqual(self.view.tasks(ACCOUNT)["total"], 1)
        for n in (1, 2):
            atomic_json(self.account / "outbox" / f"{n:064x}.json", {"id": str(n), "created_at": f"2026-10-31T12:0{n}:00+00:00"})
        self.assertEqual([v["id"] for v in self.view.outbox(ACCOUNT)["items"]], ["2", "1"])

    def test_http_static_and_api_do_not_open_config(self):
        item = self.mail()
        self.start_server()
        original = Path.open
        opened = []
        def tracked(path, *args, **kwargs):
            opened.append(path.name)
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", tracked):
            for route in ("/", "/app.js", "/styles.css", "/favicon.svg", "/api/overview", f"/api/accounts/{ACCOUNT}/mail", f"/api/accounts/{ACCOUNT}/mail/{item['id']}"):
                status, headers, body = self.request(route)
                self.assertEqual(status, 200, route)
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("config.yml", opened)
        self.assertNotIn("config.example.yml", opened)

    def test_http_attachment_and_raw_download(self):
        item = self.mail()
        self.start_server()
        base = f"/api/accounts/{ACCOUNT}/mail/{item['id']}"
        status, headers, body = self.request(base + "/attachments/0")
        self.assertEqual((status, body), (200, b"hello"))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertNotIn("..", headers["Content-Disposition"])
        self.assertNotIn("%0D", headers["Content-Disposition"])
        self.assertEqual(self.request(base + "/attachments/1")[0], 404)
        self.assertEqual(self.request(base + "/raw")[2], base64.b64decode(item["raw_base64"]))

    def test_host_origin_and_cross_site_are_rejected(self):
        self.start_server()
        port = self.server.server_port
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": f"http://127.0.0.1:{port + 1}"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("/api/overview", headers)[0], 403)
        self.assertEqual(self.request("/api/overview", {"Origin": f"http://127.0.0.1:{port}"})[0], 200)

    def test_paths_inputs_and_writes_are_rejected(self):
        self.start_server()
        for path in ("/config.yml", "/../config.yml", "/%2e%2e/config.yml", f"/api/accounts/{ACCOUNT}/mail/../../config.yml", f"/api/accounts/{'b' * 24}/mail"):
            self.assertEqual(self.request(path)[0], 404, path)
        for query in ("kind=invalid", "page=bad", "q=" + "x" * 257):
            self.assertEqual(self.request(f"/api/accounts/{ACCOUNT}/mail?{query}")[0], 400)
        self.assertEqual(self.request("/api/overview", method="POST")[0], 501)
        for identity in ("../config", "A" * 24, "x"):
            with self.assertRaises(LookupError): self.view.account_root(identity)


if __name__ == "__main__":
    unittest.main()
