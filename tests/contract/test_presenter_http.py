import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from troubleshooter_app.credentials import make_verifier
from troubleshooter_app.web import create_app

PASSWORD = "synthetic-http-presenter-password"
ORIGIN = "https://demo.example"


class PresenterHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = make_verifier(PASSWORD)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"public_origin": ORIGIN, "release_sha": "a" * 40}))
        self.password = self.root / "presenter.json"
        self.password.write_bytes(self.verifier.to_bytes())
        self.assets = self.root / "dist"
        (self.assets / "assets").mkdir(parents=True)
        (self.assets / "index.html").write_text('<html><title>Presenter</title></html>')
        (self.assets / "assets" / "main.js").write_text('console.log("synthetic");')
        self.mount = self.root / "data"
        self.mount.mkdir()
        self.client = TestClient(self.new_app(), base_url=ORIGIN)
        self.addCleanup(self.client.close)

    def new_app(self, *, mounted=True):
        with patch("troubleshooter_app.web.os.path.ismount", return_value=mounted):
            return create_app(config_path=self.config, verifier_path=self.password,
                              data_path=self.mount, assets_path=self.assets)

    def login(self, password=PASSWORD):
        return self.client.post('/api/login', json={"password": password}, headers={"Origin": ORIGIN})

    def test_login_cookie_session_logout_and_security_headers(self):
        self.assertEqual(self.client.get('/api/session').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"csrf"})
        cookie = response.headers['set-cookie']
        for attribute in ('__Host-demo-session=', 'HttpOnly', 'Secure', 'SameSite=strict', 'Path=/', 'Max-Age=28800'):
            self.assertIn(attribute, cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertEqual(self.client.get('/api/session').json(), response.json())
        status = self.client.get('/api/demo')
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()['ready'])
        self.assertIn('not yet available', status.json()['message'])
        logout = self.client.post('/api/logout', headers={'Origin': ORIGIN, 'X-CSRF-Token': response.json()['csrf']})
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get('/api/demo').status_code, 401)
        for route in ('/', '/assets/main.js', '/healthz', '/missing', '/api/session'):
            response = self.client.get(route)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        for route in ('/docs', '/openapi.json', '/config.json', '/presenter.json'):
            self.assertEqual(self.client.get(route).status_code, 404)

    def test_origin_and_csrf_checks_do_not_trust_forwarded_headers(self):
        for origin in (None, 'https://evil.example', 'null'):
            headers = {'Host': 'evil.example', 'X-Forwarded-Host': 'evil.example'}
            if origin is not None:
                headers['Origin'] = origin
            self.assertEqual(self.client.post('/api/login', json={'password': PASSWORD}, headers=headers).status_code, 403)
        response = self.login()
        for token in (None, 'a' * 43, 'invalid'):
            headers = {'Origin': ORIGIN}
            if token is not None:
                headers['X-CSRF-Token'] = token
            self.assertEqual(self.client.post('/api/logout', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/logout', headers={'X-CSRF-Token': response.json()['csrf']}).status_code, 403)
        self.assertEqual(self.client.get('/api/demo').status_code, 200)

    def test_bad_bodies_and_passwords_are_not_reflected(self):
        for raw in ('{', '[]', '{"password":"private","extra":1}', '{"password":1}',
                    '{"password":"first","password":"second"}', 'x' * 4097):
            result = self.client.post('/api/login', content=raw, headers={'Origin': ORIGIN, 'Content-Type': 'application/json'})
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json(), {'error': 'invalid_request'})
        self.assertEqual(self.client.post('/api/login', content='private', headers={'Origin': ORIGIN}).status_code, 400)
        self.assertEqual(self.login('incorrect-synthetic-password').status_code, 401)
        self.assertEqual(self.login().status_code, 200)

    def test_rate_limit_and_private_failure(self):
        for _ in range(5):
            self.assertEqual(self.login('incorrect-synthetic-password').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '60')
        (self.mount / 'presenter.sqlite3').unlink()
        health = self.client.get('/healthz')
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json(), {'error': 'service_unavailable'})
        self.assertNotIn(str(self.mount), health.text)

    def test_restart_and_password_rotation_use_same_persistent_store(self):
        self.assertEqual(self.login().status_code, 200)
        cookies = self.client.cookies
        with TestClient(self.new_app(), base_url=ORIGIN, cookies=cookies) as restarted:
            self.assertEqual(restarted.get('/api/session').status_code, 200)
        self.password.write_bytes(make_verifier('rotated-synthetic-password').to_bytes())
        with TestClient(self.new_app(), base_url=ORIGIN, cookies=cookies) as rotated:
            self.assertEqual(rotated.get('/api/session').status_code, 401)
            self.assertEqual(rotated.post('/api/login', json={'password': PASSWORD}, headers={'Origin': ORIGIN}).status_code, 401)
            self.assertEqual(rotated.post('/api/login', json={'password': 'rotated-synthetic-password'}, headers={'Origin': ORIGIN}).status_code, 200)

    def test_invalid_verifier_cannot_revoke_an_existing_session_on_startup(self):
        self.assertEqual(self.login().status_code, 200)
        before = (self.mount / 'presenter.sqlite3').read_bytes()
        invalid = {'schema': 1, 'revision': 'b' * 32,
                   'verifier': '$argon2id$v=19$m=65536,t=3,p=1$' + 'B' * 22 + '$' + 'B' * 43}
        self.password.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            self.new_app()
        self.assertEqual((self.mount / 'presenter.sqlite3').read_bytes(), before)
        self.assertEqual(self.client.get('/api/session').status_code, 200)

    def test_changed_shared_cookie_requires_reconciled_csrf_for_logout(self):
        first = self.login().json()['csrf']
        second = self.login().json()['csrf']
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.post('/api/logout', headers={'Origin': ORIGIN, 'X-CSRF-Token': first}).status_code, 403)
        current = self.client.get('/api/session').json()['csrf']
        self.assertEqual(current, second)
        self.assertEqual(self.client.post('/api/logout', headers={'Origin': ORIGIN, 'X-CSRF-Token': current}).status_code, 204)

    def test_startup_requires_mount_configuration_verifier_and_bundle(self):
        with self.assertRaises(ValueError):
            self.new_app(mounted=False)
        self.password.write_bytes(b'private-invalid')
        with self.assertRaises(ValueError):
            self.new_app()
        self.password.write_bytes(self.verifier.to_bytes())
        (self.assets / 'index.html').unlink()
        with self.assertRaises(ValueError):
            self.new_app()

    def test_expired_session_logout_is_unauthenticated(self):
        self.assertEqual(self.client.post('/api/logout', headers={'Origin': ORIGIN}).status_code, 401)
