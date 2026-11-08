# Remote agents and a real backup example

SysMan now manages local agents and independently running remote runners. SysMan's backend calls **curl** for every remote snapshot, command, configuration save, code update, rollback and code-backup deletion. The browser talks only to SysMan; it never calls the remote runner directly.

## Run the example

Python 3.10+ on macOS/Linux and curl on the SysMan host are required. Run from this repository; the sample uses the shared SysMan supervisor and worker launcher, so keep the repository together when copying it to another host.

1. Initialize once:

   ```sh
   python3 sample_agent/runner.py --init
   ```

   This prints the generated **Agent ID** (8 hex characters = 32 bits) and **Security Key** (16 hex characters = 64 bits). Save the key now; the agent stores only its SHA-256 hash and does not display it again. Initialization never overwrites existing credentials. It also creates a sample source file and default configuration.

2. Start the independent runner in one terminal:

   ```sh
   python3 sample_agent/runner.py --port 9101
   ```

   The runner remains available while main.py is stopped or paused. Initially the worker is stopped. No login page or agent UI exists.

3. Start/restart SysMan in another terminal:

   ```sh
   python3 server.py
   ```

4. Open http://localhost:8765, select **Add agent → Remote agent**, and enter:

   | Field | Value |
   | --- | --- |
   | Name | File backup agent |
   | Agent ID | The actual 8-character value printed by initialization |
   | Security Key | The actual 16-character value printed by initialization |
   | IP address | `127.0.0.1` |
   | Port | `9101` |
   | Protocol | HTTP |

   SysMan checks the credentials and returned Agent ID before registering. An incorrect key is rejected. Registrations persist across SysMan restarts. Re-registering the same remote ID is rejected.

5. Click **Manage → Config**. The actual backup agent provides source/destination fields, an enable checkbox, interval input, compression dropdown, backup-mode radio buttons, include/exclude pattern lists, maximum file size and backup label. Relative paths resolve inside `sample_agent/`.

6. Click **Run agent**. Within a few seconds, see a ZIP file in `sample_agent/backup_output/`, counters/graphs in Manage, and a creation event in the agent and central Logs views. Edit `sample_agent/demo_source/welcome.txt`; another ZIP is created after the next scan. In on-change mode, unchanged content produces a skip log, not another archive.

7. Test pause/resume/stop/restart. Then upload a modified copy of `sample_agent/main.py` under Update. Previous source stays in the runner's `sample_agent/backups/` until explicitly deleted. Restore it from Backups to test rollback. A code rollback does not roll back configuration or file archives.

Each archive contains `files/<relative path>` plus `sysman-manifest.json`. Open the ZIP with your normal archive tool to inspect or extract it to a separate directory. This example creates file backups; it does not replace or restore existing files automatically, download OuthMan backups, or implement the encryption specification in `backup_feature.md`. ZIP archives are unencrypted. Source files, generated ZIPs and code backups are never automatically deleted.

## Identity and connection contract

- Agent ID: random 32-bit value, lowercase hex with leading zeroes preserved.
- Security key: random 64-bit value, lowercase hex with leading zeroes preserved.
- Runner receives `X-Agent-ID: <8hex>` and `Authorization: Bearer <16hex>` on **every** endpoint. Both must match. Comparisons use constant-time digest comparison. Twenty failed authentications per peer cause a 60-second cooldown.
- Runner stores only a hash in owner-readable `security.json`. SysMan must store the usable key in `agents/<local-id>/connection.json` to issue unattended requests; this file is owner-readable (0600), in a private directory. It is never returned to the browser or put on curl's command line. Protect this file in host backups.
- Keys are never sent in URLs. Curl receives headers and JSON through stdin, follows no redirects, validates HTTPS normally, bypasses environment HTTP proxies, and uses connection/total timeouts. No shell interpolation is used.
- The requested key length is 64 **bits**, not 64 characters. It is a shared secret rather than a user password. For non-loopback traffic use HTTPS; the sample requires a certificate/private key when binding beyond loopback. The Agent ID identifies the runner and is not a second secret factor.

For another machine:

```sh
python3 sample_agent/runner.py --host 0.0.0.0 --port 9101 --cert /path/to/cert.pem --tls-key /path/to/key.pem
```

Use that machine's actual IP in SysMan, choose HTTPS, and use a certificate valid for that IP and trusted by curl on the SysMan host. Do not use 0.0.0.0 as the connection address. Runner bind defaults to 127.0.0.1. Use host firewall rules to allow your SysMan host. Uploaded agent code runs with the runner's OS account permissions.

## HTTP protocol

All routes require the two headers. Responses are JSON. Core snapshot fields are `agent_id`, `status`, `metrics`, `history`, `config`, `config_schema`, `runtime`, `logs`, `backups`, and `error`. Snapshot logs contain the full retained history (up to 2,000). SysMan polls every two seconds and caches snapshots; failed polls mark the agent Offline while retaining last-known logs/metrics. Disconnected values are last-known values, not fresh health measurements. Closing SysMan stops polling but leaves remote workers running.

| Method | Runner endpoint | Body |
| --- | --- | --- |
| GET | `/api/agent` | Full snapshot and identity |
| GET | `/api/agent/logs` | Full retained logs |
| POST | `/api/agent/action` | `{"action":"start"}` or pause/resume/stop/restart |
| PUT | `/api/agent/config` | Configuration object |
| POST | `/api/agent/update` | `{"source":"...Python main.py..."}` |
| POST | `/api/agent/restore` | `{"backup":"actual-returned-name.py"}` |
| DELETE | `/api/agent/backups/<name>` | Permanently remove that retained code backup |

HTTP 401 means invalid credentials or temporary cooldown; 400 means invalid configuration/code/command; 404 means endpoint or backup missing. A failed remote call in SysMan displays a sanitized status; consult runner logs for work errors. Starting or stopping the child does not stop the HTTP runner. Worker shutdown waits up to five seconds then forces termination. On runner restart, the child remains stopped until started explicitly.

### Manual curl check

This prompts for your actual credentials, keeping them off argv and shell history:

```sh
python3 -c 'import getpass,json,sys; print("Agent ID: ",file=sys.stderr,end="",flush=True); i=input(); k=getpass.getpass("Security Key: "); print("header = " + json.dumps("X-Agent-ID: " + i)); print("header = " + json.dumps("Authorization: Bearer " + k))' \
  | curl --disable --fail --silent --show-error --noproxy '*' --connect-timeout 2 --max-time 15 \
      --config - http://127.0.0.1:9101/api/agent
```

Use the same headers for every command. Add `--request POST --header 'Content-Type: application/json' --data '{"action":"start"}'` and use `/api/agent/action` to start. No supplied keys or example placeholder credentials are valid until initialized.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 tests/remote_smoke.py
```

The second command starts temporary local agent and SysMan servers and uses actual curl calls to test authentication, remote registration, configuration, a real ZIP backup, lifecycle controls, source replacement, code restore, combined logs and manual code-backup deletion. It operates only on temporary test data and shuts down its servers.
