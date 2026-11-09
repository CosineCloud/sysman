"""SysMan: local process supervisor and dependency-free HTTP API."""
import ast
from http.cookies import SimpleCookie
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from remote_agent import RemoteAgent, connection_fields
from scheduling import Scheduler, ICONS
from outhman_auth import AuthService, AuthConfig, AuthError

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('SYSMAN_DATA_DIR', str(ROOT / 'agents')))
LOCK = threading.RLock()
STARTED = time.time()
AUTH = None
AUTH_LOCK = threading.Lock()

def auth_service():
    global AUTH
    with AUTH_LOCK:
        if AUTH is None:
            AUTH = AuthService(DATA / 'auth', Path(os.environ.get('SYSMAN_AUTH_KEY_FILE', str(ROOT / '.sysman-secrets' / 'outhman.key'))))
        return AUTH

SAMPLE_DEFAULTS = {'interval': 5, 'logging_enabled': True, 'log_level': 'info', 'labels': [], 'start_message': 'Agent started'}
SAMPLE = '''CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "interval": {"type": "number", "title": "Work interval (seconds)", "minimum": 0.1, "description": "Time between work cycles."},
        "logging_enabled": {"type": "boolean", "title": "Log completed cycles"},
        "log_level": {"type": "string", "title": "Cycle log level", "enum": ["debug", "info", "warning"], "format": "radio"},
        "labels": {"type": "array", "title": "Log labels", "items": {"type": "string"}, "description": "Labels attached to cycle log messages."},
        "start_message": {"type": "string", "title": "Startup message"}
    }
}

def run(ctx):
    """Replace this example with your agent's work loop."""
    cycles = 0
    ctx.log(ctx.config.get("start_message", "Agent started"))
    while not ctx.stopping.is_set():
        config = ctx.config
        cycles += 1
        ctx.metrics(cycles=cycles)
        if config.get("logging_enabled", True):
            labels = ", ".join(str(label) for label in config.get("labels", []))
            prefix = f"[{labels}] " if labels else ""
            ctx.log(f"{prefix}Completed work cycle {cycles}", config.get("log_level", "info"))
        if ctx.wait(float(config.get("interval", 5))):
            break
    ctx.log("Agent stopped cleanly")
'''


def now():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    temporary.replace(path)

def validate_config(value, schema, path='Configuration'):
    """Validate the form schema subset; preserve additional agent settings."""
    kind = schema.get('type')
    valid = {
        'object': lambda: isinstance(value, dict),
        'array': lambda: isinstance(value, list),
        'boolean': lambda: isinstance(value, bool),
        'string': lambda: isinstance(value, str),
        'number': lambda: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        'integer': lambda: isinstance(value, int) and not isinstance(value, bool),
        'null': lambda: value is None,
    }
    if kind in valid and not valid[kind]():
        raise ValueError(f'{path} must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} must be one of the available options')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            raise ValueError(f'{path} must be at least {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            raise ValueError(f'{path} must be at most {schema["maximum"]}')
    if isinstance(value, (str, list)):
        lower, upper = ('minLength', 'maxLength') if isinstance(value, str) else ('minItems', 'maxItems')
        if lower in schema and len(value) < schema[lower]:
            raise ValueError(f'{path} needs at least {schema[lower]} characters or items')
        if upper in schema and len(value) > schema[upper]:
            raise ValueError(f'{path} allows at most {schema[upper]} characters or items')
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value: raise ValueError(f'{path}.{key} is required')
        for key, item in value.items():
            validate_config(item, schema.get('properties', {}).get(key, {}), f'{path}.{key}')
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_config(item, schema.get('items', {}), f'{path}[{index + 1}]')


class Agent:
    def __init__(self, directory):
        self.directory = directory
        self.meta = json.loads((directory / 'agent.json').read_text())
        self.process = None
        self.status = 'stopped'
        self.metrics = {}
        self.history = []
        self.error = None
        self.expected_stops = set()
        self.started_at = None
        self.last_metrics_at = None
        self.logs = json.loads((directory / 'logs.json').read_text())

    def log(self, message, level='info'):
        with LOCK:
            self.logs.append({'time': now(), 'level': level, 'message': str(message)[:10000]})
            self.logs = self.logs[-2000:]
            write_json(self.directory / 'logs.json', self.logs)

    def config_schema(self):
        # Read declarative metadata without importing or executing agent code.
        try:
            tree = ast.parse((self.directory / 'main.py').read_text())
        except (SyntaxError, OSError):
            return {'type': 'object', 'properties': {}}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CONFIG_SCHEMA' for t in node.targets):
                try:
                    schema = ast.literal_eval(node.value)
                    if isinstance(schema, dict):
                        json.dumps(schema, allow_nan=False)
                        return schema
                except (ValueError, TypeError, OverflowError):
                    pass
        return {'type': 'object', 'properties': {}}

    def runtime(self):
        active = self.process is not None and self.process.poll() is None
        return {'pid': self.process.pid if active else None,
                'uptime': max(0, int(time.time() - self.started_at)) if active and self.started_at else 0,
                'last_metrics_at': self.last_metrics_at,
                'log_count': len(self.logs),
                'error_count': sum(str(entry.get('level', '')).lower() in ('error', 'critical') for entry in self.logs)}

    def snapshot(self):
        return {**self.meta, **(self.schedule_service.snapshot() if hasattr(self, 'schedule_service') else {}), 'status': self.status, 'metrics': self.metrics, 'history': self.history,
                'config': json.loads((self.directory / 'config.json').read_text()), 'config_schema': self.config_schema(),
                'runtime': self.runtime(), 'logs': self.logs[-200:],
                'error': self.error, 'backups': [{'name': p.name, 'created': datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(), 'size': p.stat().st_size} for p in sorted((self.directory / 'backups').glob('*.py'), reverse=True)]}

    def start(self):
        if self.process and self.process.poll() is None:
            raise ValueError('Agent is already active')
        self.error = None
        self.process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'agent_runner.py'), str(self.directory)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.status = 'running'
        self.started_at = time.time()
        self.metrics = {}
        self.history = []
        self.last_metrics_at = None
        self.log('Runner started')
        threading.Thread(target=self.monitor, args=(self.process,), daemon=True).start()

    def monitor(self, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError()
                with LOCK:
                    if event.get('kind') == 'metrics' and isinstance(event.get('metrics'), dict):
                        self.metrics = {str(k): v for k, v in event['metrics'].items() if isinstance(v, (int, float)) and math.isfinite(v)}
                        self.last_metrics_at = now()
                        self.history.append({'time': self.last_metrics_at, 'metrics': self.metrics.copy()})
                        self.history = self.history[-120:]
                    else:
                        self.log(event.get('message', line.strip()), event.get('level', 'info'))
            except (ValueError, TypeError):
                self.log(line.strip())
        process.stdout.close()
        code = process.wait()
        with LOCK:
            if self.process is process:
                self.status = 'stopped' if code == 0 or process.pid in self.expected_stops else 'error'
                self.error = f'Process exited with code {code}' if self.status == 'error' else None
                self.log(f'Runner exited ({code})', 'error' if self.error else 'info')
            self.expected_stops.discard(process.pid)

    def stop(self):
        process = self.process
        if process and process.poll() is None:
            self.expected_stops.add(process.pid)
            os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        self.status = 'stopped'

    def action(self, action):
        if action == 'start': self.start()
        elif action == 'stop': self.stop()
        elif action == 'restart':
            self.stop()
            self.start()
        elif action in ('pause', 'resume'):
            expected = 'running' if action == 'pause' else 'paused'
            if self.status != expected or not self.process or self.process.poll() is not None:
                raise ValueError(f'Cannot {action} this agent')
            os.killpg(self.process.pid, signal.SIGSTOP if action == 'pause' else signal.SIGCONT)
            self.status = 'paused' if action == 'pause' else 'running'
            self.log(f'Agent {self.status}')
        else: raise ValueError('Unknown action')

    def update(self, source):
        if not isinstance(source, str): raise ValueError('Source must be Python text')
        tree = ast.parse(source)
        if not any(isinstance(n, ast.FunctionDef) and n.name == 'run' for n in tree.body):
            raise ValueError('Source must define run(ctx)')
        previous = self.status
        self.stop()
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.py'
        (self.directory / 'backups' / name).write_bytes((self.directory / 'main.py').read_bytes())
        temporary = self.directory / 'main.tmp'
        temporary.write_text(source)
        temporary.replace(self.directory / 'main.py')
        self.log(f'Code updated; previous source saved as {name}')
        if previous in ('running', 'paused'):
            self.start()
            if previous == 'paused': self.action('pause')

AGENTS = {}
def attach_schedule(agent):
    agent.schedule_service = Scheduler(agent, write_json, LOCK)
    agent.schedule_service.start()
    return agent

def validate_icon(icon):
    if icon not in ICONS: raise ValueError('Choose a supported agent icon')
    return icon

def create_agent(name, description='', icon='agents'):
    validate_icon(icon)
    if not isinstance(name, str) or not name.strip() or len(name) > 100: raise ValueError('Name is required (up to 100 characters)')
    identifier = uuid.uuid4().hex[:12]
    directory = DATA / identifier
    directory.mkdir(parents=True)
    (directory / 'backups').mkdir()
    write_json(directory / 'agent.json', {'id': identifier, 'name': name.strip(), 'description': str(description)[:500], 'icon': icon})
    write_json(directory / 'config.json', SAMPLE_DEFAULTS)
    write_json(directory / 'logs.json', [])
    (directory / 'main.py').write_text(SAMPLE)
    AGENTS[identifier] = Agent(directory)
    return attach_schedule(AGENTS[identifier])

def register_remote(data):
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('Agent name is required (up to 100 characters)')
    icon = validate_icon(data.get('icon', 'agents'))
    connection = connection_fields(data)
    identifier = uuid.uuid4().hex[:12]
    directory = DATA / identifier
    meta = {'id': identifier, 'name': name.strip(), 'description': str(data.get('description', ''))[:500], 'type': 'remote', 'icon': icon}
    # Validate identity and credentials before persisting the registration.
    from remote_agent import curl_request
    payload = curl_request(connection)
    if str(payload.get('agent_id', '')).lower() != connection['agent_id'] or not isinstance(payload.get('config'), dict) or not isinstance(payload.get('logs'), list):
        raise ValueError('Remote identity or protocol does not match')
    with LOCK:
        if any(isinstance(existing, RemoteAgent) and existing.connection['agent_id'] == connection['agent_id'] for existing in AGENTS.values()):
            raise ValueError('This remote Agent ID is already registered')
        directory.mkdir(parents=True, mode=0o700)
        secret_path = directory / 'connection.json'
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle: json.dump(connection, handle)
        write_json(directory / 'agent.json', meta)
        agent = RemoteAgent(directory)
        agent.cache = payload
        agent.logs = payload['logs'][-2000:]
        agent.online = True
        AGENTS[identifier] = agent
    agent.start_polling()
    return attach_schedule(agent)


class Handler(BaseHTTPRequestHandler):
    def send_auth_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        token = getattr(self, 'new_cookie', None)
        if token:
            secure = '; Secure' if auth_service().config.secure_cookie else ''
            self.send_header('Set-Cookie', f'sysman_session={token}; HttpOnly; SameSite=Strict; Path=/{secure}')

    def browser_session(self):
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get('Cookie', ''))
        except Exception: pass
        token = cookie['sysman_session'].value if 'sysman_session' in cookie else None
        record, replacement = auth_service().browser(token)
        if replacement: self.new_cookie = replacement
        return record

    def reply(self, value, status=200, filename=None):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_auth_headers()
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self):
        # Local dashboard is same-origin only. Block cross-origin writes / DNS rebinding.
        host = self.headers.get('Host', '')
        configured_origin = auth_service().config.public_origin
        public_host = urlparse(configured_origin).netloc if configured_origin else None
        if host.split(':')[0] not in ('localhost', '127.0.0.1') and host != public_host:
            return self.reply({'error': 'Host is not allowed'}, 403)
        origin = self.headers.get('Origin')
        expected_origin = configured_origin if public_host == host else 'http://' + host
        if origin and origin != expected_origin:
            return self.reply({'error': 'Cross-origin requests are disabled'}, 403)
        try:
            path = [unquote(p) for p in urlparse(self.path).path.split('/') if p]
            data = {}
            if self.command in ('POST', 'PUT'):
                length = int(self.headers.get('Content-Length', 0))
                limit = 8191 if path[:2] == ['api', 'auth'] else 2_000_000
                if not 0 <= length <= limit: raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data, dict): raise ValueError('JSON object required')
            if path == ['api', 'health'] and self.command == 'GET':
                return self.reply({'status': 'healthy', 'python': sys.version.split()[0], 'uptime': int(time.time()-STARTED)})
            service = auth_service()
            record = self.browser_session()
            if path == ['api', 'auth', 'session'] and self.command == 'GET':
                return self.reply(service.public_session(record))
            if path[:2] != ['api', 'auth'] and not service.public_session(record)['authenticated']:
                return self.reply({'error': 'Sign in with OuthMan to continue.'}, 401)
            if self.command != 'GET':
                service.csrf(record, self.headers.get('X-CSRF-Token'))
            if path[:2] == ['api', 'auth']:
                if self.command != 'POST': return self.reply({'error': 'Not found'}, 404)
                if path[2:] == ['logout']:
                    replacement, token = service.logout(record)
                    self.new_cookie = token
                    return self.reply(service.public_session(replacement))
                if path[2:] not in (['enroll'], ['login']): return self.reply({'error': 'Not found'}, 404)
                service.limit(getattr(self, 'client_address', ('unknown',))[0], str(data.get('username', ''))[:254])
                if path[2:] == ['enroll']: return self.reply(service.enroll(data), 201)
                response, token = service.login(data, record)
                self.new_cookie = token
                return self.reply(response)
            if path == ['api', 'agents'] and self.command == 'POST' and data.get('type') == 'remote':
                return self.reply(register_remote(data).snapshot(), 201)
            managed = AGENTS.get(path[2]) if len(path) >= 3 and path[:2] == ['api', 'agents'] else None
            if managed and self.command == 'PUT' and path[3:] == ['schedule']:
                managed.schedule_service.save(data)
                return self.reply(managed.snapshot())
            if managed and self.command == 'PUT' and path[3:] == ['metadata']:
                icon = validate_icon(data.get('icon'))
                with LOCK:
                    updated = {**managed.meta, 'icon': icon}
                    write_json(managed.directory / 'agent.json', updated)
                    managed.meta = updated
                return self.reply(managed.snapshot())
            remote = AGENTS.get(path[2]) if len(path) >= 3 and path[:2] == ['api', 'agents'] else None
            if isinstance(remote, RemoteAgent) and self.command != 'GET':
                tail = path[3:]
                allowed = (self.command == 'POST' and tail in (['action'], ['update'], ['restore'])) or (self.command == 'PUT' and tail == ['config']) or (self.command == 'DELETE' and len(tail) == 2 and tail[0] == 'backups')
                if not allowed: return self.reply({'error': 'Not found'}, 404)
                if len(tail) == 2 and (Path(tail[1]).name != tail[1] or not tail[1].endswith('.py')):
                    raise ValueError('Invalid backup name')
                from urllib.parse import quote
                target = '/api/agent/' + '/'.join(quote(part, safe='') for part in tail)
                return self.reply(remote.request(self.command, target, data if self.command != 'DELETE' else None))
            with LOCK:
                if path == ['api', 'logs'] and self.command == 'GET':
                    logs = [{**entry, 'agent_id': a.meta['id'], 'agent_name': a.meta['name']} for a in AGENTS.values() for entry in a.logs]
                    logs.sort(key=lambda entry: entry['time'], reverse=True)
                    filename = 'sysman-logs.json' if parse_qs(urlparse(self.path).query).get('download') == ['1'] else None
                    return self.reply({'logs': logs}, filename=filename)
                if path == ['api', 'agents']:
                    if self.command == 'GET': return self.reply({'agents': [a.snapshot() for a in AGENTS.values()]})
                    if self.command == 'POST': return self.reply(create_agent(data.get('name'), data.get('description', ''), data.get('icon', 'agents')).snapshot(), 201)
                if len(path) < 3 or path[:2] != ['api', 'agents'] or path[2] not in AGENTS:
                    return self.reply({'error': 'Not found'}, 404)
                agent = AGENTS[path[2]]
                tail = path[3:]
                if tail == ['logs'] and self.command == 'GET':
                    filename = f'agent-{agent.meta["id"]}-logs.json' if parse_qs(urlparse(self.path).query).get('download') == ['1'] else None
                    return self.reply({'logs': agent.logs}, filename=filename)
                if tail == ['action'] and self.command == 'POST': agent.action(data.get('action'))
                elif tail == ['config'] and self.command == 'PUT':
                    json.dumps(data, allow_nan=False)
                    validate_config(data, agent.config_schema())
                    write_json(agent.directory / 'config.json', data)
                    agent.log('Configuration updated')
                elif tail == ['update'] and self.command == 'POST': agent.update(data.get('source'))
                elif tail == ['restore'] and self.command == 'POST':
                    backup = data.get('backup', '')
                    if not isinstance(backup, str) or Path(backup).name != backup or not backup.endswith('.py'): raise ValueError('Invalid backup')
                    agent.update((agent.directory / 'backups' / backup).read_text())
                elif len(tail) == 2 and tail[0] == 'backups' and self.command == 'DELETE':
                    if Path(tail[1]).name != tail[1] or not tail[1].endswith('.py'): raise ValueError('Invalid backup')
                    (agent.directory / 'backups' / tail[1]).unlink()
                    agent.log(f'Backup deleted: {tail[1]}')
                else: return self.reply({'error': 'Not found'}, 404)
                return self.reply(agent.snapshot())
        except AuthError as error:
            self.reply({'error': str(error)}, error.status)
        except (ValueError, SyntaxError, OSError, TypeError, sqlite3.Error) as error:
            if urlparse(self.path).path.startswith('/api/auth/'):
                return self.reply({'error': 'Authentication could not complete. Contact the operator before retrying enrollment.'}, 503)
            self.reply({'error': str(error)}, 400)

    def do_GET(self):
        if urlparse(self.path).path.startswith('/api/'):
            return self.handle_api()
        requested = urlparse(self.path).path
        public_assets = ('/login', '/login.html', '/login.js', '/login.css', '/style.css', '/auth-session.js')
        if requested not in public_assets:
            try:
                if not auth_service().public_session(self.browser_session())['authenticated']:
                    self.send_response(303)
                    self.send_auth_headers()
                    self.send_header('Location', '/login')
                    self.end_headers()
                    return
            except AuthError as error:
                return self.reply({'error': str(error)}, error.status)
        if requested == '/login': self.path = '/login.html'
        if urlparse(self.path).path in ('/AGENT_INTEGRATION.md', '/REMOTE_AGENTS.md', '/deployment.md', '/AUTHENTICATION.md'):
            body = (ROOT / urlparse(self.path).path.lstrip('/')).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_auth_headers()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        name = urlparse(self.path).path.lstrip('/') or 'index.html'
        path = (ROOT / 'static' / name).resolve()
        if not path.is_relative_to(ROOT / 'static') or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_auth_headers()
        self.send_header('Content-Type', {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript'}.get(path.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = handle_api
    do_PUT = handle_api
    do_DELETE = handle_api
    def log_message(self, *_): pass

def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    for directory in DATA.iterdir():
        if directory.is_dir() and (directory / 'agent.json').exists():
            metadata = json.loads((directory / 'agent.json').read_text())
            agent = RemoteAgent(directory) if metadata.get('type') == 'remote' else Agent(directory)
            AGENTS[agent.meta['id']] = agent
            if isinstance(agent, RemoteAgent): agent.start_polling()
            attach_schedule(agent)
    if not AGENTS:
        create_agent('Email router', 'Route incoming messages to the right destination. Example agent — add your own logic.')
        create_agent('Backup agent', 'Schedule and monitor your backup jobs. Example agent — add your own logic.')

if __name__ == '__main__':
    auth_service()
    initialize()
    server = ThreadingHTTPServer((os.environ.get('SYSMAN_BIND', '127.0.0.1'), int(os.environ.get('PORT', '8765'))), Handler)
    print(f'SysMan running at http://localhost:{server.server_port}', flush=True)
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        for agent in AGENTS.values():
            if hasattr(agent, 'schedule_service'): agent.schedule_service.stop()
        for agent in AGENTS.values(): agent.stop()
        server.server_close()
