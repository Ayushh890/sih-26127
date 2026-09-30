# API reference

The backend is a FastAPI application. The interactive OpenAPI documentation is served by
the backend itself:

* `/docs` – Swagger UI;
* `/redoc` – ReDoc;
* `/openapi.json` – the machine-readable schema.

The frontend container proxies all three. This page covers the conventions and lists every
endpoint. For request and response fields, use the OpenAPI schema; it is generated from
the same code.

## Conventions

* **Authentication.** `POST /api/auth/login` with `{"username", "password"}` returns
  `{"access_token", "token_type": "bearer", …}`. Send `Authorization: Bearer <token>` on
  every other call. An invalid or expired token returns `401`, and a missing permission
  returns `403` with `{"detail": "missing permission <perm>"}`.
* **Permissions.** Every route requires one or more permissions (see
  [Roles](#roles-and-permissions)). The frontend hides actions that the user's role cannot
  perform, but the server always enforces them.
* **Validation.** Request bodies are strict Pydantic models, so unknown fields are
  rejected. Query parameters are bounded (length, range, pattern). Validation errors return
  `422` in FastAPI's standard `{"detail": [{"loc", "msg", …}]}` shape.
* **Rate limiting.** Requests are limited per client (`RATE_LIMIT_PER_MINUTE`), and login
  has a stricter limit (`LOGIN_RATE_LIMIT_PER_MINUTE`). When the limit is exceeded the API
  returns `429` with `Retry-After`. Every response carries `X-Request-ID`, which is also
  written to the structured logs.
* **Data scope.** Search and analytics endpoints take `scope=live|demo|all`. When no scope
  is given, `live` is used if any non-demo camera exists, otherwise `demo`. Responses echo
  `scope`, `synthetic` and a `notice`, so synthetic data is always labelled and never
  silently mixed with live data.
* **Empty periods.** Analytics responses carry `has_data`. When there is no data they
  carry `message: "No data available for this period."` instead of zeros.
* **Periods.** Period-based analytics take `period=15m|1h|6h|24h|7d|30d`, or explicit
  `since`/`until` (ISO-8601). `congestion` and `travel-times` take an optional `until` to
  evaluate the network as of a past time; a future value is clamped to now.
* **Privacy.** For roles in privacy mode (`privacy.pseudonymise_for_roles`, analyst and
  viewer by default), every plate field and plate-like substring in a response is replaced
  by a stable `PSN-…` pseudonym. Vehicle search accepts a pseudonym as the query, and those
  roles cannot open evidence.
* **Lists.** Lists are returned under a named key (`results`, `cameras`, `alerts`, …) with
  `limit`/`offset` where paging applies. Vehicle and alert references accept either the
  numeric id or the code (`VEH-000123`, `ALR-…`).
* **Settings.** `PATCH /api/settings/{section}` takes `{"value": {...}}`. The value is
  deep-merged into the current section, then validated for types, ranges, enums and
  cross-field consistency (e.g. `moderate < heavy < severe`). The change is audited.
* **Audit.** Logins, searches, evidence views and downloads, alert actions, OD drill-downs,
  and every camera, topology, settings, watchlist and user change are recorded. So are
  automatic alert transitions, with the user `system`.

## Endpoints

### Health

| method | path | purpose |
|---|---|---|
| GET | `/health` | Liveness (no authentication). |
| GET | `/ready` | Readiness: database, event bus, processing services and worker heartbeats. Returns `503` until ready. |

### Auth and users

| method | path | permission | purpose |
|---|---|---|---|
| POST | `/api/auth/login` | – | Obtain a bearer token (rate-limited, audited). |
| GET | `/api/auth/me` | any | Current user, role and permissions. |
| POST | `/api/auth/password` | any | Change own password. |
| GET | `/api/users/roles` | `users:manage` | Roles and their permissions. |
| GET, POST | `/api/users` | `users:manage` | List and create users. |
| PATCH | `/api/users/{id}` | `users:manage` | Change role, active flag or password (reset). |

### Cameras

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/cameras` | `cameras:read` | Cameras with live runtime state (credentials never included). |
| POST | `/api/cameras` | `cameras:write` | Register a camera. |
| GET | `/api/cameras/runtime` | `cameras:read` | Live processing metrics for all running cameras. |
| POST | `/api/cameras/test-connection` | `cameras:write` | Open a source, grab one frame and report the result. |
| GET, PATCH, DELETE | `/api/cameras/{id}` | `cameras:read` / `cameras:write` | Detail, update (the worker restarts with the new config), delete. |
| POST | `/api/cameras/{id}/start`, `/stop`, `/restart` | `cameras:control` | Worker control. |
| POST | `/api/cameras/{id}/simulate-fault`, `/simulate-disconnect`, `/clear-faults` | `cameras:control` | Fault injection for resilience testing. |
| GET | `/api/cameras/{id}/health` | `cameras:read` | Health intelligence: score, grade, factors and recommendations. |
| GET | `/api/cameras/{id}/health/history`, `/logs` | `cameras:read` | Health samples; status transitions and errors. |
| GET | `/api/cameras/{id}/snapshot.jpg`, `/stream.mjpg` | `stream:view` | Latest frame; MJPEG preview (`overlay`, `fps`). |
| POST | `/api/onvif/discover` | `cameras:write` | WS-Discovery on the local network segment. |
| POST | `/api/onvif/probe` | `cameras:write` | Device info, media profiles and stream URIs. |

Media endpoints also accept `?token=` because `<img>` tags cannot send headers.

### Vehicles and trajectories

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/vehicles/search` | `vehicles:search` | Search sightings by plate (exact, partial, fuzzy with `max_distance`, or pseudonym), camera, time, class, colour, confidence, match level and unreadable plate. Audited. |
| GET | `/api/observations` | `trajectory:read` | Recent sightings. |
| GET | `/api/observations/{id}` | `trajectory:read` | Sighting detail: plate reads with raw text and corrections, match breakdown, track and evidence. |
| GET | `/api/vehicles` | `trajectory:read` | Global vehicle identities, most recently seen first. |
| GET | `/api/vehicles/{ref}` | `trajectory:read` | One identity. |
| GET | `/api/vehicles/{ref}/trajectory` | `trajectory:read` | Confidence-annotated trajectory with road geometry for replay. |
| GET | `/api/vehicles/{ref}/prediction` | `trajectory:read` | Predicted next camera plus the 24 h network accuracy. |

### Topology

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/topology` | `cameras:read` | Edges, roads and GeoJSON. |
| GET | `/api/topology/path` | `cameras:read` | Shortest allowed path, or why none exists. |
| POST | `/api/topology/edges` | `topology:write` | Add a directed edge (optionally in both directions). |
| PATCH, DELETE | `/api/topology/edges/{id}` | `topology:write` | Update travel-time priors, allowed flag, direction or road geometry (`path: [[lat, lon], …]`, `[]` clears it); delete. |
| GET | `/api/topology/cameras/{id}/neighbours` | `cameras:read` | Upstream and downstream cameras. |

### Analytics

All analytics endpoints require `analytics:read` and take `scope`; incident impact uses the
scope of the chosen camera. Only the OD drill-down returns individual journeys.

| method | path | purpose |
|---|---|---|
| GET | `/api/analytics/overview` | Network snapshot; same payload as `analytics_updated`. |
| GET | `/api/analytics/summary` | Counts, speeds, occupancy, queue, density and class mix for a period. |
| GET | `/api/analytics/timeseries` | Bucketed traffic metrics (`bucket_s`, `camera_id`). Alias: `/api/analytics/traffic-volume`. |
| GET | `/api/analytics/congestion` | Explainable congestion score per camera (levels FREE, MODERATE, HEAVY, SEVERE, NO_DATA) with components, weights and thresholds. |
| GET | `/api/analytics/hotspots` | Cameras ranked by period congestion and alert load. |
| GET | `/api/analytics/od-matrix` | Origin–destination journey counts. Small cells are suppressed unless the user has `analytics:od_individual`. Alias: `/api/analytics/od`. |
| GET | `/api/analytics/od-matrix/vehicles` | Journeys behind one OD cell. Needs `analytics:od_individual` too; audited. |
| GET | `/api/analytics/travel-times` | Segment travel times against baseline (travel-time anomaly). Alias: `/api/analytics/travel-time`. |
| POST | `/api/analytics/incident-impact` | Before/during comparison for an incident at a camera and its neighbours in the same data scope. |

### Alerts and watchlist

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/alerts` | `alerts:read` | List alerts (`status=NEW|ACKNOWLEDGED|RESOLVED|OPEN`, `type`, `severity`, `camera_id`, `scope`). |
| GET | `/api/alerts/rules` | `alerts:read` | Configured rules (edit through `/api/settings/alerts`). |
| GET | `/api/alerts/{ref}` | `alerts:read` | Explanation, evidence and acknowledgement history. |
| POST | `/api/alerts/{ref}/acknowledge`, `/resolve`, `/reopen` | `alerts:act` | Status transitions with a note. Audited. |
| GET, POST | `/api/watchlist` | `watchlist:read` / `watchlist:write` | List entries; add a plate (reason required). Audited. |
| PATCH, DELETE | `/api/watchlist/{id}` | `watchlist:write` | Update or remove an entry. Audited. |
| GET | `/api/watchlist/{id}/retrospective` | `watchlist:read` + `vehicles:search` | Search past sightings for an entry. Audited. |

### Evidence

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/evidence/{id}` | `evidence:read` | Metadata and file hashes. |
| GET | `/api/evidence/{id}/{role}.jpg` | `evidence:read` | `vehicle`, `plate`, `before`, `detection` or `after` image. Sent with `X-Evidence-SHA256`; audited. |
| POST | `/api/evidence/{id}/verify` | `evidence:read` | Re-hash the files and manifest against the stored hash. |

### System and settings

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/system/status` | `system:read` | Workers, pipeline, bus, database, models and storage. Alias: `/api/system/metrics`. |
| GET | `/api/system/events` | `system:read` | System event log. |
| GET | `/api/system/dead-letters` | `system:read` | Events that failed ingestion after retries. |
| POST | `/api/system/dead-letters/{id}/replay` | `settings:write` | Retry in-process, or requeue to the ingest stream when ingestion runs in a separate worker (`mode`). |
| POST | `/api/system/dead-letters/{id}/discard` | `settings:write` | Mark as discarded. |
| POST | `/api/system/retention/run` | `settings:write` | Apply data and evidence retention now. |
| GET | `/api/audit` | `audit:read` | Audit log, filterable by user, action, resource and time. |
| GET | `/api/settings`, `/api/settings/{section}` | `system:read` | Sections: `identity`, `ocr`, `congestion`, `alerts`, `retention`, `privacy`. |
| PATCH, DELETE | `/api/settings/{section}` | `settings:write` | Update (validated, audited) or reset to defaults. |

### Demo and corridor

| method | path | permission | purpose |
|---|---|---|---|
| GET | `/api/demo/timeline` | `cameras:read` | Scheduled scripted events of the synthetic scenario (plates pseudonymised for privacy roles). |
| POST | `/api/demo/restart` | `cameras:control` | Purge demo data only and restart the scenario from t = 0. |
| POST | `/api/corridor/plan` | `corridor:use` | Emergency-corridor plan. **Simulation only**; no traffic signal is ever controlled. |

## WebSocket: `/ws/events`

Connect with `?token=<JWT>`, or send `{"token": "..."}` as the first message. Optional
`?types=a,b` restricts the stream, and so does a later
`{"type": "subscribe", "types": [...]}` message. Send `{"type": "ping"}` for a `pong`.

The server first sends a `hello` with the user, role, whether raw plates are visible, and
the event types the user may receive. After that every message is an envelope
`{"type", "ts", "data"}`.

| event | permission | payload |
|---|---|---|
| `vehicle_detected` | `cameras:read` | Live track at a camera (before the track ends). |
| `plate_read` | `trajectory:read` | Individual OCR read with confidence. |
| `vehicle_matched` | `trajectory:read` | Observation linked to a global vehicle: `match_score`, `confidence_level`, `match_reasons`, `new_vehicle`, prediction. |
| `trajectory_updated` | `trajectory:read` | New trajectory point with segment time, speed and explanation. |
| `alert_created`, `alert_updated` | `alerts:read` | Alert (also sent on acknowledge, resolve, reopen and automatic transitions). |
| `camera_status_changed` | `cameras:read` | Health transition with reason. |
| `analytics_updated` | `analytics:read` | Network snapshot every few seconds. |

Events the user may not see are dropped on the server. Plate fields are pseudonymised for
privacy-mode roles. The token is re-checked every 60 s. If it is invalid, expired, or the
user has been deactivated, the connection is closed with code **4401**.

## WebSocket: `/ws/cameras/{camera_id}/ingest`

Upload socket of the Local Camera page. It needs `cameras:control` and a camera whose
`source_type` is `browser`. Authenticate with `?token=<JWT>` or `{"token": "..."}` as the
first message.

* Server → `{"type": "hello", "data": {camera_id, name, enabled, max_in_flight, max_frame_bytes}}`.
* Client → one binary message per frame (a JPEG of at most 2 MB).
* Server → `{"type": "ack", "data": {received, rejected, accepted, reason?, backend?}}` for
  every frame. At most once per second `backend` carries the worker's runtime state
  (status, message, input/processing fps, frames in/processed/dropped, active tracks,
  latency, resolution), or `null` when no worker is processing the camera.
* Client `{"type": "ping"}` → `{"type": "pong", "data": {backend}}`.

Close codes: **4401** unauthenticated, **4403** missing permission, **4404** unknown
camera, **4400** not a browser camera, **4409** another session took over the camera (the
old socket is told on its next frame).

## Roles and permissions

| role | permissions |
|---|---|
| `admin` | all |
| `operator` | cameras read, write and control; stream view; vehicle search; raw plates; trajectories; evidence; analytics including OD drill-down; alerts read and act; watchlist read and write; corridor; system read |
| `analyst` | cameras read; stream view; vehicle search (pseudonymised); trajectories; analytics; alerts read; system read; corridor |
| `viewer` | cameras read; analytics; alerts read; system read |
