"""Authenticated remote-agent adapter. All network I/O uses the curl executable."""
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path


def connection_fields(data):
    identifier = str(data.get('agent_id', '')).lower()
    key = str(data.get('security_key', '')).lower()
    if not re.fullmatch(r'[0-9a-f]{8}', identifier):
        raise ValueError('Agent ID must be exactly 8 hexadecimal characters (32 bits)')
    if not re.fullmatch(r'[0-9a-f]{16}', key):
        raise ValueError('Security key must be exactly 16 hexadecimal characters (64 bits)')
    host = str(data.get('host', '')).strip()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        raise ValueError('Enter an IPv4 or IPv6 address') from None
    port = data.get('port')
    if isinstance(port, bool): raise ValueError('Invalid port')
    try: port = int(port)
    except (ValueError, TypeError): raise ValueError('Invalid port') from None
    if not 1 <= port <= 65535: raise ValueError('Port must be 1–65535')
    scheme = data.get('scheme', 'http')
    if scheme not in ('http', 'https'): raise ValueError('Protocol must be http or https')
    return {'agent_id': identifier, 'security_key': key, 'host': host, 'port': port, 'scheme': scheme}


def curl_request(connection, method='GET', path='/api/agent', data=None):
    """Keep credentials/bodies off argv, out of shell expansion, and out of errors."""
    host = connection['host']
    if ':' in host: host = f'[{host}]'
    url = f'{connection["scheme"]}://{host}:{connection["port"]}{path}'
    config = [f'url = {json.dumps(url)}', f'request = {json.dumps(method)}',
              f'header = {json.dumps("X-Agent-ID: " + connection["agent_id"])}',
              f'header = {json.dumps("Authorization: Bearer " + connection["security_key"])}']
    if data is not None:
        config += ['header = "Content-Type: application/json"',
                   'data-binary = ' + json.dumps(json.dumps(data, allow_nan=False))]
    try:
        result = subprocess.run(['curl', '--disable', '--silent', '--show-error', '--noproxy', '*',
                                 '--proto', '=http,https', '--connect-timeout', '2', '--max-time', '15',
                                 '--max-filesize', '268435456', '--write-out', '\n%{http_code}', '--config', '-'],
                                input='\n'.join(config)+'\n', text=True, capture_output=True, timeout=17)
    except FileNotFoundError:
        raise ValueError('curl is required on the SysMan host') from None
    except subprocess.TimeoutExpired:
        raise ValueError('Agent request timed out') from None
    if result.returncode:
        raise ValueError('Agent connection failed; check address, port and TLS configuration')
    try:
        body, status = result.stdout.rsplit('\n', 1)
        status = int(status)
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise ValueError('Agent returned an invalid response') from None
    if status in (401, 403): raise ValueError('Agent rejected its ID or security key')
    if status != 200:
        # Do not reflect an untrusted endpoint's body: it may echo request secrets.
        raise ValueError(f'Agent request failed (HTTP {status}); check the agent logs')
    if not isinstance(payload, dict): raise ValueError('Invalid agent response')
    return payload


class RemoteAgent:
    def __init__(self, directory, transport=curl_request):
        self.directory = Path(directory)
        self.meta = json.loads((self.directory / 'agent.json').read_text())
        self.connection = json.loads((self.directory / 'connection.json').read_text())
        self.transport = transport
        self.guard = threading.RLock()
        self.poll_guard = threading.Lock()
        self.cache = {}
        self.logs = []
        self.error = None
        self.online = False
        self.closed = threading.Event()
        self.thread = None

    def sync(self):
        with self.poll_guard:
            try:
                payload = self.transport(self.connection)
                if str(payload.get('agent_id', '')).lower() != self.connection['agent_id']:
                    raise ValueError('Remote identity does not match the configured Agent ID')
                if not isinstance(payload.get('config'), dict) or not isinstance(payload.get('logs'), list):
                    raise ValueError('Agent response does not implement the SysMan contract')
                with self.guard:
                    self.cache = payload
                    self.logs = payload['logs'][-2000:]
                    self.error = None
                    self.online = True
            except ValueError as error:
                with self.guard:
                    self.online = False
                    self.error = str(error)
                raise

    def start_polling(self):
        if self.thread: return
        def poll():
            while not self.closed.wait(2):
                try: self.sync()
                except ValueError: pass
        self.thread = threading.Thread(target=poll, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.guard:
            payload = {key: self.cache[key] for key in ('status', 'metrics', 'history', 'config', 'config_schema', 'runtime', 'error', 'backups') if key in self.cache}
            payload.update({**self.meta, **(self.schedule_service.snapshot() if hasattr(self, 'schedule_service') else {}), 'type': 'remote',
                            'connection': {k: v for k, v in self.connection.items() if k != 'security_key'},
                            'status': self.cache.get('status', 'stopped') if self.online else 'offline',
                            'error': self.error or self.cache.get('error'), 'logs': self.logs[-200:]})
            for key, default in [('metrics', {}), ('history', []), ('config', {}), ('config_schema', {}), ('runtime', {}), ('backups', [])]:
                payload.setdefault(key, default)
            return payload

    def request(self, method, path, data=None):
        self.transport(self.connection, method, path, data)
        self.sync()
        return self.snapshot()

    def stop(self):
        # Disconnect the client, leaving an independently running remote agent alone.
        self.closed.set()
