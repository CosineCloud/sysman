# OuthMan login for SysMan

SysMan now requires OuthMan authentication. The login page and a minimal health endpoint are public; dashboard HTML, agent controls, configuration, source upload, schedules, agent metadata, logs/downloads, code backups and documentation routes require an authenticated SysMan session. Missing configuration never permits anonymous dashboard access.

## Configure OuthMan first

1. In OuthMan, create a dedicated **SysMan** app under Connect your apps. Save its 16-hex app ID and once-visible 32-hex app secret.
2. Assign the app and intended operators to a shared access group. Every linked user gets full SysMan operator privileges over the shared agent workspace.
3. Approve operator accounts and have each operator sign into OuthMan to establish its required browser binding.
4. Add the SysMan backend's actual connecting IP/network to OuthMan's **API IP allowlist**. A Docker peer may be seen as the Pi or Docker gateway, depending on deployment. An empty OuthMan ACL denies all integration calls.
5. Operators generate the six-digit, 60-second code in OuthMan. SysMan does not generate OuthMan codes or use authenticator-app TOTP.

## Configure SysMan on the Raspberry Pi

From the checkout:

```sh
cp .env.example .env
chmod 600 .env
nano .env
```

Set real values locally:

```dotenv
OUTHMAN_BASE_URL=https://your-reachable-outhman-host
OUTHMAN_APP_ID=your_actual_16_hex_app_id
OUTHMAN_APP_SECRET=your_actual_32_hex_app_secret
SYSMAN_ALLOWED_USERS=your_canonical_outhman_username
```

The last three values above are descriptions, not usable credentials. Use OuthMan's generated ID/secret and actual canonical usernames. For multiple operators, use comma-separated usernames. Names are compared case-insensitively; do not use wildcard groups or a GitHub token. Keep `.env` private; it is excluded from Git and image builds.

Within Docker, `127.0.0.1` is the SysMan container, not the Pi or OuthMan container. Configure a reachable DNS name/IP. HTTPS is required for a non-loopback OuthMan URL by default. If OuthMan only serves HTTP on a trusted isolated LAN/bridge, explicitly set `OUTHMAN_ALLOW_INSECURE_HTTP=true`; the OuthMan password and app secret then travel in plaintext on that network. Prefer a TLS reverse proxy and trusted certificates. Do not disable TLS verification. Python uses the system trust store; for a private CA mount a CA bundle and set `SSL_CERT_FILE` on the SysMan service. The agent demo's CURL_CA_BUNDLE setting is independent.

For the supplied localhost/SSH tunnel deployment, leave these defaults:

```dotenv
SYSMAN_PUBLIC_ORIGIN=
SYSMAN_COOKIE_SECURE=false
```

The browser's HTTP connection is to its own localhost through SSH. If you place SysMan behind an HTTPS reverse proxy, configure its exact origin and secure cookies:

```dotenv
SYSMAN_PUBLIC_ORIGIN=https://sysman.example.com
SYSMAN_COOKIE_SECURE=true
```

The proxy must preserve that Host header. SysMan ignores untrusted forwarded headers and accepts only localhost/127.0.0.1 or the configured public origin. The supplied Compose file still publishes only on the Pi's loopback; expose a reverse proxy deliberately if you need a different entry point.

Pull and recreate the service:

```sh
git pull --ff-only
docker compose pull sysman
docker compose up -d sysman
```

Use the overlay equivalent if you run the demo backup agent. Environment changes require container recreation; refreshing the page is insufficient. The health endpoint remains available without a session for Docker checks. Healthy means the service runs, not that OuthMan setup is complete.

For file-based secrets, `OUTHMAN_APP_SECRET_FILE` is supported. Mount the actual secret file into the service at that path using a Compose override; adding a host path to `.env` does not mount it. When both are set, the file takes precedence. Never paste app secrets or user keys into browser forms or chat.

## First link, then sign in

1. Open SysMan's login page through your existing SSH tunnel.
2. Select **Link account**. Enter your existing OuthMan username/password and a freshly generated code, preserving leading zeros.
3. SysMan calls `/api/v1/enroll` from its backend, validates the app/user identity, and durably saves the encrypted user key and a local password hash. Linking does **not** sign you in.
4. Generate another fresh code in OuthMan. Select **Sign in** and enter the same username/password plus the new code.
5. SysMan creates a random pending login bound to the initiating browser. It calls `/verify`, checks the returned request ID and PIN expiry, then immediately calls `/confirm`. Only a matching app ID, immutable user ID, canonical username, request ID and boolean `verified: true` permit session creation.
6. Logout invalidates the current server-side session. Login rotates both the session cookie and CSRF token.

No password-only fallback exists. Denied access groups, suspended/unapproved accounts, invalid/expired codes, app revocation, malformed responses, wrong keys and outages do not grant a session. Do not automatically retry a consumed or uncertain code; generate a fresh one. A 409 enrollment response cannot recover a missing user key.

## Storage and session policy

- Enrolled bindings live in `/data/auth/accounts.sqlite3` (0600). User keys use authenticated Fernet encryption and are bound to provider URL, app ID, immutable user ID and canonical username. Passwords use salted scrypt hashes; plaintext passwords/codes/PINs stay in request memory only.
- The encryption key lives at `/auth-key/outhman.key` in a **separate persistent named volume**, generated once with 0600 permissions. Back up both volumes separately and preserve their pairing. If the database exists but the key is missing, or the key cannot decrypt bindings, login fails closed. It never silently generates replacement keys for existing accounts.
- Browser sessions and pending requests are memory-only. Restarting SysMan signs everyone out. Sessions expire after eight hours or 30 minutes without requests (dashboard polling counts as activity, not human interaction); pre-login browser sessions last ten minutes. Session cookies are HttpOnly, SameSite=Strict, Path=/ and Secure when HTTPS is configured. Responses are not cached.
- All browser mutations, including linking/login/logout, require the initiating browser's CSRF token. Authentication attempts are limited to ten per 15 minutes per peer IP/username. Operators behind the same backend/tunnel may share an IP quota.
- OuthMan app/user/group revocation prevents subsequent login. As the protocol specifies, it does not automatically revoke an already issued SysMan session. Remove an operator from `SYSMAN_ALLOWED_USERS` and recreate SysMan to immediately invalidate its sessions, or restart SysMan to invalidate all sessions. There is no remote OuthMan session introspection endpoint in this contract.
- Schedules continue to run in the server while users are signed out. Agent runner ID/key authentication remains separate and unchanged.

The application is a single shared operator workspace, not a multi-tenant authorization system. All allowed operators can control all registered agents. Restrict the OuthMan access group and SysMan allowlist accordingly. Preserve SSH/TLS transport protection; adding login does not turn the development HTTP server into an unrestricted public production deployment.

## Recovery and password changes

Restore the account database and its matching encryption-key volume together. Also preserve OuthMan's app credential separately. Replacing the app credential/provider or deleting bindings does not recreate OuthMan's once-visible user key. Lost enrollment responses require controlled recovery by the OuthMan administrator; the documented API has no key retrieval or self-service rotation endpoint. Do not repeatedly enroll in the hope of recovering a key.

If a user changes their password in OuthMan, the stored local hash must be synchronized by an operator with shell access:

```sh
docker compose exec sysman python3 auth_admin.py actual_canonical_username
```

The command prompts for the new current OuthMan password twice and updates only its local hash. It does not change or recover the encrypted user key and cannot bypass OuthMan: the next browser login still requires `/verify` and `/confirm` with a fresh code. Restart SysMan as well if you need to invalidate existing sessions immediately.

## Local development and tests

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Set the same environment variables securely, then run `python3 server.py`. The default local account database is `agents/auth/` and the separate key is `.sysman-secrets/outhman.key`. Both paths are ignored by Git. Docker uses Debian's architecture-native `python3-cryptography` package (including ARM64 and ARM/v7), avoiding x86-only wheels or an ARM Rust build.

```sh
python3 -m unittest discover -s tests -v
python3 tests/remote_smoke.py
```

The smoke test runs a temporary **mock OuthMan protocol server**, uses real HTTP calls for enrollment/verify/confirm and a CSRF-protected SysMan cookie session, then runs the real agent lifecycle/backup test and logout. It does not contact your production OuthMan. A real enrollment/login acceptance check still requires your actual reachable server, app credentials, approved operator, group access, ACL and fresh codes.
