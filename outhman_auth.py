"""OuthMan v1 linking, confirmed login, encrypted bindings and server-side sessions."""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from cryptography.fernet import Fernet, InvalidToken


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs): return None


def read_secret(name, environment):
    path = environment.get(name + '_FILE')
    return Path(path).read_text().strip() if path else environment.get(name, '').strip()


class AuthConfig:
    def __init__(self, environment=None):
        env = os.environ if environment is None else environment
        self.base_url = env.get('OUTHMAN_BASE_URL', '').rstrip('/')
        self.app_id = env.get('OUTHMAN_APP_ID', '').strip().lower()
        self.app_secret = ''
        self.allowed_users = {name.strip().casefold() for name in env.get('SYSMAN_ALLOWED_USERS', '').split(',') if name.strip()}
        self.public_origin = env.get('SYSMAN_PUBLIC_ORIGIN', '').rstrip('/')
        self.secure_cookie = self.public_origin.startswith('https://') or env.get('SYSMAN_COOKIE_SECURE', 'false').lower() == 'true'
        self.issue = None
        try:
            self.app_secret = read_secret('OUTHMAN_APP_SECRET', env)
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError()
            local = parsed.hostname == 'localhost'
            try: local = local or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError: pass
            if parsed.scheme == 'http' and not local and env.get('OUTHMAN_ALLOW_INSECURE_HTTP') != 'true': raise ValueError()
            if not re.fullmatch(r'[0-9a-f]{16}', self.app_id) or not re.fullmatch(r'[0-9a-fA-F]{32}', self.app_secret): raise ValueError()
            if not self.allowed_users or len(self.allowed_users) > 100: raise ValueError()
            if self.public_origin:
                public = urlsplit(self.public_origin)
                if public.scheme not in ('http', 'https') or not public.hostname or public.username or public.password or public.path not in ('', '/') or public.query or public.fragment: raise ValueError()
        except (ValueError, OSError):
            self.issue = 'OuthMan login is not configured. Ask the SysMan operator to configure the app credentials, URL and allowed users.'


def call_outhman(config, endpoint, payload):
    body = json.dumps(payload).encode()
    if len(body) >= 8192: raise AuthError('Login request is too large.', 400)
    request = Request(config.base_url + '/api/v1/' + endpoint, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'X-OuthMan-App': config.app_id,
        'Authorization': 'Bearer ' + config.app_secret})
    # No automatic proxying, redirects or credential-bearing retries.
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            status = response.status
            encoded = response.read(8193)
    except HTTPError as error:
        status = error.code
        error.close()
        messages = {401: 'OuthMan rejected the credentials or code. Generate a fresh code and try again.',
                    403: 'OuthMan denied access. Check the app access group, account approval and backend IP allowlist.',
                    409: 'This account is already linked in OuthMan. Restore the original SysMan binding or use administrator recovery.',
                    423: 'OuthMan has blocked or suspended this account. Contact its administrator.',
                    429: 'OuthMan is limiting authentication attempts. Wait before trying again.'}
        raise AuthError(messages.get(status, 'OuthMan authentication failed. Try again with a fresh code.'), status if status in messages else 502) from None
    except (URLError, TimeoutError, OSError):
        raise AuthError('OuthMan is unavailable. Login remains locked; try again with a fresh code.', 503) from None
    try:
        result = json.loads(encoded)
        if len(encoded) > 8192 or not isinstance(result, dict) or status != (201 if endpoint == 'enroll' else 200): raise ValueError()
        return result
    except (ValueError, TypeError):
        raise AuthError('OuthMan returned an invalid response. No session was created.', 502) from None


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, maxmem=64*1024*1024)
    return base64.b64encode(salt).decode() + ':' + base64.b64encode(digest).decode()


def password_matches(password, stored):
    try:
        salt, _ = stored.split(':', 1)
        return hmac.compare_digest(password_hash(password, base64.b64decode(salt)), stored)
    except (ValueError, TypeError): return False


class AuthService:
    def __init__(self, directory, key_file, config=None, transport=call_outhman):
        self.directory, self.key_file = Path(directory), Path(key_file)
        self.config = config or AuthConfig()
        self.transport = transport
        self.lock = threading.RLock()
        self.enrollment_lock = threading.Lock()
        self.sessions, self.pending, self.attempts = {}, {}, {}
        self.database = self.directory / 'accounts.sqlite3'
        self.cipher = None
        self.storage_ready = False
        # A missing configuration never falls back to anonymous access.
        if self.config.issue: return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.key_file.exists():
                if self.database.exists():
                    raise ValueError('Missing encryption key')
                self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(self.key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(Fernet.generate_key())
                    output.flush(); os.fsync(output.fileno())
            self.cipher = Fernet(self.key_file.read_bytes().strip())
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, provider TEXT NOT NULL, app_id TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL, login TEXT NOT NULL, password_hash TEXT NOT NULL, encrypted_key TEXT NOT NULL, UNIQUE(provider,app_id,user_id), UNIQUE(provider,app_id,login))')
                # Detect a replaced key before accepting any new enrollments.
                for row in db.execute('SELECT * FROM accounts'):
                    self.decrypt_binding(row)
            os.chmod(self.database, 0o600)
            self.storage_ready = True
        except (OSError, ValueError, InvalidToken, sqlite3.Error):
            self.config.issue = 'Authentication storage is unavailable. Ask the operator to restore the matching account database and encryption key.'

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally:
            db.close()

    def ready(self):
        if self.config.issue or not self.storage_ready: raise AuthError(self.config.issue or 'Authentication is unavailable.', 503)

    def browser(self, token=None):
        now = time.time()
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items() if value['expires'] > now and value['idle'] > now}
            self.pending = {key: value for key, value in self.pending.items() if value['expires'] > now}
            digest = hashlib.sha256((token or '').encode()).hexdigest()
            record = self.sessions.get(digest)
            if record:
                record['idle'] = min(record['expires'], now + (1800 if record['user'] else 600))
                if record['user'] and record['user']['username'].casefold() not in self.config.allowed_users:
                    del self.sessions[digest]
                else: return record, None
            if len(self.sessions) >= 2048: raise AuthError('Too many active browser sessions. Try again later.', 429)
            token = secrets.token_urlsafe(32)
            record = {'user': None, 'csrf': secrets.token_urlsafe(32), 'expires': now+600, 'idle': now+600,
                      'browser': hashlib.sha256(token.encode()).hexdigest()}
            self.sessions[record['browser']] = record
            return record, token

    def csrf(self, record, supplied):
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), record['csrf'].encode()):
            raise AuthError('The browser session changed. Refresh the page and try again.', 403)

    def public_session(self, record):
        return {'authenticated': bool(record['user']) and not self.config.issue,
                'configured': self.storage_ready and not self.config.issue, 'csrf_token': record['csrf'], 'user': record['user']}

    def limit(self, peer, username):
        now = time.time()
        with self.lock:
            self.attempts = {key: entry for key, entry in self.attempts.items() if now-entry[0] < 900}
            for key in ('ip:'+peer, 'user:'+username.casefold()):
                start, count = self.attempts.get(key, (now, 0))
                if count >= 10: raise AuthError('Too many authentication attempts. Wait 15 minutes before retrying.', 429)
                self.attempts[key] = (start, count+1)
            if len(self.attempts) > 4096: raise AuthError('Authentication is busy. Try again later.', 429)

    def credentials(self, data):
        self.ready()
        username, password, code = data.get('username'), data.get('password'), data.get('code')
        if not isinstance(username, str) or not 1 <= len(username.strip()) <= 254 or not isinstance(password, str) or not 1 <= len(password.encode()) <= 1024 or not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            raise AuthError('Enter a username, password and a six-digit OuthMan code.', 400)
        username = username.strip()
        if username.casefold() not in self.config.allowed_users: raise AuthError('This account is not allowed to operate SysMan.', 403)
        return username, password, code

    def lookup(self, username):
        with self.connect() as db:
            return db.execute('SELECT * FROM accounts WHERE provider=? AND app_id=? AND login=?',
                              (self.config.base_url, self.config.app_id, username.casefold())).fetchone()

    def decrypt_binding(self, account):
        payload = json.loads(self.cipher.decrypt(account['encrypted_key'].encode()))
        expected = {'provider': account['provider'], 'app_id': account['app_id'], 'user_id': account['user_id'], 'username': account['username']}
        if any(payload.get(key) != value for key, value in expected.items()) or not re.fullmatch(r'[0-9a-fA-F]{32}', payload.get('user_key', '')):
            raise InvalidToken()
        return payload['user_key']

    def enroll(self, data):
        username, password, code = self.credentials(data)
        with self.enrollment_lock:
            if self.lookup(username): raise AuthError('This account is already linked. Sign in with a fresh code.', 409)
            hashed = password_hash(password)
            # Reserve a local write transaction before consuming the provider's once-visible key.
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                result = self.transport(self.config, 'enroll', {'username': username, 'password': password, 'code': code})
                canonical = result.get('username')
                if result.get('app_id') != self.config.app_id or type(result.get('user_id')) is not int or result['user_id'] <= 0 or not isinstance(canonical, str) or canonical.casefold() != username.casefold() or not isinstance(result.get('user_key'), str) or not re.fullmatch(r'[0-9a-fA-F]{32}', result['user_key']):
                    raise AuthError('OuthMan returned an invalid account binding. Ask its administrator for recovery.', 502)
                encrypted = self.cipher.encrypt(json.dumps({'provider': self.config.base_url, 'app_id': self.config.app_id, 'user_id': result['user_id'], 'username': canonical, 'user_key': result['user_key']}).encode()).decode()
                try:
                    db.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)', (secrets.token_hex(16), self.config.base_url, self.config.app_id, result['user_id'], canonical, username.casefold(), hashed, encrypted))
                except sqlite3.IntegrityError:
                    raise AuthError('This OuthMan identity is already linked. Use administrator recovery.', 409) from None
        return {'linked': True, 'message': 'Account linked. Generate a new OuthMan code, then sign in.'}

    def login(self, data, record):
        username, password, code = self.credentials(data)
        account = self.lookup(username)
        # A fixed dummy hash avoids a fast unknown-account check.
        if not account:
            password_hash(password, b'\0'*16)
            raise AuthError('Sign-in failed. Link your account first, or check your credentials.')
        if not password_matches(password, account['password_hash']):
            raise AuthError('Sign-in failed. Check your password; if it changed in OuthMan, ask the SysMan operator to synchronize it.')
        try: user_key = self.decrypt_binding(account)
        except (InvalidToken, ValueError, TypeError): raise AuthError('Stored account binding is unavailable. Contact the SysMan operator.', 503) from None
        request_id = secrets.token_urlsafe(32)
        moment = time.time()
        pending = {'account': account['id'], 'expected_user': account['user_id'], 'browser': record['browser'], 'expires': moment+30}
        with self.lock:
            if record['browser'] not in self.sessions: raise AuthError('The login session expired. Refresh and retry.', 401)
            self.pending[request_id] = pending
        try:
            verified = self.transport(self.config, 'verify', {'username': account['username'], 'password': password, 'code': code, 'user_key': user_key, 'request_id': request_id})
            expiry = verified.get('expires_at')
            if verified.get('request_id') != request_id or not isinstance(verified.get('auth_pin'), str) or not re.fullmatch(r'[0-9a-fA-F]{32}', verified['auth_pin']) or isinstance(expiry, bool) or not isinstance(expiry, (int,float)) or not math.isfinite(expiry) or expiry <= time.time():
                raise AuthError('OuthMan returned an invalid or expired verification. Generate a fresh code.', 502)
            confirmed = self.transport(self.config, 'confirm', {'username': account['username'], 'auth_pin': verified['auth_pin'], 'request_id': request_id})
            if confirmed.get('verified') is not True or confirmed.get('app_id') != self.config.app_id or type(confirmed.get('user_id')) is not int or confirmed['user_id'] != account['user_id'] or confirmed.get('request_id') != request_id or confirmed.get('username') != account['username']:
                raise AuthError('OuthMan confirmation did not match this login. No session was created.', 502)
            with self.lock:
                saved = self.pending.pop(request_id, None)
                current = self.sessions.get(record['browser'])
                if saved != pending or not current or current is not record or saved['expires'] <= time.time() or expiry <= time.time() or record['expires'] <= time.time() or saved['browser'] != record['browser'] or saved['expected_user'] != account['user_id']:
                    raise AuthError('Login expired or was already completed. Generate a fresh code.', 401)
                del self.sessions[record['browser']]
                token = secrets.token_urlsafe(32)
                digest = hashlib.sha256(token.encode()).hexdigest()
                current = {'user': {'username': account['username']}, 'csrf': secrets.token_urlsafe(32), 'expires': time.time()+28800,
                           'idle': time.time()+1800, 'browser': digest}
                self.sessions[digest] = current
                return self.public_session(current), token
        finally:
            with self.lock: self.pending.pop(request_id, None)

    def logout(self, record):
        with self.lock:
            self.sessions.pop(record['browser'], None)
            for request_id in list(self.pending):
                if self.pending[request_id]['browser'] == record['browser']: self.pending.pop(request_id)
        return self.browser()
