---
description: Launch (or reload) the DASHR AI dev stack — Postgres/Redis/MinIO, the FastAPI backend, a Celery worker, and the Next.js frontend
allowed-tools: Bash, Read
---

Launch the full DASHR AI website locally so it can be exercised in a
browser or driven programmatically. This always reloads: any backend,
worker, or frontend process already running from a previous launch is
stopped first, so the running instance is guaranteed to be serving the
current code, not stale state from an earlier session.

## Step 1 — Run the launch script

Run:
```
bash scripts/launch-website.sh
```

This script (in `scripts/launch-website.sh`):
1. Stops any previously launched `uvicorn`/Celery worker/`next dev` process
   for this project (via PID files in `/tmp/dashr-dev/`, falling back to
   killing whatever is bound to ports 8000/3000), so nothing stale is left
   running.
2. Brings up `postgres`, `redis`, and `minio` via
   `docker compose -f infra/docker-compose.yml up -d` and waits for
   Postgres to accept connections.
3. Starts the API (`apps/api`, `uvicorn app.main:app`) in the background on
   port 8000, waiting for `/docs` to respond before continuing.
4. Starts a Celery worker (`apps/api`, `celery -A app.workers.celery_app
   worker -Q cards`) in the background, waiting for it to respond to
   `celery inspect ping` before continuing. Without this, uploaded cards
   enqueue for parsing/enrichment (via the "Parse Cards" button) but never
   actually get processed — the UI will look like nothing happened.
5. Starts the web app (`apps/web`, `next dev`) in the background on port
   3000, with `API_INTERNAL_URL` pointed at the API so the Next.js rewrite
   proxy works, waiting for the root route to respond.
6. Prints the URLs, log file paths, and the exact `kill` command to stop
   all three.

## Step 2 — Handle failures

If the script exits non-zero, it already printed the last 40 lines of the
failing service's log (`/tmp/dashr-dev/api.log`, `/tmp/dashr-dev/worker.log`,
or `/tmp/dashr-dev/web.log`). Read the fuller log with the Read tool if that
excerpt isn't enough to diagnose it — common causes are a missing/stale
`apps/api/.venv` (needs `pip install -r requirements.txt`),
`apps/web/node_modules` missing (needs `npm install`), or Docker not
running. Report the root cause rather than retrying the script blindly.

## Step 3 — Report to the user

On success, report in this exact format:
```
Frontend: http://localhost:3000
API:      http://localhost:8000 (docs at /docs)
Logs:     /tmp/dashr-dev/api.log
          /tmp/dashr-dev/worker.log
          /tmp/dashr-dev/web.log
Stop with: kill $(cat /tmp/dashr-dev/api.pid) $(cat /tmp/dashr-dev/worker.pid) $(cat /tmp/dashr-dev/web.pid)
```

Do not open a browser or take a screenshot unless the user asks — this
command's job is just to get the stack running and reachable.
