# API key setup and local testing

Your key stays on the machine running the grading worker. **Do not upload the key to GitHub**, paste it into an issue, put it in browser code, or commit it in `.env`. The web app does not need to read the key. The worker reads a private file; its environment variable contains the **file path**, not the key value. [Official OpenAI API authentication guidance](https://developers.openai.com/api/reference/overview)

## Windows: test locally without Docker

Keep a private copy of your key outside the repository, for example:

```text
C:\Users\YOUR_USER\.essay-checker\openai_api_key.txt
```

Create the directory and open the file using PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.essay-checker" | Out-Null
notepad "$env:USERPROFILE\.essay-checker\openai_api_key.txt"
```

Paste your existing key into Notepad, save it as plain UTF-8 text, and close the editor. The file should contain only the key. You can instead copy your existing key file into that directory through File Explorer, without displaying its contents in a terminal. Keep this directory outside MEGASync/OneDrive and other shared or backed-up project folders. Only your Windows account should have access.

From your existing `WebApp` folder, or a clone of `PacheckEssay`, open PowerShell and run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ESSAY_DATA = "$PWD\data"
$env:SECURE_COOKIE = "false"
.\.venv\Scripts\python.exe -m uvicorn essay_web.app:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). Uploading and estimating do not send paid API requests. Leave this terminal open.

To enable actual grading, open a **second PowerShell window in the same project folder**:

```powershell
$env:ESSAY_DATA = "$PWD\data"
$env:OPENAI_API_KEY_FILE = "$env:USERPROFILE\.essay-checker\openai_api_key.txt"
.\.venv\Scripts\python.exe -m essay_web.worker
```

The worker sends paid requests for submitted jobs, including jobs already waiting in the queue. Keep both terminals open; Ctrl+C stops each process. Local runtime files go into the ignored `data` directory. It still contains sensitive student information during the retention window, so use synthetic responses for the first test.

## Docker: keep the key on the Docker host

On the Docker host, create a private key file outside the clone, for example `/srv/essay-secrets/openai_api_key`. The file contains only the key. Use a local editor or copy the file over SSH/SFTP directly to that host; never transfer it via GitHub. For the Linux container's UID 10001, the README describes file ownership and permissions.

From the cloned project folder, copy `.env.example` to `.env` and set **only the host path**:

```dotenv
OPENAI_SECRET_FILE=/srv/essay-secrets/openai_api_key
```

For Docker Desktop on Windows, use the Windows host path with forward slashes, such as:

```dotenv
OPENAI_SECRET_FILE=C:/Users/YOUR_USER/.essay-checker/openai_api_key.txt
```

Then start with `docker compose up -d --build`. Compose mounts the file read-only into the worker at `/run/secrets/openai_api_key`. The key is not baked into the image or mounted into the web service. Neither GitHub Actions secrets nor GitHub Pages are used by this deployment.

The Docker configuration expects HTTPS and secure cookies. For a **localhost-only HTTP Docker test**, create a temporary `compose.local.yaml`:

```yaml
services:
  web:
    environment:
      SECURE_COOKIE: "false"
```

Start with `docker compose -f compose.yaml -f compose.local.yaml up -d --build` and open port 8001 locally. Return to the base Compose configuration with secure cookies when enabling HTTPS through your proxy/tunnel. Do not add the local override to your production deployment.

## Verify before a future push

```powershell
git status --short
git check-ignore data/example.csv .env secrets/example.txt openai_api_key.txt
```

The current ignore rules exclude these paths, but they cannot detect every arbitrary filename. Keep the actual key outside the repository even when it is ignored. If you ever commit a real key, revoke/rotate it in the provider dashboard; deleting it from a later commit does not remove it from Git history.

No key is needed to clone this repository or run the offline tests. See [README](README.md) for models, cost exports, retention, Docker deployment, and staging validation.
