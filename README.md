# NIRNAY

**One vehicle. Many cameras. One city-wide view.**

NIRNAY is a multi-camera ANPR, vehicle-trajectory and traffic-intelligence platform for
city traffic management. It was built for Smart India Hackathon 2026, problem statement
**SIH26127** (Bharat Electronics Limited).

It ingests ordinary CCTV and ANPR streams: RTSP, HTTP/MJPEG, webcams and video files. It
detects, tracks and reads the plate of every vehicle, and links each sighting into one
global vehicle identity with an explicit confidence and explanation. On top of that it
builds trajectories, explainable congestion scores, origin–destination and travel-time
analytics, and rule-based alerts, all shown live in an operations console.

> This system is intended for authorised traffic-management environments. It processes
> **vehicles and traffic only**. There is no facial recognition and no person
> identification, and it never controls traffic signals.

## Quick start

```bash
./run.sh            # Docker Compose if available, otherwise local dev mode (no Docker)
# Windows: run.bat
```

Open <http://localhost:3000> and log in as `admin`, `operator`, `analyst` or `viewer`. The
password is `DEMO_PASSWORD` from `.env` (default `nirnay-demo`).

No configuration is needed. On first start NIRNAY seeds a **synthetic demo network**: six
cameras on a Lucknow corridor, rendered by a scripted traffic scenario. The frames go
through exactly the same pipeline as real cameras. Demo cameras and every record derived
from them are labelled **DEMO / synthetic** and are never mixed with live data. See
[docs/demo-script.md](docs/demo-script.md) for a five-minute walkthrough.

To connect real cameras, see [docs/cctv-integration.md](docs/cctv-integration.md).

## What it does

| area | capabilities |
|---|---|
| **Ingestion** | RTSP, HTTP (MJPEG or snapshot), webcam, browser Local Camera (the laptop webcam streamed from the console, labelled LOCAL CAMERA DEMO), file and demo sources behind one `CameraSource` interface. Reconnects with backoff and jitter, health states ONLINE/DEGRADED/OFFLINE/CONNECTING/ERROR, ONVIF discovery and probing. A failing camera never affects the others. |
| **ANPR pipeline** | YOLOX vehicle detection → ByteTrack → YOLOv9 plate detection → rectification and enhancement → CCT OCR → Indian plate normalisation (raw text kept, corrections listed) → temporal voting → MobileNetV2 Re-ID → speed, direction and lane estimates. Each camera has configurable fps, resolution, thresholds, frame skip and queue size. |
| **Identity and trajectory** | Weighted fusion of plate, appearance, time, route and attributes. Every link carries `match_score`, `match_reasons` and a confidence level. The road topology rejects physically impossible links. Journeys are replayed on an offline road map, with a predicted next camera whose outcome is tracked. |
| **Analytics** | Counts, speeds, occupancy, queue and density. An explainable congestion score (FREE/MODERATE/HEAVY/SEVERE), hotspots, the OD matrix, travel-time anomalies, incident impact and an emergency-corridor planner (simulation only). |
| **Alerts** | Explicit, configurable rules: watchlist, repeated sighting, impossible travel (cloned plates), wrong way, severe congestion, camera offline or degraded, OCR degradation, traffic surge and travel-time anomaly. Statuses NEW → ACKNOWLEDGED → RESOLVED, each with an explanation and an audit trail. |
| **Evidence** | Vehicle and plate crops plus before, detection and after frames, stored under a SHA-256 manifest that can be verified. Optional encryption at rest. |
| **Camera health intelligence** | A 0–100 health score per camera, with the factors behind it and concrete recommendations. Fault injection is available for resilience testing. |
| **Security and privacy** | JWT authentication and four RBAC roles (Admin, Traffic Operator, Analyst, Read-only). Audit logs for searches, evidence access, alert actions and admin changes. Plates are pseudonymised (`PSN-…`) for analytics roles. Camera credentials are encrypted and never returned. Configurable data and evidence retention, rate limiting and strict input validation. |

## Stack

* **Backend.** Python 3.11+, FastAPI, SQLAlchemy 2 and Alembic, PostgreSQL/PostGIS (SQLite
  for the zero-config demo), Redis Streams (an in-process bus for single-machine use),
  ONNX Runtime and OpenCV.
* **Frontend.** React, TypeScript, Vite, TanStack Query, Tailwind, Leaflet (offline) and
  Recharts.
* **Deployment.** Docker Compose (frontend, backend, worker, postgres, redis), `run.sh` /
  `run.bat`, and a non-Docker dev mode.

The default models are all permissively licensed (Apache-2.0 or MIT) and run locally. See
[models/README.md](models/README.md) and `configs/models.yaml`.

## Repository layout

```
backend/     FastAPI app (app/), Alembic migrations, tests, scripts (smoke test, demo video renderer)
frontend/    React operations console (src/pages, src/components, src/test)
configs/     models.yaml, demo_network.json
docker/      Dockerfiles and nginx config
models/      model files (downloaded) and their licences
scripts/     download_models.py
docs/        documentation
```

## Documentation

* [Architecture](docs/architecture.md): processes, data flow, storage, security and known limitations.
* [CCTV integration](docs/cctv-integration.md): sources, credentials, ONVIF, calibration and health.
* [ANPR pipeline](docs/anpr-pipeline.md): every stage, evidence and tuning.
* [Trajectory engine](docs/trajectory-engine.md): identity fusion, topology, baselines and prediction.
* [Deployment](docs/deployment.md): Compose, dev mode, configuration, scaling and the production checklist.
* [API](docs/api.md): conventions, endpoints, WebSocket events and roles. OpenAPI is served at `/docs`.
* [Demo script](docs/demo-script.md): the five-minute jury walkthrough.

## Tests

```bash
./run.sh test
# or
cd backend && ../.venv/bin/python -m pytest -q
cd frontend && npx tsc --noEmit -p . && npx vitest run
```

The backend tests include an end-to-end A → B → C journey. Live cameras are registered
through the API, and events are fed through the real ingestion, identity, trajectory and
alert services. The test then checks the results through the REST API, covering:
* impossible travel and wrong-way alerts;
* dead-letter handling and replay;
* the privacy scope and settings validation;
* Alembic migrations;
* worker sharding.

`backend/scripts/smoke_demo.py` runs the full live pipeline on the demo cameras for a set
time and checks that the scripted events are detected.

## Status and limitations

What has and has not been verified is stated honestly in
[docs/deployment.md](docs/deployment.md) and in the limitations section of
[docs/architecture.md](docs/architecture.md). In particular:
* the Compose, PostgreSQL and Redis deployment has not been run in the development
  environment;
* speeds are monocular estimates;
* the default detector and OCR are general-purpose models, not models trained on Indian
  traffic.
