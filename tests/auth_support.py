"""Test-only OuthMan protocol fixture; contains no production credentials."""
import json
import time
from http.server import BaseHTTPRequestHandler
from outhman_auth import AuthConfig, AuthError

APP_ID = 'a'*16
APP_SECRET = 'b'*32
USER_KEY = 'c'*32
PASSWORD = 'test-password!'


def config(base='http://127.0.0.1:10070'):
    return AuthConfig({'OUTHMAN_BASE_URL': base, 'OUTHMAN_APP_ID': APP_ID,
                       'OUTHMAN_APP_SECRET': APP_SECRET, 'SYSMAN_ALLOWED_USERS': 'alice'})


class Provider:
    def __init__(self):
        self.code = '012345'; self.linked = False; self.pins = {}; self.calls = []; self.override = {}
    def issue(self, code='012345'): self.code = code
    def transport(self, settings, endpoint, data):
        self.calls.append((endpoint, data.copy()))
        if endpoint in ('enroll', 'verify'):
            if data['username'] != 'alice' or data['password'] != PASSWORD or data['code'] != self.code or self.code is None:
                raise AuthError('Rejected test credentials/code', 401)
            if endpoint == 'enroll':
                if self.linked: raise AuthError('Already linked', 409)
                self.linked = True; self.code = None
                return {**{'app_id': APP_ID, 'user_id': 42, 'username': 'alice', 'user_key': USER_KEY}, **self.override.get(endpoint, {})}
            if not self.linked or data['user_key'] != USER_KEY: raise AuthError('Wrong user key', 401)
            self.code = None
            pin = 'd'*32; self.pins[pin] = data['request_id']
            return {**{'auth_pin': pin, 'request_id': data['request_id'], 'expires_at': time.time()+30}, **self.override.get(endpoint, {})}
        request = self.pins.pop(data['auth_pin'], None)
        if not request or request != data['request_id']: raise AuthError('Expired PIN', 401)
        return {**{'verified': True, 'app_id': APP_ID, 'user_id': 42, 'username': 'alice', 'request_id': request}, **self.override.get(endpoint, {})}

    def handler(self):
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                status = 200
                try:
                    if self.headers.get('X-OuthMan-App') != APP_ID or self.headers.get('Authorization') != 'Bearer '+APP_SECRET:
                        raise AuthError('Invalid app', 401)
                    data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    endpoint = self.path.rsplit('/',1)[-1]
                    result = provider.transport(config(), endpoint, data)
                    if endpoint == 'enroll': status = 201
                except AuthError as error: result = {'error': str(error)}; status = error.status
                body = json.dumps(result).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *_): pass
        return Handler
