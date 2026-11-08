import json
import tempfile
import time
import unittest
from pathlib import Path
import server

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = server.DATA
        server.DATA = Path(self.temp.name)
        self.agent = server.create_agent('Test agent')

    def tearDown(self):
        if hasattr(self.agent, 'schedule_service'): self.agent.schedule_service.stop()
        self.agent.stop()
        server.AGENTS.pop(self.agent.meta['id'], None)
        server.DATA = self.old_data
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.time() + 4
        while time.time() < deadline:
            if predicate(): return
            time.sleep(.05)
        self.fail('Timed out waiting for worker')

    def test_lifecycle_metrics_and_backup_restore(self):
        original = (self.agent.directory / 'main.py').read_text()
        self.agent.start()
        self.wait_for(lambda: self.agent.metrics.get('cycles', 0) >= 1)
        snapshot = self.agent.snapshot()
        self.assertEqual(snapshot['runtime']['pid'], self.agent.process.pid)
        self.assertIsNotNone(snapshot['runtime']['last_metrics_at'])
        self.assertEqual(snapshot['config_schema']['properties']['logging_enabled']['type'], 'boolean')
        self.agent.action('pause')
        self.assertEqual(self.agent.status, 'paused')
        self.agent.action('resume')
        self.assertEqual(self.agent.status, 'running')
        self.agent.update('def run(ctx):\n    ctx.metrics(updated=1)\n    ctx.wait(60)\n')
        self.wait_for(lambda: self.agent.metrics.get('updated') == 1)
        backups = list((self.agent.directory / 'backups').glob('*.py'))
        self.assertEqual(backups[0].read_text(), original)
        self.agent.update(backups[0].read_text())
        self.assertEqual(len(list((self.agent.directory / 'backups').glob('*.py'))), 2)
        self.agent.stop()
        self.assertEqual(self.agent.status, 'stopped')
        self.assertTrue(json.loads((self.agent.directory / 'logs.json').read_text()))

    def test_invalid_update_preserves_source(self):
        old = (self.agent.directory / 'main.py').read_text()
        with self.assertRaises(SyntaxError): self.agent.update('def !')
        with self.assertRaises(ValueError): self.agent.update('x = 1')
        self.assertEqual((self.agent.directory / 'main.py').read_text(), old)
        self.assertEqual(self.agent.snapshot()['backups'], [])

    def test_runtime_failure_reported(self):
        self.agent.update('def run(ctx):\n    raise RuntimeError("failure test")\n')
        self.agent.start()
        self.wait_for(lambda: self.agent.status == 'error')
        self.assertTrue(self.agent.error)


class FormSchemaTests(unittest.TestCase):
    def test_native_form_schema_validation(self):
        schema = {'type': 'object', 'required': ['interval'], 'properties': {
            'interval': {'type': 'number', 'minimum': 1},
            'enabled': {'type': 'boolean'},
            'mode': {'type': 'string', 'enum': ['full', 'incremental']},
            'folders': {'type': 'array', 'items': {'type': 'string'}}}}
        server.validate_config({'interval': 5, 'enabled': True, 'mode': 'full', 'folders': ['a', 'b']}, schema)
        for config in ({'interval': 0}, {'interval': True}, {'interval': 5, 'enabled': 'yes'},
                       {'interval': 5, 'mode': 'bad'}, {'interval': 5, 'folders': [1]}, {}):
            with self.assertRaises(ValueError): server.validate_config(config, schema)

    def test_json_mode_obeys_text_and_list_limits(self):
        for value, schema in [('', {'type': 'string', 'minLength': 1}), ('long', {'maxLength': 2}), ([], {'minItems': 1}), ([1, 2], {'maxItems': 1})]:
            with self.assertRaises(ValueError): server.validate_config(value, schema)

    def test_metadata_does_not_execute_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('agent.json').write_text('{"id":"schema-test","name":"Schema test"}')
            root.joinpath('logs.json').write_text('[]')
            root.joinpath('main.py').write_text('raise RuntimeError("must not import")\nCONFIG_SCHEMA = {"properties": {"enabled": {"type": "boolean"}}}\n')
            agent = server.Agent(root)
            self.assertEqual(agent.config_schema()['properties']['enabled']['type'], 'boolean')
            self.assertEqual(agent.runtime()['pid'], None)

class LogsApiTests(unittest.TestCase):
    setUp = SupervisorTests.setUp
    tearDown = SupervisorTests.tearDown

    def request(self, path):
        import io
        handler = server.Handler.__new__(server.Handler)
        handler.command = 'GET'
        handler.path = path
        handler.headers = {'Host': 'localhost:8765'}
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'GET ' + path + ' HTTP/1.1'
        handler.wfile = io.BytesIO()
        handler.handle_api()
        headers, body = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return headers.decode(), json.loads(body)

    def test_full_retained_logs_download_and_global_attribution(self):
        self.agent.logs = [{'time': server.now(), 'level': 'info', 'message': f'Entry {n}'} for n in range(250)]
        headers, result = self.request(f'/api/agents/{self.agent.meta["id"]}/logs?download=1')
        self.assertIn('Content-Disposition: attachment;', headers)
        self.assertEqual(len(result['logs']), 250)
        self.assertEqual(len(self.agent.snapshot()['logs']), 200)
        headers, result = self.request('/api/logs?download=1')
        self.assertIn('sysman-logs.json', headers)
        self.assertEqual(len(result['logs']), 250)
        self.assertEqual(result['logs'][0]['agent_name'], 'Test agent')
        self.assertEqual(result['logs'][0]['agent_id'], self.agent.meta['id'])

if __name__ == '__main__': unittest.main()
