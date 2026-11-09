"""Optional live curl integration check: python3 tests/remote_smoke.py."""
import json
from http.cookies import SimpleCookie
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from remote_agent import curl_request
from sample_agent import runner
from outhman_auth import AuthService
from tests.auth_support import Provider, config as auth_config, PASSWORD

SESSION_COOKIE = None
CSRF_TOKEN = None


def local_call(port, method, path, data=None):
    global SESSION_COOKIE, CSRF_TOKEN
    config = f'url = "http://127.0.0.1:{port}{path}"\nrequest = "{method}"\n'
    if SESSION_COOKIE:
        config += 'header = ' + json.dumps('Cookie: '+SESSION_COOKIE) + '\n'
    if CSRF_TOKEN and method != 'GET':
        config += 'header = ' + json.dumps('X-CSRF-Token: '+CSRF_TOKEN) + '\n'
    if data is not None:
        config += 'header = "Content-Type: application/json"\ndata-binary = ' + json.dumps(json.dumps(data)) + '\n'
    result = subprocess.run(['curl', '--disable', '--silent', '--show-error', '--fail', '--noproxy', '*', '--max-time', '20', '--dump-header', '-', '--config', '-'], input=config, text=True, capture_output=True, check=True)
    headers, body = result.stdout.split('\n\n',1)
    for line in headers.splitlines():
        if line.lower().startswith('set-cookie:'):
            SESSION_COOKIE = line.split(':',1)[1].strip().split(';',1)[0]
    payload = json.loads(body)
    if 'csrf_token' in payload: CSRF_TOKEN = payload['csrf_token']
    return payload


def wait_for(predicate):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for real backup metrics')


def run():
    with tempfile.TemporaryDirectory() as root:
        directory = Path(root) / 'worker'
        identifier, key = runner.initialize(directory)
        original = (server.ROOT / 'sample_agent' / 'main.py').read_text()
        (directory / 'main.py').write_text(original)
        worker = server.Agent(directory)
        security = json.loads((directory / 'security.json').read_text())
        agent_http = ThreadingHTTPServer(('127.0.0.1', 0), runner.make_handler(worker, security))
        sysman_http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        for api in (agent_http, sysman_http): threading.Thread(target=api.serve_forever, daemon=True).start()
        provider = Provider()
        provider_http = ThreadingHTTPServer(('127.0.0.1',0), provider.handler())
        threading.Thread(target=provider_http.serve_forever,daemon=True).start()
        old_auth = server.AUTH
        server.AUTH = AuthService(Path(root)/'accounts',Path(root)/'secrets/key',auth_config(f'http://127.0.0.1:{provider_http.server_port}'))
        old_data, old_agents = server.DATA, server.AGENTS
        server.DATA, server.AGENTS = Path(root) / 'platform', {}
        server.DATA.mkdir()
        try:
            global SESSION_COOKIE, CSRF_TOKEN
            SESSION_COOKIE = CSRF_TOKEN = None
            session = local_call(sysman_http.server_port,'GET','/api/auth/session')
            assert not session['authenticated']
            credentials = {'username':'alice','password':PASSWORD,'code':'012345'}
            linked = local_call(sysman_http.server_port,'POST','/api/auth/enroll',credentials)
            assert linked['linked']
            provider.issue()
            logged_in = local_call(sysman_http.server_port,'POST','/api/auth/login',credentials)
            assert logged_in['authenticated']
            assert 'user_key' not in json.dumps(logged_in) and 'auth_pin' not in json.dumps(logged_in)
            connection = dict(agent_id=identifier, security_key=key, host='127.0.0.1', port=agent_http.server_port, scheme='http')
            try:
                curl_request({**connection, 'security_key': '0'*16})
                raise AssertionError('Invalid key accepted')
            except ValueError: pass
            registered = local_call(sysman_http.server_port, 'POST', '/api/agents', {**connection, 'name': 'Smoke backup', 'type': 'remote'})
            assert key not in json.dumps(registered)
            prefix = '/api/agents/' + registered['id']
            appearance = local_call(sysman_http.server_port, 'PUT', prefix+'/metadata', {'icon': 'folder'})
            assert appearance['icon'] == 'folder'
            assert json.loads((server.DATA / registered['id'] / 'agent.json').read_text())['icon'] == 'folder'
            config = {**runner.DEFAULT_CONFIG, 'interval': 1}
            local_call(sysman_http.server_port, 'PUT', prefix+'/config', config)
            local_call(sysman_http.server_port, 'POST', prefix+'/action', {'action': 'start'})
            wait_for(lambda: worker.metrics.get('backups_created', 0) >= 1)
            archives = list((directory / 'backup_output').glob('*.zip'))
            assert archives, 'No actual ZIP archive created'
            local_call(sysman_http.server_port, 'POST', prefix+'/action', {'action': 'pause'})
            assert worker.status == 'paused'
            local_call(sysman_http.server_port, 'POST', prefix+'/action', {'action': 'resume'})
            assert worker.status == 'running'
            updated = local_call(sysman_http.server_port, 'POST', prefix+'/update', {'source': original+'\n# smoke update\n'})
            backup = updated['backups'][0]['name']
            local_call(sysman_http.server_port, 'POST', prefix+'/restore', {'backup': backup})
            assert (directory / 'main.py').read_text() == original
            local_call(sysman_http.server_port, 'POST', prefix+'/action', {'action': 'restart'})
            local_call(sysman_http.server_port, 'POST', prefix+'/action', {'action': 'stop'})
            assert worker.status == 'stopped'
            logs = local_call(sysman_http.server_port, 'GET', '/api/logs')['logs']
            assert any('Created backup-' in entry['message'] for entry in logs)
            local_call(sysman_http.server_port, 'DELETE', prefix+'/backups/'+backup)
            assert not (directory / 'backups' / backup).exists()
            from scheduling import DEFAULT_SCHEDULE
            service = server.AGENTS[registered['id']].schedule_service
            service.stop()  # Deterministic clock for this test instead of the background loop.
            scheduled = local_call(sysman_http.server_port, 'PUT', prefix+'/schedule', {**DEFAULT_SCHEDULE, 'enabled': True, 'timezone': 'UTC', 'weekdays': [0]})
            assert scheduled['schedule']['enabled']
            service.tick(datetime(2026,11,9,12,tzinfo=timezone.utc))
            assert worker.status == 'running', 'Remote schedule did not start the worker through curl'
            service.tick(datetime(2026,11,9,18,tzinfo=timezone.utc))
            assert worker.status == 'stopped', 'Remote schedule did not stop the worker through curl'
            assert service.state['last_action'] == 'stop'
            local_call(sysman_http.server_port, 'PUT', prefix+'/schedule', {**DEFAULT_SCHEDULE, 'enabled': False})
            logged_out = local_call(sysman_http.server_port,'POST','/api/auth/logout',{})
            assert not logged_out['authenticated']
            print('PASS: OuthMan mock-provider enrollment, verify/confirm, CSRF/session/logout; curl authentication, registration, config, actual ZIP backup, pause/resume, update, restore, restart/stop, combined logs, backup deletion, saved icon and scheduled remote start/stop; no credentials exposed.')
        finally:
            for agent in server.AGENTS.values():
                if hasattr(agent, 'schedule_service'): agent.schedule_service.stop()
                agent.stop()
            worker.stop()
            for api in (agent_http, sysman_http, provider_http): api.shutdown(); api.server_close()
            server.DATA, server.AGENTS, server.AUTH = old_data, old_agents, old_auth

if __name__ == '__main__': run()
