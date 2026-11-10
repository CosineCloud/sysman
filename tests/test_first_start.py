import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import first_start
import server
from first_start import FirstStart, load_config, settings_path
from outhman_auth import AuthConfig, AuthService, AuthError
from tests.auth_support import APP_ID, APP_SECRET, Provider, PASSWORD

class FirstStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
        self.data=self.root/'data'; self.key=self.root/'key/outhman.key'
        self.service=AuthService(self.data/'auth',self.key,AuthConfig({}))
        self.manager=FirstStart(self.data,self.key,{})
        self.record,self.cookie=self.service.browser()
        self.payload={'setup_token':self.manager.token(self.service),'base_url':'http://127.0.0.1:10070',
                      'app_id':APP_ID,'app_secret':APP_SECRET,'allowed_users':['alice'],'allow_insecure_http':False}
    def tearDown(self): self.temporary.cleanup()
    def test_first_start_encrypted_save_reload_and_closure(self):
        self.assertTrue(self.manager.required(self.service))
        status=self.manager.status(self.service,self.record)
        self.assertNotIn(self.payload['setup_token'],json.dumps(status))
        self.assertNotIn(APP_SECRET,json.dumps(status))
        configured=self.manager.save(self.service,self.payload)
        self.assertTrue(configured.storage_ready)
        self.assertNotIn(APP_SECRET.encode(),settings_path(self.data).read_bytes())
        self.assertFalse(self.manager.required(configured))
        self.assertFalse(self.manager.token_path.exists())
        reload=load_config(self.data,self.key,{'OUTHMAN_BASE_URL':'https://wrong.example','OUTHMAN_APP_SECRET_FILE':'/nonexistent'})
        self.assertEqual(reload.base_url,'http://127.0.0.1:10070')
        self.assertEqual(reload.app_secret,APP_SECRET)
        with self.assertRaises(AuthError): self.manager.save(configured,self.payload)
    def test_token_validation_and_fields_never_enable_anonymous_access(self):
        for fields in ({'setup_token':'wrong'},{'app_id':'bad'},{'allowed_users':[]},{'base_url':'http://10.0.0.2:10070'},{'allow_insecure_http':'yes'}):
            with self.assertRaises(AuthError): self.manager.save(self.service,{**self.payload,**fields})
            self.assertFalse(settings_path(self.data).exists())
        configured=self.manager.save(self.service,{**self.payload,'base_url':'http://10.0.0.2:10070','allow_insecure_http':True})
        self.assertTrue(configured.storage_ready)
        self.assertFalse(configured.public_session(configured.browser()[0])['authenticated'])
    def test_missing_settings_key_does_not_reopen_setup(self):
        configured=self.manager.save(self.service,self.payload)
        self.key.unlink()
        restored=AuthService(self.data/'auth',self.key,load_config(self.data,self.key,{}))
        self.assertFalse(restored.storage_ready)
        self.assertFalse(self.manager.required(restored))
    def test_host_reopen_preserves_encrypted_backup_before_linking(self):
        configured=self.manager.save(self.service,self.payload)
        backup=self.manager.reopen(configured)
        self.assertTrue((self.data/backup).exists())
        self.assertFalse(settings_path(self.data).exists())
        initial=AuthService(self.data/'auth',self.key,load_config(self.data,self.key,{}))
        self.assertTrue(self.manager.required(initial))
        self.assertNotEqual(self.manager.token(initial),self.payload['setup_token'])

    def test_reopening_cannot_replace_an_existing_linked_identity(self):
        configured=self.manager.save(self.service,self.payload)
        provider=Provider(); configured.transport=provider.transport
        configured.enroll({'username':'alice','password':PASSWORD,'code':'012345'})
        with self.assertRaises(AuthError): self.manager.reopen(configured)
        self.assertTrue(settings_path(self.data).exists())

    def request(self,path,method='GET',payload=None,csrf=True):
        handler=server.Handler.__new__(server.Handler); handler.command=method; handler.path=path
        encoded=json.dumps(payload or {}).encode()
        handler.headers={'Host':'localhost:8765','Cookie':'sysman_session='+self.cookie,'Content-Length':str(len(encoded))}
        if csrf: handler.headers['X-CSRF-Token']=self.record['csrf']
        handler.request_version='HTTP/1.1'; handler.requestline=method+' '+path+' HTTP/1.1'
        handler.client_address=('127.0.0.1',10000); handler.rfile=io.BytesIO(encoded); handler.wfile=io.BytesIO()
        with patch.object(server,'AUTH',self.service):
            if path.startswith('/api/'): handler.handle_api()
            else: handler.do_GET()
            configured=server.AUTH
        headers,body=handler.wfile.getvalue().split(b'\r\n\r\n',1)
        return headers.decode(),json.loads(body) if body and path.startswith('/api/') else body,configured
    def test_http_redirect_csrf_token_guard_and_activation(self):
        headers,_,_=self.request('/')
        self.assertIn('Location: /setup',headers)
        self.assertTrue(self.request('/api/auth/session')[1]['setup_required'])
        self.assertIn('200',self.request('/setup')[0])
        self.assertIn('403',self.request('/api/auth/setup','POST',self.payload,csrf=False)[0])
        self.assertIn('403',self.request('/api/auth/setup','POST',{**self.payload,'setup_token':'wrong'})[0])
        headers,result,configured=self.request('/api/auth/setup','POST',self.payload)
        self.assertIn('200',headers); self.assertTrue(result['configured']); self.assertTrue(configured.storage_ready)
        self.assertNotIn(APP_SECRET,json.dumps(result))

if __name__=='__main__': unittest.main()
