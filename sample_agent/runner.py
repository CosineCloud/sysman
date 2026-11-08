"""Persistent authenticated HTTP runner; worker main.py runs in a child process."""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import socket
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
import server as core

DEFAULT_CONFIG = {'source_directory': 'demo_source', 'backup_directory': 'backup_output',
                  'interval': 10, 'enabled': True, 'compression': 'deflated', 'mode': 'on_change',
                  'include_patterns': ['*'], 'exclude_patterns': ['*.tmp', '.DS_Store'],
                  'max_file_mb': 10, 'label': 'SysMan integration test'}


def initialize(directory, name='File backup agent'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    security_path = directory / 'security.json'
    if security_path.exists(): raise ValueError('Already initialized; existing credentials were not changed')
    identifier, key = secrets.token_hex(4), secrets.token_hex(8)
    descriptor = os.open(security_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump({'agent_id': identifier, 'security_key_hash': hashlib.sha256(key.encode()).hexdigest()}, output)
    main_path = directory / 'main.py'
    if not main_path.exists():
        main_path.write_bytes(Path(__file__).with_name('main.py').read_bytes())
    (directory / 'backups').mkdir(exist_ok=True)
    core.write_json(directory / 'agent.json', {'id': identifier, 'name': name, 'description': 'Real ZIP file backups with filtering, compression and change detection.'})
    core.write_json(directory / 'config.json', DEFAULT_CONFIG)
    core.write_json(directory / 'logs.json', [])
    demo = directory / 'demo_source'
    demo.mkdir(exist_ok=True)
    (demo / 'welcome.txt').write_text('SysMan backup integration test. Edit this file to trigger another backup.\n')
    return identifier, key


def make_handler(agent, security):
    operations = threading.Lock()
    attempts = {}
    auth_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, payload, status=200):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            peer = self.client_address[0]
            with auth_lock:
                count, until = attempts.get(peer, (0, 0))
                if until > time.monotonic(): return False
                if until: count = 0
                supplied = self.headers.get('Authorization', '')
                key = supplied[7:] if supplied.startswith('Bearer ') else ''
                valid_id = hmac.compare_digest(self.headers.get('X-Agent-ID', '').encode(), security['agent_id'].encode())
                valid_key = hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), security['security_key_hash'])
                if valid_id and valid_key:
                    attempts.pop(peer, None)
                    return True
                count += 1
                if len(attempts) > 1000: attempts.clear()
                attempts[peer] = (count, time.monotonic()+60 if count >= 20 else 0)
                return False

        def handle_api(self):
            self.connection.settimeout(20)
            if not self.authorized(): return self.reply({'error': 'Invalid agent credentials or temporarily rate limited'}, 401)
            if self.headers.get('Origin'): return self.reply({'error': 'Browser calls are disabled; use SysMan'}, 403)
            try:
                path = [unquote(p) for p in urlparse(self.path).path.split('/') if p]
                if path[:2] != ['api', 'agent']: return self.reply({'error': 'Not found'}, 404)
                tail = path[2:]
                data = {}
                if self.command in ('POST', 'PUT'):
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 2_000_000: raise ValueError('Invalid request size')
                    data = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(data, dict): raise ValueError('JSON object required')
                if self.command == 'GET':
                    with core.LOCK:
                        if not tail:
                            payload = agent.snapshot()
                            payload.update(agent_id=security['agent_id'], logs=agent.logs[:])
                            return self.reply(payload)
                        if tail == ['logs']: return self.reply({'logs': agent.logs[:]})
                    return self.reply({'error': 'Not found'}, 404)
                with operations:
                    if self.command == 'POST' and tail == ['action']: agent.action(data.get('action'))
                    elif self.command == 'PUT' and tail == ['config']:
                        core.validate_config(data, agent.config_schema())
                        core.write_json(agent.directory / 'config.json', data)
                        agent.log('Configuration updated')
                    elif self.command == 'POST' and tail == ['update']: agent.update(data.get('source'))
                    elif self.command == 'POST' and tail == ['restore']:
                        name = data.get('backup', '')
                        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.py'): raise ValueError('Invalid backup name')
                        agent.update((agent.directory / 'backups' / name).read_text())
                    elif self.command == 'DELETE' and len(tail) == 2 and tail[0] == 'backups':
                        name = tail[1]
                        if Path(name).name != name or not name.endswith('.py'): raise ValueError('Invalid backup name')
                        (agent.directory / 'backups' / name).unlink()
                        agent.log(f'Code backup deleted: {name}')
                    else: return self.reply({'error': 'Not found'}, 404)
                    with core.LOCK:
                        payload = agent.snapshot()
                        payload.update(agent_id=security['agent_id'], logs=agent.logs[:])
                        return self.reply(payload)
            except (ValueError, TypeError, SyntaxError, OSError) as error:
                self.reply({'error': str(error)}, 400)

        do_GET = handle_api
        do_POST = handle_api
        do_PUT = handle_api
        do_DELETE = handle_api
        def log_message(self, *_): pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', action='store_true', help='Initialize and show credentials once')
    parser.add_argument('--name', default='File backup agent')
    parser.add_argument('--data-dir', default=os.environ.get('SYSMAN_AGENT_DIR'), help='Persistent agent directory (main.py, config, logs and backups)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9101)
    parser.add_argument('--cert', help='HTTPS certificate PEM')
    parser.add_argument('--tls-key', help='HTTPS private key PEM')
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve() if args.data_dir else Path(__file__).resolve().parent
    if args.init:
        identifier, key = initialize(directory, args.name)
        print(f'Agent ID (32-bit): {identifier}\nSecurity Key (64-bit, save now): {key}')
        print(f'Name: {args.name}\nDefault address: 127.0.0.1:{args.port}\nRun without --init to start the runner.')
        return
    if not (directory / 'security.json').exists(): parser.error('Run with --init first')
    if bool(args.cert) != bool(args.tls_key): parser.error('Provide both --cert and --tls-key')
    if not ipaddress.ip_address(args.host).is_loopback and not args.cert:
        parser.error('Non-loopback binding requires --cert and --tls-key (HTTPS)')
    security = json.loads((directory / 'security.json').read_text())
    agent = core.Agent(directory)
    class AgentServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in args.host else socket.AF_INET
    api = AgentServer((args.host, args.port), make_handler(agent, security))
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.tls_key)
        api.socket = context.wrap_socket(api.socket, server_side=True)
    print(f'Runner listening on {"https" if args.cert else "http"}://{args.host}:{args.port}; worker stopped', flush=True)
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try: api.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        agent.stop()
        api.server_close()

if __name__ == '__main__': main()
