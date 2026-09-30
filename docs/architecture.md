# NIRNAY architecture

NIRNAY turns many independent CCTV/ANPR cameras into one city-wide view of vehicle
movement and traffic state. This document describes the moving parts and how data flows
between them. Everything described here exists in the code; limitations are listed at the end.

## Process layout

```
             ┌────────────────────────── browser (React console) ──────────────────────────┐
             │  REST (JSON, bearer JWT)        WebSocket /ws/events        MJPEG / JPEG      │
             └──────────────┬──────────────────────────┬───────────────────────┬────────────┘
                            │                          │                       │
                 ┌──────────▼──────────────────────────▼───────────────────────▼─────────┐
                 │                         API process (FastAPI)                          │
                 │ auth · RBAC · rate limit · audit · REST routes · WS fan-out · media   │
                 └──────────┬──────────────────────────┬─────────────────────────────────┘
                            │ SQL                      │ event bus (Redis or in-memory)
                 ┌──────────▼─────────┐     ┌──────────▼──────────────────────────────────┐
                 │ PostgreSQL / SQLite│◄────┤ worker process (or embedded in the API)      │
                 │ (Alembic schema)   │     │  StreamManager → one StreamWorker per camera │
                 └────────────────────┘     │  IngestionService (single DB writer)         │
                                            │  Scheduler (periodic rules, analytics, retention)
                                            └──────────────────────────────────────────────┘
```

`WORKER_MODE` selects the topology:

| mode | where workers run | bus | use |
|---|---|---|---|
| `embedded` (default) | inside the API process | in-memory (or Redis if `REDIS_URL` set) | single machine, development, demo |
| `external` | `python -m app.workers.main` (the `worker` service in Compose) | Redis (required) | production |
| `api-only` | nowhere (API serves stored data only) | any | read-only replicas, tests |

The worker entry point accepts `--role all|streams|ingest`, so camera streaming and
ingestion can be scaled or placed separately.

## Backend modules (`backend/app`)

| package | responsibility |
|---|---|
| `camera/sources` | `CameraSource` interface and the RTSP, HTTP (MJPEG / snapshot), webcam, file and synthetic-demo implementations. A grabber thread per source fills a bounded queue (`max_queue_size`); old frames are dropped, never queued up as latency. |
| `camera/health` | per-camera state machine CONNECTING → ONLINE ⇄ DEGRADED, → OFFLINE / ERROR, reconnect with exponential backoff + jitter. |
| `camera/onvif` | WS-Discovery and the ONVIF Profile S calls needed to add a camera (device info, profiles, stream URI) with WS-Security digest auth. |
| `ml` | model registry (ONNX Runtime), vehicle detector, ByteTrack, plate detector, plate rectification/enhancement, OCR, Indian plate normalisation, temporal OCR fusion, Re-ID embedder, monocular speed/direction/lane geometry, the per-camera `anpr_pipeline`. |
| `workers` | `StreamManager` (reconciles one `StreamWorker` thread per enabled camera with the DB, executes control commands), `StreamWorker` (source → pipeline → bus), the local dead-letter spool, the external worker entry point. |
| `core` | settings (`pydantic-settings`), event bus (`RedisBus` / `InMemoryBus`), structured JSON logging, JWT/bcrypt/Fernet security, rate limiter, runtime registry. |
| `services` | ingestion (single writer), identity (global vehicle matching), topology graph, trajectory + next-camera prediction, travel-time baselines, analytics + congestion, alert engine + rules, watchlist, health intelligence, corridor planner (simulation), privacy masking, retention, scheduler, audit, settings. |
| `evidence` | evidence packages (crops + before/detection/after frames), SHA-256 manifest, optional encryption at rest. |
| `api/routes` | REST + WebSocket endpoints (see [api.md](api.md)). |
| `db` | SQLAlchemy 2 models, session management, seed data (demo network, demo accounts). Schema changes go through Alembic (`backend/alembic`). |
| `demo` | synthetic city network, scripted scenario and frame renderer used by the demo camera source. |

## Data flow of one vehicle

1. **Source** – a frame is grabbed from the camera (or rendered by the demo source; the rest of the chain is identical).
2. **StreamWorker** – throttles to `processing_fps` / `frame_skip`, runs the pipeline, updates health and the live preview frame.
3. **Pipeline** – detection → ByteTrack → plate detection → rectify/enhance → OCR → normalise (raw text kept) → temporal voting across the track → Re-ID embedding → speed, direction, lane. When the track leaves the scene one `observation` event is emitted, with evidence crops/frames attached. Per-minute `traffic_sample` and periodic `health_sample` events are emitted too.
4. **Bus** – events go to the durable `ingest` channel (Redis Stream with a consumer group, or a bounded in-memory queue). If publishing fails, the worker spools the event to disk and replays it later.
5. **IngestionService** – one transaction per event: track, observation, plate reads, embedding, evidence manifest → **identity** match (global vehicle) → **trajectory** point + prediction bookkeeping → per-observation **alert rules** → commit → acknowledge. Failing events are retried and then moved to `dead_letters` (visible and replayable on the System page).
6. **UI events** – after commit, `vehicle_detected`, `plate_read`, `vehicle_matched`, `trajectory_updated` and any `alert_created` are published on the `ui` channel; the API fans them out to WebSocket clients filtered by permission and privacy mode.
7. **Scheduler** – every few seconds evaluates periodic rules (congestion, degraded cameras, OCR degradation, surge, travel-time anomaly), broadcasts `analytics_updated`, and runs retention (hourly, first pass a minute after start-up).

## Storage

Main tables: `cameras`, `camera_edges`, `roads`, `vehicle_tracks`, `vehicle_observations`,
`plate_reads`, `vehicle_embeddings`, `global_vehicles`, `trajectory_points`, `traffic_metrics`,
`camera_health`, `alerts`, `watchlist`, `evidence`, `audit_logs`, `system_events`,
`dead_letters`, `worker_heartbeats`, `system_settings`, `users`, `roles`.
Indexes cover the hot queries (plate lookups, per-camera time ranges, trajectory order,
alert status/time, audit time/user). On PostgreSQL the schema is created and upgraded by
Alembic (`alembic upgrade head` runs automatically at API start-up); SQLite databases are
created directly from the same SQLAlchemy models.

PostgreSQL is the production database. The Compose file uses the `postgis/postgis` image so
the spatial extension is available, but the current schema stores coordinates as
latitude/longitude columns and road geometry as GeoJSON; distance and path computations
are done by the in-process topology graph. SQLite (WAL mode) is supported for the
zero-configuration demo and for tests.

## Real-time channel

`/ws/events?token=<JWT>` streams `{type, ts, data}` envelopes. Clients may restrict types
with `?types=` or a `{"type":"subscribe","types":[…]}` message. Each event type requires a
permission (e.g. alert events need `alerts:read`); plates are pseudonymised on the way out for
roles in privacy mode. The server closes with code 4401 on an invalid or expired token.

## Security and privacy

* **Authentication** – JWT bearer tokens (HS256, `JWT_SECRET`), bcrypt password hashes, login rate limit.
* **RBAC** – four roles: Admin, Traffic Operator, Analyst, Read-only (`viewer`). Permissions are fine-grained (`cameras:control`, `plates:view_raw`, `analytics:od_individual`, …) and enforced on every route and every WebSocket event.
* **Audit** – logins, searches (with their filters), evidence access, alert acknowledgements/resolutions, camera/topology/settings/user changes are written to `audit_logs`, visible on the Audit page.
* **Privacy mode** – roles listed in the `privacy.pseudonymise_for_roles` setting (analyst, viewer by default) only ever receive `PSN-…` keyed pseudonyms (HMAC with `PSEUDONYM_SECRET`) instead of plate text, in REST and WebSocket payloads; they cannot open evidence. Aggregates (OD matrix) do not expose individual journeys without `analytics:od_individual`.
* **Secrets** – camera credentials are split out of the URI, stored Fernet-encrypted, and never returned (`has_credentials` / `username_hint` only); URIs in logs and errors are masked. Passwords never appear in logs.
* **Encryption at rest** – evidence images optionally Fernet-encrypted (`EVIDENCE_ENCRYPTION=true`); camera credentials always.
* **Retention** – configurable observation, evidence and health retention (Settings → retention), executed hourly by the scheduler and on demand from the System page.
* **Scope** – vehicles and traffic only: no face or person detection model is included or supported. No traffic-signal control: the emergency corridor is advisory simulation output.
* **Input validation** – strict Pydantic request models (unknown fields rejected), bounded query parameters.

## Failure handling

* A camera failing to connect, decode, or run a model never affects other cameras: each has its own thread, every stage is guarded, failures become health transitions.
* Bus unavailable → events spooled to disk, replayed on recovery.
* Poison events → retried, then dead-lettered with the error; replay/discard from the UI.
* Scheduler jobs are isolated from each other; failures become `system_events`.
* `/health` is liveness; `/ready` checks database, bus, cameras and worker heartbeats.

## Known limitations

* Speeds are monocular estimates; accuracy depends on per-camera calibration (`focal_px` or a ground-plane homography).
* The default detector is COCO-trained (no auto-rickshaw class) and the OCR model is a global plate model; see [models/README.md](../models/README.md).
* The synthetic demo renderer draws sedans, hatchbacks, SUVs, buses and trucks (no two-wheelers or auto-rickshaws) as simple shaded shapes with legible plates; it exercises the full pipeline but is not photo-realistic.
* Tested here on a 2-CPU / 4 GB machine with six demo cameras at 3–5 processed fps each. Under heavy synthetic load a camera may report DEGRADED (processing below target), which is the health monitor working as intended.
