# Updating PacheckEssay on Linux (plain Docker)

These commands run on the Linux Docker host, inside its Git clone. No Compose configuration or API-key transfer is required. Python 3 must be installed on the host.

## First update

Finish or cancel any queued/running grading jobs, then run as the checkout owner:

```bash
cd ~/apps/PacheckEssay
git pull --ff-only
bash deploy/update.sh
```

Change the directory if you cloned it elsewhere. If your account needs elevated Docker access, the script requests sudo at the start; do not run the entire script as root over another user's checkout.

## Later updates

```bash
cd ~/apps/PacheckEssay
bash deploy/update.sh
```

The script pulls `main` from the PacheckEssay repository, builds the replacement image while the old containers run, and switches both processes with a brief web interruption. It only reports success after checking web health and a fresh worker heartbeat.

Defaults match the plain-Docker deployment commands:

- Web container: `pacheckessay-web`
- Worker container: `pacheckessay-worker`

For different names, use:

```bash
bash deploy/update.sh --web-container YOUR_WEB_NAME --worker-container YOUR_WORKER_NAME
```

List names with `docker ps --format '{{.Names}}'` (prefix with sudo if needed).

## What stays in place

- Your actual LAN address/port mapping, including **8005**.
- The existing persistent data volume or bind mount, including usage accounting and unexpired jobs.
- The worker's existing read-only API-key file mount; the script does not open the key file.
- Existing environment settings, including model, cost limits, and HTTP/HTTPS cookie configuration.

The script does not use `.env` as deployment input, delete volumes, prune images, or copy student data into backups. Temporary copies of container environment settings are private and removed after use.

## When it stops

- Git has local changes or is not on `main`: it refuses to overwrite your checkout. Move or commit the changes deliberately, then retry. `compose.local.yaml` and `.env` are ignored.
- Grading is queued/running: finish or cancel it, then retry. A final check after stopping web submissions protects against a new job arriving during the build.
- The Docker deployment uses an advanced network, devices, or resource settings unsupported by this updater: it stops before replacement; update that deployment manually.
- The image fails to build: existing containers remain running.
- New containers fail to start or become ready: the script restores the previous containers against the same data and exits with an error. If restoration itself fails, it explicitly reports that administrator action is needed.

The Git checkout remains at the pulled revision even if deployment fails. Container restoration does not undo future database migrations; read any migration instructions before updating to such a release.

The script has automated fake-Docker tests for configuration preservation, active-job races, build/start/readiness failures, and restoration. An actual Docker update cannot be verified on the development workstation; the first server run is the host smoke test.
