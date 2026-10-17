# Implementation verification — 2026-10-16

- 35 offline tests passed on Windows / Python 3.11, FastAPI 0.115.12, OpenAI Python SDK 2.0.0. All provider calls were mocked; an actual SDK Response object was also exercised.
- Browser JavaScript passed `node --check`; Python modules passed compilation.
- Local browser verification completed: synthetic CSV upload, preview, automatic identity/response mapping, rubric configuration, model selection controls, usage estimate, and enabled submission button. Browser console reported no errors. The development screenshot is retained with the local delivery, outside this source repository.
- Verified queue claim exclusivity, pre-dispatch ledger writes, all four models' routing, duplicate student IDs, blank answers, merged columns, private sessions/tokens, origin rejection, CSV formula escaping, resume skips, changed-configuration rejection, unknown-call acknowledgement, restart recovery, budget enforcement, expiry, orphan cleanup, accounting exports, and offline model comparison.
- Compose YAML parsed successfully; checked that the web service receives no API-key secret mount. The image context is an allowlist of application source and requirements.

Docker is not installed on this workstation, so an actual image build and container/proxy smoke test have not been run. No server was deployed, real API key read, paid API request made, or real student record opened. Instructor-scored model evaluation remains a staging requirement; GPT-4.1 mini is the initial default and GPT-6 Luna is selectable.

See README.md for setup, manual usage exports, retention/backups, and the staging checklist. Desktop `Reference` files are unchanged.

## Plain-Docker updater — 2026-10-17

Added `deploy/update.sh` and its standard-library Python helper, following the scheduler's pull/build/readiness pattern. The combined suite passed **52 tests**, including 17 updater checks with fake Docker and Bash syntax validation. Tests verify configuration preservation, active-job races, build/start/readiness failures, temporary environment cleanup, and restoration of previous containers. No real Docker deployment, key-file access, or paid API call was performed. See `UPDATE.md` for Linux server usage and limitations.
