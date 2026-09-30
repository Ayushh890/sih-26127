# Deployment

NIRNAY runs in three ways: a full Docker Compose stack, a local development mode without
Docker, and a split production layout. They all use the same code. Only configuration
differs.

> **What was verified here.** The development mode (SQLite, in-process bus, embedded
> workers, six demo cameras) was run and tested on a 2-CPU / 4 GB Linux machine. Docker,
> PostgreSQL and Redis were not available there, so the Compose stack, the Redis bus and a
> live PostgreSQL database have **not** been exercised. The Alembic migration is tested by
> upgrading and downgrading SQLite and checking it against the models; for PostgreSQL only
> the generated SQL (including the PostGIS parts) is tested, offline. Run the Compose stack
> once in your own environment before a production rollout.

## 1. Quick start

```bash
./run.sh          # Docker Compose if available, otherwise dev mode
./run.sh docker   # force the Compose stack
./run.sh dev      # force dev mode (no Docker)
./run.sh test     # backend pytest + frontend typecheck and vitest
./run.sh stop     # stop the Compose stack
```

On Windows use `run.bat` with the same arguments.

On first run the launcher creates `.env` from `.env.example` and generates a random
`JWT_SECRET`, `PSEUDONYM_SECRET`, `POSTGRES_PASSWORD` and `ENCRYPTION_KEY`. `.env` is
listed in `.gitignore`; never commit it.

Open <http://localhost:3000>. The demo accounts are `admin`, `operator`, `analyst` and
`viewer`, with the password from `DEMO_PASSWORD` (default `nirnay-demo`). The demo network
is seeded automatically (`SEED_DEMO=true`), so no configuration is needed. All demo cameras
and their data are labelled **DEMO / synthetic**.

## 2. Docker Compose

`docker-compose.yml` defines five services:

| service | image | role |
|---|---|---|
| `postgres` | `postgis/postgis:16-3.4` | Main database. The schema is created by Alembic at API start-up. |
| `redis` | `redis:7-alpine` | Event bus: the durable `ingest` stream, `ui` pub/sub and `control` pub/sub. `noeviction` stops events being dropped silently. |
| `backend` | `docker/backend.Dockerfile` | FastAPI API (`WORKER_MODE=external`: serves the API and the WebSocket, runs no cameras). |
| `worker` | same image | `python -m app.workers.main` (role `all`): camera stream workers, the ingestion service and the scheduler. |
| `frontend` | `docker/frontend.Dockerfile` | Built React app served by nginx on port 3000. Proxies `/api`, `/ws`, `/health`, `/ready` and `/docs` to the backend. |

* The backend image downloads the models at build time and verifies their pinned SHA-256
  checksums. For offline builds use `--build-arg DOWNLOAD_MODELS=0` and place the models
  in `./models` yourself (see `scripts/download_models.py`). Missing detector and Re-ID
  models fall back to the documented alternatives. OCR has no fallback: without the OCR
  model, plates are not read.
* The API binds to `127.0.0.1:8000` on the host. Only the frontend (port 3000) is meant to
  be exposed.
* The containers run as an unprivileged user (`uid 10001`). Evidence and spool data are
  kept in the `nirnay-data` volume, and the database in `pgdata`.

## 3. Development mode (no Docker)

`./run.sh dev`:

1. Creates `.venv` and installs `backend/requirements-dev.txt`.
2. Downloads and verifies the models.
3. Installs the frontend dependencies.
4. Starts `uvicorn app.main:app` on 127.0.0.1:8000.
5. Starts the Vite dev server on port 3000.

With the defaults this uses:

* SQLite at `data/nirnay.db` (WAL mode). The schema is created directly from the models.
* The in-process event bus.
* `WORKER_MODE=embedded`: camera workers, ingestion and the scheduler run inside the API
  process.

Manual equivalent:

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python scripts/download_models.py
cd backend && ../.venv/bin/uvicorn app.main:app --port 8000   # terminal 1
cd frontend && npm install && npm run dev                      # terminal 2
```

## 4. Configuration

Every variable is documented in `.env.example`. The important ones:

| variable | default | notes |
|---|---|---|
| `APP_ENV` | `development` | `production` enforces the checks in §6. |
| `DATABASE_URL` | SQLite in `data/` | `postgresql+psycopg://user:pass@host:5432/nirnay` for production. |
| `REDIS_URL` | empty (in-process bus) | Required for `WORKER_MODE=external`. |
| `WORKER_MODE` | `embedded` | `embedded`, `external` or `api-only`. |
| `WORKER_ID`, `WORKER_SHARD` | `worker-1`, `0/1` | See §5. |
| `JWT_SECRET`, `ENCRYPTION_KEY`, `PSEUDONYM_SECRET` | – | Required in production. |
| `PRIVACY_MODE`, `EVIDENCE_ENCRYPTION` | `true`, `false` | Pseudonymised plates for analytics roles; Fernet-encrypted evidence at rest. |
| `DEMO_USERS`, `SEED_DEMO`, `DEMO_SOURCE` | `true`, `true`, `synthetic` | Turn the demo users and data off in production. |
| `INFERENCE_DEVICE`, `INFERENCE_THREADS` | `auto`, `0` | ONNX Runtime provider and thread count. |
| `DEFAULT_PROCESSING_FPS`, `DEFAULT_CONFIDENCE_THRESHOLD`, `OCR_CONFIDENCE_THRESHOLD` | 5, 0.35, 0.55 | Defaults for new cameras; each camera can override them. |
| `CAMERA_TIMEOUT`, `CAMERA_MAX_BACKOFF` | 8 s, 60 s | Offline detection and the reconnect backoff cap. |
| `RETENTION_*_DAYS` | 30 / 14 / 7 | Initial retention for observations, evidence and health. Adjustable at runtime in Settings. |

Model paths and parameters are set in `configs/models.yaml` (see `models/README.md` for
licences). Runtime tuning is done in **Settings** in the UI: identity weights, OCR
thresholds, congestion, alert rules, retention and privacy. Those changes are validated
and audited, and need no restart.

### Recorded demo mode

`DEMO_SOURCE=recorded` replays pre-rendered MP4 files through the ordinary **file** source
instead of rendering frames live. This saves CPU on small machines and exercises the
file-ingest path. Generate the files once:

```bash
cd backend && ../.venv/bin/python scripts/generate_demo_videos.py
```

## 5. Scaling out

* **Split roles.** `python -m app.workers.main --role streams` runs camera workers only.
  `--role ingest` runs the ingestion service and the scheduler. `--role all` runs both.
* **Shard cameras.** Run N stream workers with `WORKER_SHARD=0/N` … `N-1/N` and distinct
  `WORKER_ID`s. Each worker takes a stable, disjoint subset of the cameras (a hash of the
  camera id), and a camera added through the API is picked up by exactly one worker.
* **One scheduler.** Run exactly one `ingest` (or `all`) worker. The scheduler runs the
  periodic alert rules and retention, so two of them would duplicate that work. Ingestion
  reads the ingest stream through a Redis consumer group and acknowledges each event only
  after it is committed. Events left pending by a crashed worker are reclaimed after 60 s.
* **API.** API replicas with `WORKER_MODE=external` are stateless apart from their
  WebSocket connections. Control commands (restart, fault injection) reach every worker
  over Redis pub/sub.
* **Sizing.** On a 2-CPU / 4 GB machine, six demo cameras ran at 3–5 processed fps each
  with everything in one process. Plan roughly one CPU core per 2–3 cameras at 5 fps with
  the default nano models, and more for dense scenes. The Cameras page shows the input and
  processing fps, queue depth and dropped frames of every camera, so you can see when a
  worker is saturated.

## 6. Production checklist

* `APP_ENV=production`. The API refuses to start if `JWT_SECRET` is weak or missing, if
  `ENCRYPTION_KEY` is unset, or if `DEMO_USERS=true`.
* `SEED_DEMO=false` and `DEMO_USERS=false`. Create the first administrator with
  `ADMIN_USERNAME` and `ADMIN_PASSWORD`, then remove them from the environment.
* PostgreSQL and Redis, with `WORKER_MODE=external` and a separate worker service.
* TLS termination in front of the frontend. Keep the API port private.
* `CORS_ORIGINS` set to the real frontend origin.
* `EVIDENCE_ENCRYPTION=true` where the law requires encryption of stored images.
* Back up `ENCRYPTION_KEY` securely. Without it the stored camera credentials and
  encrypted evidence cannot be recovered.
* Review Settings → retention and Settings → privacy against the applicable policy.
* Put cameras on an isolated network, and give NIRNAY read-only camera accounts.
* Scrape `/ready` (it returns 503 when a dependency fails) and collect the structured JSON
  logs (`LOG_JSON=true`). API log lines carry the request id, and pipeline log lines carry
  the camera id.

## 7. Operations

* **Health.** `/health` is liveness and `/ready` is readiness. The **System Health** page
  shows workers, the bus, the database, models, storage, dead letters and system events.
* **Dead letters.** Events that still fail ingestion after retries are stored with their
  error. On the System page you can replay one (in-process, or requeued to the ingest
  stream) or discard it.
* **Retention.** Runs hourly and on demand (System → *Run retention now*). It deletes
  observations, evidence files and health samples older than the configured periods.
* **Backups.** Back up the PostgreSQL database and the data volume (evidence) together,
  since evidence manifests are hash-linked to database rows.
