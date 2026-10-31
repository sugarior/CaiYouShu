from __future__ import annotations

from contextlib import closing
import http.client
import json
import tempfile
from pathlib import Path
import threading
import unittest

from multiuser import PlatformServer
from tenant_platform import TenantPlatform


class MultiuserHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = TenantPlatform(Path(self.temp.name) / 'data', probe_mail=lambda config: None,
                                       probe_model=lambda key: None, start_supervisor=False,
                                       launch_agents=False, legacy_config=Path(self.temp.name) / 'missing-config.yml')
        self.platform.setup_model('demo-provider-key-12345')
        self.first = self.platform.register(login_email='one@example.com', password='first-demo-password',
                                            agent_email='agent-one@example.com', authorization_code='secret-one',
                                            controllers=['controller-one@example.com'])
        self.second = self.platform.register(login_email='two@example.com', password='second-demo-password',
                                             agent_email='agent-two@example.com', authorization_code='secret-two',
                                             controllers=['controller-two@example.com'])
        self.server = PlatformServer(('127.0.0.1', 0), self.platform,
                                     public_origin='https://81.69.224.197')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.platform.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.cookie = ''
        self.cookie_header = ''

    def request(self, path, method='GET', payload=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        headers = {}
        body = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload).encode()
        if self.cookie:
            headers['Cookie'] = self.cookie
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            data = response.read()
            cookie = response.getheader('Set-Cookie')
            if cookie:
                self.cookie_header = cookie
                self.cookie = cookie.split(';', 1)[0]
            return response.status, json.loads(data) if data else {}
        finally:
            client.close()

    def test_login_gates_mail_api_and_each_tenant_has_its_own_root(self):
        self.assertEqual(self.request('/api/inbox')[0], 401)
        status, result = self.request('/api/login', method='POST', payload={
            'login_email': 'one@example.com', 'password': 'first-demo-password'})
        self.assertEqual(status, 200)
        self.assertEqual(result['user']['id'], self.first['id'])
        status, result = self.request('/api/inbox')
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 0)
        with closing(self.platform._connect()) as con:
            second_agent_id = self.platform.account_id(self.second['id'])
            row = con.execute('SELECT id FROM users WHERE id=?', (self.first['id'],)).fetchone()
            self.assertIsNotNone(row)
        self.assertFalse((self.platform.data_root(self.first['id']) / 'accounts' / second_agent_id).exists())

    def test_http_rejects_foreign_host_header(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request('GET', '/api/bootstrap', headers={'Host': 'attacker.invalid'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()

    def test_proxy_allows_only_configured_public_origin_and_uses_secure_cookie(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request('GET', '/api/bootstrap', headers={
                'Host': f'127.0.0.1:{self.server.server_port}',
                'Origin': 'https://81.69.224.197'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            connection.close()

        status, _ = self.request('/api/login', method='POST', payload={
            'login_email': 'one@example.com', 'password': 'first-demo-password'})
        self.assertEqual(status, 200)
        self.assertIn('; Secure', self.cookie_header)

        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request('GET', '/api/bootstrap', headers={
                'Host': f'127.0.0.1:{self.server.server_port}',
                'Origin': 'https://attacker.invalid'})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
