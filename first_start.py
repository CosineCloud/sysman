"""One-time, host-authorized setup of encrypted OuthMan configuration."""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from cryptography.fernet import Fernet, InvalidToken
from outhman_auth import AuthConfig, AuthService, AuthError

SETTINGS_KEYS = {'OUTHMAN_BASE_URL', 'OUTHMAN_APP_ID', 'OUTHMAN_APP_SECRET', 'SYSMAN_ALLOWED_USERS', 'OUTHMAN_ALLOW_INSECURE_HTTP'}


def settings_path(directory): return Path(directory) / 'outhman-settings.enc'


def load_config(directory, key_file, environment=None):
    environment = dict(os.environ if environment is None else environment)
    path = settings_path(directory)
    if not path.exists(): return AuthConfig(environment)
    try:
        saved = json.loads(Fernet(Path(key_file).read_bytes().strip()).decrypt(path.read_bytes()))
        if not isinstance(saved, dict) or set(saved) != SETTINGS_KEYS or any(not isinstance(value, str) for value in saved.values()): raise ValueError()
        environment.update(saved)
        # Portal-saved app secret takes precedence over an old file-secret setting.
        environment.pop('OUTHMAN_APP_SECRET_FILE', None)
        return AuthConfig(environment)
    except (OSError, ValueError, TypeError, InvalidToken):
        config = AuthConfig({})
        config.issue = 'Saved OuthMan settings cannot be decrypted. Restore the matching configuration and encryption-key volumes.'
        return config


def has_accounts(directory):
    path = Path(directory) / 'auth' / 'accounts.sqlite3'
    if not path.exists(): return False
    try:
        connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
        try: return connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] > 0
        finally: connection.close()
    except sqlite3.Error:
        # Never offer setup over damaged or unknown existing authentication records.
        return True


class FirstStart:
    def __init__(self, directory, key_file, environment=None):
        self.directory, self.key_file = Path(directory), Path(key_file)
        self.environment = dict(os.environ if environment is None else environment)
        self.token_path = self.key_file.parent / 'setup.token'
        self.lock = threading.RLock()

    def required(self, service):
        return not service.storage_ready and not settings_path(self.directory).exists() and not has_accounts(self.directory)

    def token(self, service):
        with self.lock:
            if not self.required(service): raise AuthError('First-start setup is closed. Use the existing login or operator recovery.', 409)
            self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError: pass
            else:
                with os.fdopen(descriptor, 'w') as output:
                    output.write(secrets.token_urlsafe(32))
                    output.flush(); os.fsync(output.fileno())
            value = self.token_path.read_text().strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', value): raise AuthError('Setup token is unavailable. Contact the operator.', 503)
            return value

    def status(self, service, record):
        required = self.required(service)
        if required: self.token(service)
        return {'setup_required': required, 'configured': service.storage_ready and not service.config.issue,
                'csrf_token': record['csrf'], 'defaults': {'base_url':'', 'app_id':'', 'allowed_users':[], 'allow_insecure_http':False}}

    def reopen(self, service):
        """Host-only recovery, run with the service stopped and no linked accounts."""
        with self.lock:
            service.ready()
            if has_accounts(self.directory):
                raise AuthError('Linked accounts exist. Restore the matching settings/key; setup cannot replace their provider identity.', 409)
            if not AuthConfig(self.environment).issue:
                raise AuthError('Environment variables already configure OuthMan. Correct or clear them before reopening portal setup.', 409)
            path = settings_path(self.directory)
            if not path.exists(): raise AuthError('No saved portal settings to reopen.', 409)
            backup = path.with_name('outhman-settings-' + secrets.token_hex(8) + '.enc.backup')
            path.replace(backup)
            self.token_path.unlink(missing_ok=True)
            return backup.name

    def save(self, service, data):
        with self.lock:
            expected = self.token(service)
            supplied = data.get('setup_token')
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise AuthError('Invalid setup token. Retrieve it from the SysMan container and try again.', 403)
            base, app_id, secret, users = data.get('base_url'), data.get('app_id'), data.get('app_secret'), data.get('allowed_users')
            insecure = data.get('allow_insecure_http', False)
            if not isinstance(base, str) or not isinstance(app_id, str) or not isinstance(secret, str) or not isinstance(users, list) or not 1 <= len(users) <= 100 or any(not isinstance(user, str) or not user.strip() or len(user.strip()) > 254 or ',' in user or '\n' in user or '\r' in user for user in users) or type(insecure) is not bool:
                raise AuthError('Enter the OuthMan URL, app credentials and at least one allowed username.', 400)
            saved = {'OUTHMAN_BASE_URL':base.strip().rstrip('/'), 'OUTHMAN_APP_ID':app_id.strip().lower(),
                     'OUTHMAN_APP_SECRET':secret.strip(), 'SYSMAN_ALLOWED_USERS':','.join(user.strip() for user in users),
                     'OUTHMAN_ALLOW_INSECURE_HTTP':'true' if insecure else 'false'}
            environment = {**self.environment, **saved}
            environment.pop('OUTHMAN_APP_SECRET_FILE', None)
            config = AuthConfig(environment)
            if config.issue:
                raise AuthError('Check the URL, 16-hex app ID, 32-hex app secret and usernames. Non-local HTTP requires the trusted-network checkbox.', 400)
            candidate = AuthService(self.directory/'auth', self.key_file, config)
            candidate.ready()
            # Encrypt the secret-bearing settings and atomically commit before enabling login.
            path = settings_path(self.directory)
            temporary = path.with_suffix('.tmp')
            encoded = candidate.cipher.encrypt(json.dumps(saved).encode())
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor,'wb') as output:
                output.write(encoded); output.flush(); os.fsync(output.fileno())
            temporary.replace(path)
            # Completion closes setup even if removing a stale token file fails.
            try: self.token_path.unlink(missing_ok=True)
            except OSError: pass
            return candidate
