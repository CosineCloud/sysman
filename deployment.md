# Deploy SysMan on Raspberry Pi with Docker

## Supported Raspberry Pi systems

Use Raspberry Pi OS / Debian on a Raspberry Pi with ARMv7 or ARM64 support. A 64-bit OS on a Pi 3, 4 or 5 is recommended. Pi 1 and original Pi Zero ARMv6 systems are not supported by these images.

Published images contain `linux/arm64`, `linux/arm/v7` and `linux/amd64` manifests. Docker selects the image matching the Pi's OS architecture. SysMan and the sample agent use Python's standard library, curl, OpenSSL and Debian timezone/CA packages; there are no pip wheels or x86-only libraries. Extra dependencies you add to your own agents must support that agent's architecture.

Check your system:

```sh
uname -m
docker version
docker compose version
```

`aarch64` uses ARM64; `armv7l` uses ARM/v7. If Docker is not installed, follow Docker's [Debian installation guide](https://docs.docker.com/engine/install/debian/) for 64-bit Raspberry Pi OS, or the applicable Docker Raspberry Pi OS installation guide for 32-bit systems. Install the Compose plugin as well. Prefix Docker commands with `sudo` if your account lacks Docker access.

## Published images

The repository's **Test and publish containers** workflow tests the application and builds/publishes both images when main changes:

- `ghcr.io/cosinecloud/sysman:latest` — SysMan dashboard and scheduler.
- `ghcr.io/cosinecloud/sysman-agent:latest` — independent sample backup runner and worker.

Wait for the workflow to succeed before pulling a new image. Source push and image publication are separate operations. Immutable `sha-...` tags are published alongside latest; use a verified SHA tag to pin a deployment.

GitHub Container Registry packages are private on first publication, even for a public source repository. For anonymous pulls, the owner must set each package's **Package settings → Change visibility → Public** once. Alternatively authenticate on the Pi using a **personal access token (classic)** with `read:packages`. Your GitHub account password and fine-grained tokens cannot be used for this GHCR login.

1. Open [Create a classic token with read:packages](https://github.com/settings/tokens/new?scopes=read:packages).
2. Give it a name, set an expiration, select **read:packages**, and generate it. The token owner must have access to the packages. Authorize the token for SSO if your organization requires it.
3. On the Pi, run the command below. Use the GitHub username of the account that created the token; `CosineCloud` applies only if that is the token owner's account. A Raspberry Pi/Linux username or an organization name is not a substitute.
4. At `Password:`, paste the **token**, not your GitHub password. Nothing appears while pasting; press Enter. Success prints `Login Succeeded`.

```sh
docker login ghcr.io --username CosineCloud
```

After login, verify access:

```sh
docker pull ghcr.io/cosinecloud/sysman:latest
docker pull ghcr.io/cosinecloud/sysman-agent:latest
```

If login returns `denied: denied`, check that the token is classic, unexpired and has read:packages, and that the username matches its owner. If login succeeds but pulling fails, check that the owner has access to both packages and any required organization SSO authorization. Alternatively, make both packages public and pull without logging in.

Do not share your token in chat or put it into this repository, compose files or shell command arguments. See GitHub's [Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## Run SysMan

On the Pi:

```sh
git clone https://github.com/CosineCloud/sysman.git
cd sysman
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8765/api/health
```

The UI is bound to the Pi's loopback address. From your laptop, replace `pi-user@PI_IP` with your Pi's actual SSH username and IP:

```sh
ssh -L 8765:127.0.0.1:8765 pi-user@PI_IP
```

Keep that SSH session open and visit http://localhost:8765 on the laptop. If you use a desktop browser on the Pi, open the same URL directly there.

SysMan is a single-user local control service with trusted Python-code upload. The supplied deployment deliberately uses SSH tunneling rather than exposing an unauthenticated administrative UI on the LAN or internet. Docker does not need privileged mode or the host Docker socket. The dashboard uses the `sysman-data` named volume for agent registrations, keys, schedules, configurations, logs and code backups.

## Add a complete Dockerized test agent

This optional overlay runs the backup agent on a private Docker network using HTTPS. Its data, updated main.py, code backups and ZIP files are in a persistent `agent-data` volume. A separate volume stores the demo TLS certificate/private key.

From the repository directory on the Pi:

```sh
docker compose -f compose.yaml -f compose.demo.yaml pull sysman backup-agent
docker compose -f compose.yaml -f compose.demo.yaml run --rm cert-init
docker compose -f compose.yaml -f compose.demo.yaml run --rm agent-init
```

Save the **Agent ID** and **Security Key** printed by `agent-init`; the key is shown once. Initialization refuses to replace existing credentials. `cert-init` generates a one-year self-signed demo certificate and a CA bundle that retains the normal system roots. Run the initialization commands before starting the services:

```sh
docker compose -f compose.yaml -f compose.demo.yaml up -d sysman backup-agent
docker compose -f compose.yaml -f compose.demo.yaml ps
```

In SysMan, choose **Add agent → Remote agent**:

| Field | Value |
| --- | --- |
| Name | Raspberry Pi backup agent |
| Agent ID | Actual printed 8-hex-character ID |
| Security Key | Actual printed 16-hex-character key |
| IP address | `172.30.86.3` |
| Port | `9101` |
| Protocol | HTTPS |
| Icon | Your preferred icon |

The static IP avoids changing the saved connection when Compose recreates the agent. If `172.30.86.0/24` overlaps your network, change both Compose IP addresses/subnet and the certificate SAN in `docker/create-demo-certs.sh` **before initialization**. The sample runner is not published to the host or internet. Do not use `127.0.0.1` in the SysMan connection form for this overlay: inside its container, that address refers to SysMan itself.

Click **Manage → Run agent**. Counters, graphs and log messages should appear. Inspect the output:

```sh
docker compose -f compose.yaml -f compose.demo.yaml exec backup-agent ls -lh /data/backup_output
```

Trigger another real backup:

```sh
docker compose -f compose.yaml -f compose.demo.yaml exec backup-agent python3 -c 'from pathlib import Path; p=Path("/data/demo_source/welcome.txt"); p.write_text(p.read_text()+"Changed from Docker\n")'
```

The next scan creates a new ZIP. Main.py updates and rollback in SysMan modify the copy in `/data`, so they survive container recreation. The image's bundled main.py seeds a new volume only; pulling a newer image preserves your existing code and credentials. Updating the runner itself requires pulling a new image and recreating the container.

For actual host files, add a read-only bind mount to `backup-agent`, such as `/home/pi/Documents:/source:ro`, and configure Source folder as `/source`. Keep the backup destination in `/data/backup_output` or mount a separate writable destination. Named volumes preserve data when containers are recreated; keep an off-device copy for disk-loss recovery. The sample ZIPs are unencrypted.

## Icons and automatic schedules

Choose an icon during registration or in **Manage → Appearance**. It is stored in SysMan and survives restarts.

Under **Manage → Schedule**, enable scheduling, choose an IANA timezone, weekdays and start/stop times. Weekdays refer to the **start** day; a stop time earlier than the start time ends the next day. For example, Monday 22:00–06:00 means Monday night through Tuesday morning. Next action, last action and last error are shown in the workspace.

SysMan checks enabled schedules every 15 seconds. Schedules define an authoritative work window:

- Inside the window, a stopped worker is started; an explicitly paused worker stays paused.
- Outside the window, a running or paused worker is stopped.
- Manually stopping inside an enabled window lasts until the next check. Disable the schedule first for a lasting manual override.
- Disabling a schedule leaves the worker in its current state.
- After downtime/reconnection, SysMan applies the current window once rather than replaying missed actions. Offline agents are retried at later checks, with the error shown in the workspace.
- DST: repeated local times use their first occurrence; a missing time moves to the first valid minute. If this collapses a window to zero length, that day's window is skipped.

**The schedule controls main.py inside the agent container. The authenticated runner and container stay running so they can receive the next start command.** SysMan must also remain running for its schedules to execute. Stopping the entire agent container makes its runner unreachable; Docker-container start/stop scheduling is not part of this implementation and would require a separate Docker API controller.

`restart: unless-stopped` restarts containers after an ordinary host reboot, but it is not a calendar scheduler. If you manually stop a container, start it again with Compose. A restarted agent runner initially leaves its worker stopped; an enabled SysMan schedule starts it when the current work window permits.

## Updates, logs, recovery

SysMan alone:

```sh
git pull --ff-only
docker compose pull
docker compose up -d
docker compose logs --tail=100 sysman
```

With the demo agent:

```sh
git pull --ff-only
docker compose -f compose.yaml -f compose.demo.yaml pull sysman backup-agent
docker compose -f compose.yaml -f compose.demo.yaml up -d sysman backup-agent
docker compose -f compose.yaml -f compose.demo.yaml logs --tail=100
```

Agent work logs are available in SysMan's Logs UI and download buttons. Container logs show runner/service startup messages. Use `docker compose stop` (or the overlay equivalent) to stop containers without removing volumes. Do not use `down -v` unless you intend to erase the stored data and credentials.

Keep copies of both named data volumes and the certificate volume when backing up the deployment. SysMan's remote connection file contains the usable key, while the runner keeps its hash. Retain old code backups until you manually delete them. Pin an older published SHA tag and recreate containers to roll back an image; use Manage → Backups to roll back an agent's main.py independently.

## Build locally if registry access is unavailable

On the Pi, from the source checkout:

```sh
docker compose build sysman
docker compose up -d --pull never
```

For the agent, build its target too:

```sh
docker compose -f compose.yaml -f compose.demo.yaml build sysman backup-agent
```

Then follow the initialization sequence above, adding `--pull never` to `run` commands when offline. Debian package downloads are needed during the image build. Docker builds for the Pi's architecture locally; no architecture-specific Python dependencies need compilation.

## Troubleshooting

- **denied/unauthorized when pulling:** package is private; make it public once or log in with a PAT that has read:packages.
- **no matching manifest:** ensure the workflow completed and that the Pi OS is ARM64 or ARM/v7, not ARMv6. Inspect with `docker buildx imagetools inspect ghcr.io/cosinecloud/sysman:latest`.
- **Offline agent:** confirm backup-agent is running, setup initialized its data/certificates, and the saved endpoint is HTTPS at 172.30.86.3:9101.
- **TLS error:** initialize certificates and use the overlay for SysMan so its CA bundle is mounted. Demo certificates expire after one year. Replace certificates deliberately and restart both services; do not disable certificate verification.
- **No automatic run:** check Schedule enabled, selected weekdays, timezone and next action; keep SysMan and the runner container active.
- **Permission denied on bind-mounted paths:** containers run as UID/GID 10001. Grant that UID read access to the source and write access to the destination, or use the supplied named volumes.

Reference: [Docker volumes](https://docs.docker.com/engine/storage/volumes/), [restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/), and the official [Python image architecture manifest](https://github.com/docker-library/official-images/blob/master/library/python).
