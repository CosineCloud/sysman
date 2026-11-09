"""Local recovery: synchronize a linked user's password hash without changing its identity/key."""
import argparse
import getpass
import os
from pathlib import Path
from outhman_auth import AuthService, password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username', help='Canonical OuthMan username')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    data = Path(os.environ.get('SYSMAN_DATA_DIR', str(root/'agents')))
    service = AuthService(data/'auth', Path(os.environ.get('SYSMAN_AUTH_KEY_FILE', str(root/'.sysman-secrets/outhman.key'))))
    service.ready()
    account = service.lookup(args.username.strip())
    if not account: parser.error('No matching linked account; no records changed')
    password = getpass.getpass('New current OuthMan password: ')
    if not password or len(password.encode()) > 1024 or password != getpass.getpass('Repeat password: '):
        parser.error('Passwords did not match or were invalid; no records changed')
    with service.connect() as db:
        db.execute('UPDATE accounts SET password_hash=? WHERE id=?', (password_hash(password),account['id']))
    print('Local password hash synchronized. The next login still requires OuthMan verification and a fresh code.')

if __name__ == '__main__': main()
