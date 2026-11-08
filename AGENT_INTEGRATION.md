# Building agents for SysMan

SysMan supplies the UI, process controls, metrics, configuration, logs, updates and backup management. Agents do not need a login screen or web server.

## Agent layout

Each agent lives in `agents/<id>/`:

- `main.py`: your Python functions and `run(ctx)` entry point.
- `config.json`: persistent JSON object, edited in SysMan.
- `logs.json`: persistent structured logs, managed by SysMan.
- `agent.json`: platform metadata (id, name, description).
- `backups/`: previous main.py versions, retained until manually deleted.

The shared `agent_runner.py` is the runner for each agent. The supervisor launches it as a separate process; it imports that agent's main.py. The supervisor receives UI commands, manages code replacement and backups, and restarts the runner. Keeping updates outside the worker allows replacement even when agent code is broken.

## Required contract

```python
def run(ctx):
    ctx.log('Starting email router')
    processed = 0
    while not ctx.stopping.is_set():
        config = ctx.config
        # Do a bounded unit of work here. Use timeouts on network requests.
        processed += 1
        ctx.metrics(processed=processed, pending=0)
        if ctx.wait(float(config.get('interval', 5))):
            break
    ctx.log('Stopped')
```

`ctx.config` loads current configuration. Validate its values in your agent. `ctx.log(message, level='info')` sends structured logs. `ctx.metrics(**numeric_values)` publishes real counters and gauges for the dashboard. `ctx.wait(seconds)` sleeps until the next cycle or a stop request. `ctx.stopping` is a threading Event. Plain stdout is also captured as a log; use the context API for structured events.

Use `try/finally` to close resources. Keep imports free of long-running work. Do not launch detached children. Install your dependencies in SysMan's Python environment; the runner uses the same interpreter. Agents share local-user filesystem permissions; they are trusted code, not sandboxed plugins.

## Lifecycle

Start imports main.py in a fresh runner. Pause/resume use POSIX process-group signals, freezing all execution rather than checkpointing it. Stop requests cooperative shutdown, waits up to five seconds, then kills the process group. Restart stops and imports the current source again. Supported hosts are macOS and Linux.

No automatic start occurs when the platform restarts. Unexpected exits show an error and require an explicit start. Metrics/history are session data. Config and logs persist; logs retain the latest 2,000 entries, with the latest 200 available to the dashboard.

## Updates and rollback

Upload a UTF-8 Python source file from the agent details panel. SysMan parses the source and requires a top-level `def run(ctx)` before stopping the agent. It saves the previous main.py in `backups/`, atomically replaces main.py, and restores the prior active/paused state. Stopped agents remain stopped. Syntax validation does not prove runtime correctness; inspect logs after an update.

Restore a backup from the Backups view. Restoration also backs up the current source, so it can be undone. Backups are never automatically purged. Explicit deletion is permanent. Only main.py is versioned: configuration, dependencies, external data and imported helper modules are not rolled back.

## Connecting an existing agent

1. Add an agent in SysMan.
2. Refactor your script's loop into `run(ctx)` and replace UI code with context logs and metrics.
3. Put serializable settings into the configuration editor. Keep secrets in environment variables rather than config/log messages.
4. Upload main.py, configure settings and start.
5. Verify counters and logs, then test pause, resume, stop and restore.

SysMan supports local processes and authenticated remote runners. Remote agents are registered with a 32-bit ID, 64-bit security key, name, IP address and port. See [REMOTE_AGENTS.md](REMOTE_AGENTS.md) for the curl protocol and runnable file-backup example.

## Native configuration forms

SysMan renders configuration as form controls by default. JSON is available only through the **Show JSON** button. Existing settings without schema metadata are inferred from their stored values, including nested objects and editable arrays.

To provide friendly labels, dropdown options, radio choices, and numeric limits, declare a literal `CONFIG_SCHEMA` dictionary at the top level of `main.py`:

```python
CONFIG_SCHEMA = {
    "type": "object",
    "required": ["interval"],
    "properties": {
        "interval": {"type": "number", "title": "Interval (seconds)", "minimum": 1},
        "enabled": {"type": "boolean", "title": "Enable notifications"},
        "destination": {"type": "string", "title": "Destination folder"},
        "mode": {"type": "string", "enum": ["incremental", "full"], "title": "Backup mode"},
        "compression": {"type": "string", "enum": ["none", "gzip"], "format": "radio"},
        "folders": {"type": "array", "items": {"type": "string"}, "title": "Folders"}
    }
}
```

Initialize the corresponding values in `config.json`. The metadata must be a literal dictionary, not dynamically computed. SysMan reads it without importing the agent. The supported schema subset includes type, properties, items, enum, required, minimum, maximum, minLength, maxLength, minItems and maxItems validation, plus title, description and radio presentation hints. Additional configuration keys are preserved. Your code must actually consume these settings; declaring a field does not implement its behavior.

Manage opens a dedicated per-agent overview with process state, PID, uptime, retained log/error counts, latest metric timestamp, numeric counters and individual metric histories. Metrics reset on each new process run and are not used as a liveness heartbeat. A running process can be idle or stalled; the dashboard reports process state rather than claiming business-level success.

Agent Logs and the main Logs section use the same persisted entries. Download JSON through either view to export all retained logs (up to 2,000 per agent). Global downloads add agent_id and agent_name. Deleting code backups does not delete logs.
