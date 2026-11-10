# SysMan

A local Python agent control center with light/dark themes, process lifecycle controls, live numeric metrics, structured logs, JSON configuration and source updates with manual-retention backups.

## Run

Requires Python 3.10+ on macOS or Linux. Install `requirements.txt` for encrypted OuthMan account storage.

```sh
python3 -m pip install -r requirements.txt
python3 server.py
```

Open http://localhost:8765. Set `PORT` to choose another local port. Use Ctrl+C to shut down the platform and its workers.

The included Email router and Backup agent are stopped integration examples. Their default code emits work-cycle counts; replace it with your actual routing/backup implementation. Nothing sends email or copies files until you implement that logic.

Create agents through Add agent. Manage opens each agent’s own process health, counters and metric graphs. Configuration uses native form controls; raw JSON stays hidden until requested. Agent logs and the main Logs view offer full retained JSON downloads, with search and agent/level filters in the main view. Use agent details for source upload and backups. See [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md) for the runner contract and integration guide.

The server binds to loopback and rejects cross-origin API calls. OuthMan linking/login is mandatory; see [AUTHENTICATION.md](AUTHENTICATION.md) to configure credentials and allowed operators. Missing configuration locks the dashboard. Uploaded Python is trusted code running with your user's permissions.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Remote runner example

Run `python3 sample_agent/runner.py --init` once, save the generated ID/key, then run `python3 sample_agent/runner.py`. Register it with Add agent → Remote agent at `127.0.0.1:9101`. The example backs up actual demo files into ZIP archives. See [REMOTE_AGENTS.md](REMOTE_AGENTS.md) for full setup, HTTPS, curl protocol and verification.

## Raspberry Pi / Docker

See [deployment.md](deployment.md) to pull ARM64 or ARM/v7 images, run SysMan and the real backup-agent container, choose agent icons, and configure timezone-aware start/stop schedules. SysMan controls the worker inside each running runner container; it does not stop Docker containers.

## OuthMan login

Read [AUTHENTICATION.md](AUTHENTICATION.md) before upgrading: the first-start portal collects OuthMan settings using a setup token retrieved with `docker compose exec sysman python3 setup_admin.py token`. Then link an existing approved OuthMan account and sign in with a fresh code. Advanced environment configuration remains supported. Operator sessions protect all agent APIs, logs/downloads and code updates. Protect and back up the separate authentication-key volume together with the encrypted account database.
