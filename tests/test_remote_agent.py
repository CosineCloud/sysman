import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import remote_agent
import server
from sample_agent import main as backup
from sample_agent import runner

class RemoteTests(unittest.TestCase):
    def test_connection_validation(self):
        fields = dict(agent_id='0123abcd', security_key='0123456789abcdef', host='127.0.0.1', port=9101)
        self.assertEqual(remote_agent.connection_fields(fields)['port'], 9101)
        for change in ({'agent_id': 'bad'}, {'security_key': 'bad'}, {'host': 'host/path'}, {'port': 65536}, {'scheme': 'file'}):
            with self.assertRaises(ValueError): remote_agent.connection_fields({**fields, **change})

    def test_curl_credentials_use_stdin_never_arguments_and_no_redirects(self):
        connection = remote_agent.connection_fields(dict(agent_id='0123abcd', security_key='0123456789abcdef', host='127.0.0.1', port=9101))
        result = type('Result', (), {'returncode': 0, 'stdout': '{"ok":true}\n200'})()
        with patch('remote_agent.subprocess.run', return_value=result) as call:
            self.assertTrue(remote_agent.curl_request(connection, 'POST', '/api/agent/update', {'source': 'x="\n"; $(not-a-shell)'} )['ok'])
            args, kwargs = call.call_args
            self.assertNotIn(connection['security_key'], ' '.join(args[0]))
            self.assertIn(connection['security_key'], kwargs['input'])
            self.assertNotIn('--location', args[0])
            self.assertEqual(args[0][0:2], ['curl', '--disable'])

    def test_authentication_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            identifier, key = runner.initialize(directory)
            directory.joinpath('main.py').write_text(server.SAMPLE)
            agent = server.Agent(directory)
            security = json.loads(directory.joinpath('security.json').read_text())
            self.assertNotIn(key, directory.joinpath('security.json').read_text())
            cls = runner.make_handler(agent, security)
            def request(supplied_key, supplied_id=identifier):
                handler = cls.__new__(cls)
                handler.command = 'GET'; handler.path = '/api/agent'
                handler.headers = {'X-Agent-ID': supplied_id, 'Authorization': 'Bearer ' + supplied_key}
                handler.client_address = ('127.0.0.1', 30000)
                handler.connection = type('Socket', (), {'settimeout': lambda *_: None})()
                handler.request_version = 'HTTP/1.1'; handler.requestline = 'GET /api/agent HTTP/1.1'
                handler.wfile = io.BytesIO(); handler.handle_api()
                return handler.wfile.getvalue()
            self.assertIn(b'401 Unauthorized', request('wrong'))
            self.assertIn(b'401 Unauthorized', request(key, '00000000' if identifier != '00000000' else '11111111'))
            response = request(key)
            self.assertIn(b'200 OK', response)
            self.assertNotIn(key.encode(), response)
            self.assertEqual(json.loads(response.split(b'\r\n\r\n')[1])['agent_id'], identifier)

    def test_real_backup_change_detection_and_archive_contents(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            source = directory / 'source'; source.mkdir()
            (source / 'keep.txt').write_text('actual file content')
            (source / 'ignore.tmp').write_text('exclude me')
            ctx = type('Context', (), {'stopping': threading.Event(), 'log': lambda *_: None})()
            config = {**runner.DEFAULT_CONFIG, 'source_directory': str(source), 'backup_directory': str(directory / 'output')}
            with patch.object(backup, '__file__', str(directory / 'main.py')):
                result = backup.backup_once(ctx, config)
                self.assertEqual(result['backup_created'], 1)
                archive = next((directory / 'output').glob('*.zip'))
                with zipfile.ZipFile(archive) as stored:
                    self.assertEqual(stored.read('files/keep.txt'), b'actual file content')
                    self.assertNotIn('files/ignore.tmp', stored.namelist())
                self.assertEqual(backup.backup_once(ctx, config)['backup_created'], 0)
                (source / 'keep.txt').write_text('edited file')
                self.assertEqual(backup.backup_once(ctx, config)['backup_created'], 1)
                self.assertEqual(len(list((directory / 'output').glob('*.zip'))), 2)
                self.assertEqual((source / 'keep.txt').read_text(), 'edited file')

if __name__ == '__main__': unittest.main()
