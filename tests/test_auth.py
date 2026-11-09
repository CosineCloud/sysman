import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
import server
from outhman_auth import AuthService, AuthConfig, AuthError
from tests.auth_support import Provider, config, PASSWORD, USER_KEY

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.provider = Provider()
        self.service = AuthService(self.root/'data', self.root/'secrets/key', config(), self.provider.transport)
        self.record, self.cookie = self.service.browser()
        self.input = {'username':'alice', 'password':PASSWORD, 'code':'012345'}
    def tearDown(self): self.temp.cleanup()
    def enroll(self):
        result = self.service.enroll(self.input)
        self.provider.issue()
        return result
    def test_link_then_confirmed_login_encryption_and_rotation(self):
        self.assertTrue(self.enroll()['linked'])
        self.assertFalse(self.service.public_session(self.record)['authenticated'])
        self.assertNotIn(USER_KEY.encode(), self.service.database.read_bytes())
        self.assertNotIn(PASSWORD.encode(), self.service.database.read_bytes())
        output, cookie = self.service.login(self.input, self.record)
        self.assertTrue(output['authenticated']); self.assertNotEqual(cookie, self.cookie)
        self.assertNotIn(USER_KEY, json.dumps(output)); self.assertNotIn('auth_pin', output)
        self.assertEqual([call[0] for call in self.provider.calls], ['enroll','verify','confirm'])
        self.assertEqual(self.provider.calls[1][1]['code'], '012345')
        self.assertEqual(self.provider.calls[1][1]['request_id'], self.provider.calls[2][1]['request_id'])
        record, _ = self.service.browser(cookie)
        self.service.logout(record)
        self.assertFalse(self.service.public_session(self.service.browser(cookie)[0])['authenticated'])
    def test_wrong_password_wrong_code_and_reused_code_fail(self):
        self.enroll()
        for change in ({'password':'bad'}, {'code':'123456'}, {'code':12345}):
            with self.assertRaises(AuthError): self.service.login({**self.input,**change}, self.record)
        self.service.login(self.input, self.record)
        with self.assertRaises(AuthError): self.service.login(self.input, self.service.browser()[0])
    def test_confirmation_must_match_app_user_request_and_boolean(self):
        self.enroll()
        for changes in ({'app_id':'f'*16}, {'user_id':43}, {'request_id':'wrong'}, {'verified':'true'}, {'username':'mallory'}):
            self.provider.override['confirm'] = changes; self.provider.issue()
            with self.assertRaises(AuthError): self.service.login(self.input, self.record)
            self.assertFalse(self.service.public_session(self.record)['authenticated'])
        self.assertEqual(self.service.pending, {})
    def test_expired_pin_and_outage_never_create_session(self):
        self.enroll(); self.provider.override['verify']={'expires_at':time.time()-1}
        with self.assertRaises(AuthError): self.service.login(self.input, self.record)
        self.assertFalse(self.record['user'])
        self.service.transport=lambda *_: (_ for _ in ()).throw(AuthError('Offline',503))
        with self.assertRaises(AuthError): self.service.login(self.input, self.record)
        self.assertFalse(self.record['user'])
    def test_browser_change_during_confirmation_cannot_grant_session(self):
        self.enroll(); original=self.provider.transport
        def transfer(settings, endpoint, data):
            if endpoint=='confirm': self.service.logout(self.record)
            return original(settings,endpoint,data)
        self.service.transport=transfer
        with self.assertRaises(AuthError): self.service.login(self.input,self.record)
        self.assertTrue(all(not item['user'] for item in self.service.sessions.values()))
    def test_storage_restore_requires_matching_key(self):
        self.enroll()
        self.service.key_file.unlink()
        restored=AuthService(self.root/'data',self.root/'secrets/key',config(),self.provider.transport)
        self.assertFalse(restored.storage_ready)
        self.assertFalse(self.service.key_file.exists())
        self.service.key_file.write_bytes(Fernet.generate_key())
        restored=AuthService(self.root/'data',self.root/'secrets/key',config(),self.provider.transport)
        self.assertFalse(restored.storage_ready)
    def test_allowed_users_csrf_and_rate_limit(self):
        with self.assertRaises(AuthError): self.service.enroll({**self.input,'username':'mallory'})
        with self.assertRaises(AuthError): self.service.csrf(self.record,'bad')
        self.service.csrf(self.record,self.record['csrf'])
        for _ in range(10): self.service.limit('127.0.0.1','alice')
        with self.assertRaises(AuthError): self.service.limit('127.0.0.1','alice')
    def test_missing_configuration_is_locked(self):
        service=AuthService(self.root/'missing',self.root/'key',AuthConfig({}))
        self.assertFalse(service.public_session(service.browser()[0])['configured'])
        with self.assertRaises(AuthError): service.enroll(self.input)

class AuthApiTests(unittest.TestCase):
    setUp = AuthTests.setUp
    tearDown = AuthTests.tearDown
    # Reuse only the temporary-service fixture, not the above test cases.
    def request(self,path,method='GET',body=None,csrf=True,cookie=None,origin=None):
        handler=server.Handler.__new__(server.Handler)
        handler.command=method; handler.path=path
        encoded=json.dumps(body or {}).encode()
        handler.headers={'Host':'localhost:8765','Content-Length':str(len(encoded)),'Cookie':'sysman_session='+(cookie or self.cookie)}
        if csrf: handler.headers['X-CSRF-Token']=self.record['csrf']
        if origin: handler.headers['Origin']=origin
        handler.client_address=('127.0.0.1',1234)
        handler.request_version='HTTP/1.1'; handler.requestline=method+' '+path+' HTTP/1.1'
        handler.rfile=io.BytesIO(encoded); handler.wfile=io.BytesIO()
        with patch.object(server,'AUTH',self.service):
            if path.startswith('/api/'): handler.handle_api()
            else: handler.do_GET()
        headers,body=handler.wfile.getvalue().split(b'\r\n\r\n',1)
        return headers.decode(), json.loads(body) if body and path.startswith('/api/') else body
    def test_protected_routes_and_public_health(self):
        for path in ('/api/agents','/api/logs','/api/agents/unknown/logs?download=1'):
            headers,_=self.request(path); self.assertIn('401',headers)
        self.assertIn('303',self.request('/')[0])
        self.assertIn('Location: /login',self.request('/app.js')[0])
        self.assertIn('200',self.request('/api/health')[0])
        self.assertIn('200',self.request('/login')[0])
    def test_csrf_and_cross_origin_login_rejected(self):
        self.assertIn('403',self.request('/api/auth/enroll','POST',self.input,csrf=False)[0])
        self.assertIn('403',self.request('/api/auth/login','POST',self.input,origin='https://other.example')[0])
    def test_all_agent_mutations_require_a_session(self):
        for suffix,method in [('action','POST'),('config','PUT'),('update','POST'),('restore','POST'),('metadata','PUT'),('schedule','PUT'),('backups/test.py','DELETE')]:
            headers,_=self.request('/api/agents/unknown/'+suffix,method,{})
            self.assertIn('401',headers)
    def test_idle_and_absolute_expiry_invalidate_sessions(self):
        self.record['user']={'username':'alice'}
        self.record['idle']=time.time()-1
        replacement,_=self.service.browser(self.cookie)
        self.assertIsNone(replacement['user'])
        replacement['user']={'username':'alice'}; replacement['expires']=time.time()-1
        # Any browser refresh prunes expired records.
        self.service.browser()
        self.assertNotIn(replacement['browser'], self.service.sessions)

    def test_session_cookie_is_http_only_and_auth_data_not_cached(self):
        headers,payload=self.request('/api/auth/session',cookie='invalid')
        self.assertIn('HttpOnly',headers); self.assertIn('SameSite=Strict',headers); self.assertIn('Cache-Control: no-store',headers)
        self.assertNotIn('app_secret',payload)

