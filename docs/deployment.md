# Deployment

NIRNAY runs in three ways: a full Docker Compose stack, a local development mode without
Docker, and a split production layout. They all use the same code. Only configuration
differs.

> **What was verified here.** The development mode (SQLite, in-process bus, embedded
> workers, six demo cameras) was run and tested on a 2-CPU / 4 GB Linux machine. The
> production topology was then run natively (without containers) against PostgreSQL 17 +
> PostGIS 3.5 and Redis 8: Alembic upgrade on PostGIS, `WORKER_MODE=external` API plus a
> separate `python -m app.workers.main` worker, vehicles tracked across five cameras over the
> Redis bus. The single-container mode of §8 was run the same way, with SQLite and with a
> hosted-style `postgresql://` URL. **Docker itself could not run in that environment**, so
> the images (`docker/*.Dockerfile`) and the Compose stack have not been built or started.
> Build and run them once in your own environment before a production rollout.

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
| `DATABASE_URL` | SQLite at `$DATA_DIR/nirnay.db` | `postgresql+psycopg://user:pass@host:5432/nirnay` for production. `postgres://` / `postgresql://` URLs from hosting providers are accepted. |
| `DATA_DIR` | `<repo>/data` (`/data` in the images) | Evidence, spool, the default SQLite database and (single container) generated secrets. Put it on a volume. |
| `FRONTEND_DIR` | unset (`/app/frontend/dist` in `app.Dockerfile`) | When set, the API also serves the built console with an SPA fallback (§8). |
| `REDIS_URL` | empty (in-process bus) | Required for `WORKER_MODE=external`. |
| `WORKER_MODE` | `embedded` | `embedded`, `external` or `api-only`. |
| `WORKER_ID`, `WORKER_SHARD` | `worker-1`, `0/1` | See §5. |
| `JWT_SECRET`, `ENCRYPTION_KEY`, `PSEUDONYM_SECRET` | – | Required in production. |
| `PRIVACY_MODE`, `EVIDENCE_ENCRYPTION` | `true`, `false` | Pseudonymised plates for analytics roles; Fernet-encrypted evidence at rest. |
| `DEMO_USERS`, `SEED_DEMO`, `DEMO_SOURCE` | `true`, `true`, `synthetic` | Turn the demo users and data off in production. |
| `DEMO_CAMERAS` | empty (all six) | Comma list of demo cameras started automatically, e.g. `CAM-01,CAM-02,CAM-03`. The others are seeded but stopped. |
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

## 8. Single-container cloud deployment (Railway, Render, a VM)

`docker/app.Dockerfile` builds one image with everything: the console is built with Node and
served by the API itself (`FRONTEND_DIR`), camera workers and ingestion run in the API process
(`WORKER_MODE=embedded`, no Redis), and the database is SQLite on the data volume unless
`DATABASE_URL` is set. `docker/start.sh` is the entry point:

* listens on `$PORT` (set by the platform; default 8000) with proxy headers trusted, so
  the platform's HTTPS and WebSocket (`wss://…/ws/…`) termination work unchanged;
* generates `JWT_SECRET`, `PSEUDONYM_SECRET` and `ENCRYPTION_KEY` once into
  `$DATA_DIR/secrets.env` (mode 600) unless they are set in the environment, which
  always wins;
* **refuses to start** if `DEMO_PASSWORD` is empty or the published default while demo
  users are on (a hosted demo is on the public internet), and if `DATABASE_URL` points to an
  external database while `ENCRYPTION_KEY` is not set (the stored camera credentials would
  become unreadable after the next redeploy);
* when started as root, gives `$DATA_DIR` to the unprivileged `nirnay` user and drops to it
  (for platforms that mount volumes owned by root). *This path has not been exercised.*

### Resource needs (measured)

About **400 MB RAM plus ~130 MB per running camera** at the default demo rate, and roughly
0.2 CPU per camera: 4 cameras ≈ 1.15–1.2 GB and 0.75 CPU, 6 cameras ≈ 1.5–1.6 GB. Choose
`DEMO_CAMERAS` to fit the instance; stopped cameras can be started from the console. The
image is about 1.5 GB (ONNX Runtime, OpenCV, models). Free tiers with 512 MB RAM cannot run
the pipeline, and hosts without a persistent disk lose evidence and the SQLite database on
every redeploy.

### Railway

`railway.json` selects the Dockerfile and the `/health` check.

1. Push the repository to GitHub, then in Railway: *New Project → Deploy from GitHub repo*.
2. Service → *Settings → Volumes*: add a volume mounted at **`/data`**. Railway mounts
   volumes owned by root, so also set `RAILWAY_RUN_UID=0` (start.sh then chowns the volume
   and drops privileges).
3. *Variables*: `DEMO_PASSWORD` (a strong value) and `DEMO_CAMERAS` (for example
   `CAM-01,CAM-02,CAM-03` on 1 GB). Optional: `LOG_JSON=true`.
4. Optional PostgreSQL: add Railway's PostgreSQL service and set
   `DATABASE_URL=${{Postgres.DATABASE_URL}}` **and** `ENCRYPTION_KEY` (see
   `.env.example`). If the database has no PostGIS extension the migration skips the
   PostGIS parts (geometry columns and spatial indexes); everything else works.
5. *Settings → Networking → Generate Domain*, then open it and log in as `admin` with
   `DEMO_PASSWORD`.

Railway's trial and free plans cap a service at 1 GB and 0.5 GB of RAM respectively; the
Hobby plan is billed by usage (at the time of writing about $10 per GB-month and $20 per
vCPU-month, with $5 included), so a 3–4 camera demo running all month costs roughly $20–30.
Check current prices before relying on this.

### Render

`render.yaml` is a blueprint for the same image with a 5 GB disk at `/data`. Render's free
instance (512 MB, no disk) is too small, so the blueprint uses the paid `standard` plan
(2 GB). *New → Blueprint*, choose the repository, and enter `DEMO_PASSWORD` when asked.

### A free VM (full Compose stack)

A VM with 2+ vCPU and 4+ GB (for example an Oracle Cloud *Always Free* Ampere instance)
runs the complete Compose stack of §2, with PostgreSQL/PostGIS and Redis:

```bash
git clone <your repository> nirnay && cd nirnay
./run.sh docker            # creates .env with random secrets, builds and starts the stack
```

Set `DEMO_PASSWORD` in `.env` before exposing it, and put TLS in front of port 3000 (for
example Caddy: `caddy reverse-proxy --from your.domain --to localhost:3000`). On an ARM
(Ampere) VM, note that the official `postgis/postgis` image is published for amd64 only:
change the `postgres` image in `docker-compose.yml` to an arm64 PostGIS build, or to plain
`postgres:16` (the migration then skips the PostGIS parts). The other images are multi-arch.

### Single container locally

```bash
docker build -f docker/app.Dockerfile -t nirnay .
docker run -p 8000:8000 -v nirnay-data:/data -e DEMO_PASSWORD='choose-one' -e DEMO_CAMERAS=CAM-01,CAM-02,CAM-03 nirnay
```
