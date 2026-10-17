# PacheckEssay — Docker web app

For your first local run, see [API key setup and local testing](KEY_SETUP.md). Keep the key outside this repository. Copying the code to GitHub does not transfer the key or deploy the application.

## Updating an existing plain-Docker deployment

On your Linux Docker host, finish or cancel queued/running grading jobs, then run from this repository:

```sh
git pull --ff-only
bash deploy/update.sh
```

The initial `git pull` obtains the new updater. On subsequent updates, just run `bash deploy/update.sh`: it pulls `main` from `cobeng13/PacheckEssay`, executes the freshly pulled updater, builds before downtime, and replaces both containers. Python 3 is required on the host; no Python packages or Docker Compose are needed. Run it as the checkout owner. It requests sudo for Docker if your account cannot access the daemon directly.

Defaults are `pacheckessay-web` and `pacheckessay-worker`, matching the plain-Docker setup. If you used different names:

```sh
bash deploy/update.sh --web-container YOUR_WEB_NAME --worker-container YOUR_WORKER_NAME
```

The updater reads existing Docker configuration rather than loading `.env`. It preserves the actual published address/port (including LAN port **8005**), environment settings, persistent `/data` mount, read-only key-file bind mount, restart policy, and supported security options. It does not open the key file, print environment values, delete volumes, prune images, or back up student content. Temporary environment files have private permissions and are removed after use. It requires the standard bridge network; advanced networks/devices/resource limits require a manual deployment and are rejected before the switch.

The app must be idle. New submissions during the image build cause the update to stop before interrupting the worker. Expect brief web downtime during the switch. Readiness requires a fresh heartbeat from the replacement worker and a successful web health response. If replacement startup fails, the updater restores the previous containers against the same persistent data and exits with an error. It never rewinds the Git checkout; fix the problem and rerun. Automatic container restoration does not reverse future database migrations, so review migration instructions for future releases. Docker itself is required for a real host smoke test; the automated updater tests simulate Docker failure/recovery without accessing keys or running paid requests.

Standalone adaptation of the private Tkinter essay checker. `Reference` is unchanged. The browser UI uses FastAPI and Jinja; a separate Python worker processes persistent SQLite jobs. No faculty accounts, payment processing, or public job directory.

## Deploy on your Docker server

1. Copy this entire `WebApp` directory to the server. Use Docker Engine with Compose v2.
2. Create a key file **outside this project**, for example `/srv/essay-secrets/openai_api_key`. Put your OpenAI project key into that file using a local editor. Do not paste it into commands, Compose files, Git, or browser forms. The container runs as UID 10001: make the file readable by that UID only, e.g. ownership `10001:10001`, mode `0400`, inside a host directory accessible only to the administrator. Docker Compose file secrets are read-only bind mounts, not encrypted secret storage; host filesystem permissions and disk protection remain important.
3. Copy `.env.example` to `.env`, set `OPENAI_SECRET_FILE` to that file's absolute path, and configure the trusted reverse proxy IP. `.env` contains paths/settings, not the key itself.
4. Start from this directory:

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Only the worker receives the key mount. The image build copies an allowlist of source files; student data, key files, test runs, and the desktop application are excluded.

The web port defaults to `127.0.0.1:8001`; put it behind your existing HTTPS reverse proxy. For nginx on the same host, the location block can use:

```nginx
location / {
    client_max_body_size 6m;
    proxy_pass http://127.0.0.1:8001;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

Set `TRUSTED_PROXY_IPS` to the actual source IP that uvicorn sees for this proxy (often the Docker bridge gateway); do not trust arbitrary public proxy headers. If nginx is itself in Docker, attach the web service to its private network and proxy to `web:8000` instead. Keep the web service off the public network. Secure cookies require HTTPS. Origin checking uses the forwarded scheme and host, so verify a file submission through the real proxy before launch.

The worker uses an OS-level file lock: keep **one worker** and one shared data volume on a local filesystem. SQLite WAL is unsuitable for a shared network filesystem or independent hosts. Web and worker can restart without losing queued jobs. `/health` returns only `ready` or `worker_unavailable` and checks a recent worker heartbeat.

## Faculty workflow

1. Enter faculty name and optional department, course, and assessment. Upload CSV, JSON rows, or an HTML table. Preview the first ten rows.
2. Review identity columns, select response columns, add/remove questions, and select multiple columns to merge into one answer.
3. Enter rubrics, point allocations, model answers, and instructions. Load `.txt` files or download/reload a rubric preset JSON.
4. Select a model, estimate usage, and start. The API key is never requested by the form.

Save the private job link to return later. Its token is in the URL fragment, not the query string or reverse-proxy access log. Requests send it in `X-Job-Token`; anyone holding the link can use that job until it expires. Browser drafts are isolated by an HttpOnly, SameSite cookie. No public listing exists. Faculty attribution is self-reported and is not proof of identity. Do not share a public unrestricted URL if you require verified billing identity.

Closing the browser does not stop grading. Stop takes effect after the current API request. Resume reuses successful results, only processes missing/failed work, and rejects changed source content or grading settings. A network timeout, HTTP 5xx, or interrupted in-flight request is uncertain and is **not automatically retried**. The faculty must explicitly acknowledge possible duplicate provider charges before retrying uncertain work. HTTP 429 uses at most three attempts with backoff; permanent credential/model errors stop the batch. There is no automatic model fallback.

Downloads contain `gradebook_final.csv`, per-question `ForLMSUpload` CSVs, summary CSV, and selected student/question JSON/text reports. Duplicate student IDs are kept as separate input rows. Review any duplicate LMS identifiers before importing. Spreadsheet formula prefixes in exported text cells are escaped.

Model scores and flags are assistance for instructor review. In particular, a generated plagiarism flag is not an independent plagiarism test.

## Models and accounting

Explicit capability definitions and a versioned Standard-text rate card live in `essay_web/models.py`. GPT-6 Luna is the economical candidate; **GPT-4.1 mini remains the initial default until the staging comparison below passes**. Set `DEFAULT_MODEL=gpt-6-luna` after that comparison. GPT-5.6 Luna/Terra remain selectable. Verify account access and rates before deployment; changing the catalog requires a code update/rebuild. Existing attempts preserve their original price snapshots.

Documentation consulted:

- [GPT-6 Luna capabilities and pricing](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [API authentication](https://developers.openai.com/api/reference/overview)

Every outbound attempt is inserted before the SDK is called. SDK retries are disabled. The ledger records faculty labels, opaque job/task IDs, effective settings, timestamps, elapsed time, provider IDs, token usage, status, and pricing. It never stores prompts, student names, essays, rubric text, feedback, or raw exception messages in accounting rows. Token usage is committed before grade validation, so invalid/refused/truncated outputs retain any returned billable usage. Reasoning tokens are part of output tokens, not an extra charge. Cached tokens are charged as a subset of input; cache writes use a separate rate where supported. Unknown rates/usage remain unknown instead of zero. No money is automatically collected.

Export usage from a trusted administrator terminal:

```sh
docker compose exec web python -m essay_web.admin export-usage --output /tmp/usage --from-date 2026-10-01 --to-date 2026-11-01
docker compose cp web:/tmp/usage ./reports
```

Dates are UTC; `--to-date` is exclusive. Omit dates to export all accounting history. `requests.csv` contains individual attempts; `summary.csv` groups faculty/date/model with known USD cost and unknown-cost counts; `events.csv` distinguishes empty answers and resume skips. Failed attempts with known usage retain their cost. These exports support manual charging and are unavailable through web routes.

If an uncertain cost can be verified from provider records, an administrator can record it:

```sh
docker compose exec web python -m essay_web.admin reconcile-cost --attempt 123 --usd 0.0012
```

This preserves the original request status. It is allowed only for a non-active attempt with an unknown cost. The export is a calculated provider-cost ledger, not a provider invoice: reconcile unknowns and any pricing changes before charging faculty.

## Limits, retention, and backups

Defaults: 5 MB upload, 2,000 student/question tasks per batch, 50 questions, 20 queued/running jobs, 100 unsubmitted drafts, 60 HTTP requests per source IP per minute, 8,192 output tokens per API attempt, and $5 daily UTC budget. Environment settings in `.env.example` change these limits. Each grading prompt has a conservative 60,000-byte/token-bound ceiling to avoid long-context pricing. Requests reserve a conservative cost ceiling before dispatch; known costs release the unused reservation. Unknown costs keep their reservations until reconciled. Limits apply globally; a shared campus IP shares the HTTP rate limit. Faculty name is never used as a security boundary.

Web and worker run cleanup independently. Drafts expire 24 hours after upload. All uploaded/student/result content, intermediate exports, and download ZIPs expire 24 hours after the job finishes, fails, or is cancelled. Expired content is denied by routes immediately and removed by cleanup within approximately one minute when the web process runs. Worker-only cleanup runs between jobs. If all services are down, physical deletion occurs after startup. Queued/running job content is kept until the job reaches a terminal state. Resume resets the terminal expiry when it finishes again.

The persistent `essay-data` volume contains both temporary content and the operational SQLite database, whose task rows contain grading results. **Do not back up/snapshot that volume**, its WAL files, `content`, or `/tmp`. Back up only administrator accounting CSV exports and sanitized source/configuration. SQLite secure deletion and WAL checkpointing reduce deleted-content remnants; they do not guarantee forensic erasure on SSDs or remove host snapshots. Existing host-level backup policies must exclude this volume. Accounting CSVs retain faculty labels but no student data. Accounting records remain until the administrator deletes them.

Manual cleanup:

```sh
docker compose exec web python -m essay_web.admin cleanup
```

## Local development and offline tests

Python 3.11+ (Docker uses Python 3.12):

```sh
python -m venv .venv
# Activate the environment for your shell.
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

For a local UI preview without a key/worker, set `ESSAY_DATA` to a temporary writable directory and `SECURE_COOKIE=false`, then run:

```sh
uvicorn essay_web.app:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

Uploads/estimates work; submitted jobs wait in the queue. Running `python -m essay_web.worker` requires `OPENAI_API_KEY_FILE` and causes paid requests for queued jobs. Never start it against real essays during an offline test.

## Staging checklist and model comparison

- Build the image, validate Compose, and check both service processes as UID 10001. Ensure the worker can read the mounted key and the web service cannot.
- Confirm HTTPS cookies, proxy headers, a successful upload, health, private job links, and inaccessible data/secret paths. Verify wrong/missing tokens fail.
- Submit synthetic data, restart containers while queued, and confirm resume behavior. Exercise a controlled interrupted request; verify explicit uncertain-call acknowledgement.
- Select representative de-identified instructor-scored responses, including empty, partial, off-topic, borderline, multilingual, and high-scoring answers. Include at least 30 responses across the actual rubrics used.
- Run identical batches explicitly on GPT-4.1 mini and GPT-6 Luna in staging. These are paid API calls and must be initiated deliberately with approved samples. Export gradebooks and request usage.
- Use the comparison tool below to calculate mean absolute score error, score agreement within 10% of rubric maximum, cost, and median latency. Review feedback side by side against the rubric. Require Luna's mean absolute error to be no worse than the baseline, agreement within tolerance at least 90%, no material feedback/scoring defects, and lower cost per successful response. Otherwise retain the baseline.
- After instructor approval, change `DEFAULT_MODEL`, restart services, and pilot with a small faculty batch. Review known/unknown costs and expiry before broader use.

Comparison tool, using opaque sample IDs and manually assembled instructor/model scores:

```sh
python -m essay_web.compare --scores staging-scores.csv --requests reports/requests.csv --output comparison.json
```

Score CSV columns: `sample_id,max_points,instructor_score,gpt-4.1-mini,gpt-6-luna`. One row per student/question. Supply a request export covering only these staging runs. Output includes numeric gates; human feedback review is separately required. No tool sends API requests automatically.

No server address, SSH credentials, real API calls, or instructor reference grades were available during implementation. Production deployment and the comparison therefore require completion on your server.
