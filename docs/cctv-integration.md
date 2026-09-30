# Connecting cameras

NIRNAY works with ordinary CCTV and ANPR cameras. Every camera is one row in `cameras`
and one `StreamWorker` thread. Live cameras and synthetic demo cameras go through the
same pipeline; only the `CameraSource` implementation is different.

## Source types

| `source_type` | `source_uri` | Notes |
|---|---|---|
| `rtsp` | `rtsp://host:554/path` or `rtsps://…` | FFmpeg via OpenCV. `processing.source_options.transport` can be `tcp` (default) or `udp`. |
| `http` | `http(s)://host/…` | `source_options.mode`: `mjpeg` (default, a multipart MJPEG stream) or `snapshot` (the JPEG URL is polled at `source_options.snapshot_fps`, default 2). |
| `webcam` | `0`, `1`, … or `/dev/videoN` | A capture device attached to the **server** (the machine running the stream workers). |
| `browser` | `browser://local` | **Local Camera demo.** The webcam of the computer running the console, streamed from the browser. See [Local Camera](#local-camera-demo-input). |
| `file` | a local video path | Played at native speed. `source_options.loop` restarts the file at the end. Use this for recorded footage and the `recorded` demo mode. |
| `demo` | `demo://CAM-01` | Frames rendered by the synthetic scenario. These cameras are flagged `is_demo` and labelled DEMO everywhere. |

URIs are validated when you create a camera: an RTSP camera must use `rtsp://` or
`rtsps://`, a webcam must be a device index, and so on. Unknown request fields are
rejected.

## Local Camera (demo input)

For demonstrations without CCTV, the console's **Local Camera** page (`/local-camera`,
needs `cameras:control`) streams the laptop webcam into the normal pipeline. It is
labelled *LOCAL CAMERA DEMO* and is not presented as CCTV.

1. Register a camera with `source_type: "browser"` and `source_uri: "browser://local"`
   (the page offers a form for users with `cameras:write`; it is created disabled).
2. Press **Start camera**. The browser asks for camera permission, captures frames
   (`getUserMedia`), encodes them as JPEG and sends each one as a binary message over
   `WS /ws/cameras/{id}/ingest?token=…`. Then it enables the camera.
3. The API puts each frame into the camera's input inbox on the event bus (in memory, or
   a Redis list when the workers run in their own container). `BrowserPushSource` reads
   it there, and the camera's `StreamWorker` handles it like any RTSP frame: detection,
   tracking, plate reading, re-identification, health and ingestion.
4. **Stop camera** releases the webcam, closes the socket and disables the camera. This is
   an intentional stop, so no offline alert is raised.

Frames are real pixels, so the camera is not `is_demo`. Its observations count as live
data. Once any non-demo camera exists, the console's *Auto* scope shows live data; pick
*All* in the header to see the demo cameras alongside it.

**Backpressure.** The server acknowledges every frame. The browser keeps at most
`max_in_flight` (2) frames unacknowledged and skips captures while the window is full.
The inbox keeps only the newest 3 frames per camera and drops the oldest, so memory stays
bounded whatever the upload rate. Frames larger than 2 MB, or not JPEG, are rejected and
counted.

**Truthful state.** The page shows frames sent, frames the backend acknowledged, and the
worker's own measured input fps, processing fps, active tracks and status. Nothing is
estimated in the browser. If no frames arrive, the camera is OFFLINE with *waiting for the
browser to send frames*, and it reconnects within 2 s of the browser starting.

**Browser requirements.** Camera access needs a secure page: `https://…` or
`http://localhost`. A LAN IP over plain `http://` is blocked by the browser. The page
explains denied permission, missing cameras, a camera in use by another application and
unsupported browsers.

## Credentials

* If you enter `rtsp://user:pass@host/…`, the `user:pass@` part is removed from the URI
  before it is stored. The password is kept only as a Fernet-encrypted value
  (`ENCRYPTION_KEY`).
* You can also send `username` and `password` as separate fields. The password is
  write-only.
* API responses never contain the password or a credentialed URI. They only show
  `has_credentials` and a masked `username_hint`.
* Logs, errors and camera log entries always use the masked URI.
* To remove stored credentials, send `PATCH /api/cameras/{id}` with
  `{"clear_credentials": true}`.

## Adding a camera

1. **Discover (optional).** Use Cameras → *Add camera* → *Discover cameras on the network*, or call
   `POST /api/onvif/discover`. This sends a WS-Discovery probe on the local network
   segment and lists the devices that answer.
2. **Probe (optional).** `POST /api/onvif/probe` with the device `xaddr` and credentials
   reads the device information, media profiles and each profile's RTSP stream URI. It
   authenticates with a WS-Security password digest. Only the Profile S calls needed to
   add a camera are implemented; NIRNAY never sends PTZ or imaging commands.
3. **Test connection.** `POST /api/cameras/test-connection` opens the source, grabs one
   frame and returns `ok`, the resolution, the native fps, the latency and any error (with
   credentials masked). To test an
   existing camera with its stored password, pass `camera_id`.
4. **Create.** `POST /api/cameras` with the id, name, latitude and longitude, source,
   and optionally direction of travel, lane count, road name, zone, `processing` and
   `calibration`. The stream manager picks the camera up within a few seconds.
5. **Link it into the road graph.** Add directed edges on the Road Topology page (or
   `POST /api/topology/edges`) to its upstream and downstream cameras. Give each edge a
   distance, a minimum and typical travel time, and optionally the road geometry. Without
   edges a camera still counts traffic, but the identity engine cannot link its
   sightings to other cameras.

## Processing settings (`processing`)

| field | range | effect |
|---|---|---|
| `processing_fps` | 0.5–30 | Frames analysed per second. Extra frames are dropped at the source and never queued. |
| `frame_skip` | 0–30 | Additionally skip N frames between analysed frames. |
| `max_queue_size` | 1–64 | Size of the grabber queue. When it is full, the oldest frame is discarded. |
| `confidence_threshold` | 0.05–0.95 | Vehicle detector confidence. |
| `plate_confidence`, `ocr_confidence_threshold` | | Per-camera overrides of the plate-detector and OCR thresholds. |
| `min_plate_vehicle_width` | 40–1000 px | Vehicles narrower than this are tracked, but OCR is not attempted on them. |
| `max_reads_per_track` | 1–50 | Maximum number of OCR reads fused for each track. |
| `queue_speed_kmh` | 1–30 | Vehicles slower than this count as queued. |
| `bucket_seconds` | 15–900 | Period of the traffic samples. |
| `source_options` | object | Source-specific options, as listed above, plus `open_timeout_s`. |

When you change the configuration, the camera's worker restarts with the new settings.
Other cameras are not affected.

## Calibration (`calibration`)

The speed, direction and lane estimates are monocular, so they are only as good as the
calibration:

* `focal_px` together with `reference_width` uses a pinhole model and a class-typical
  vehicle width. Alternatively, a 3×3 image-to-ground `homography` gives better accuracy.
* `camera_height_m` and `vanishing_point` refine the ground-plane estimate.
* `lane_boundaries` are x-positions, as fractions of the frame width, that divide the
  lanes.
* `flow_toward_camera` and `one_way` define the legal direction of travel, which the
  WRONG_WAY rule uses.
* `speed_limit_kmh` sets the camera's posted limit.

## Health and resilience

Each camera has a health state machine:

`CONNECTING → ONLINE ⇄ DEGRADED`, and from any state `→ OFFLINE` (no frames for
`CAMERA_TIMEOUT` seconds, or the connection failed) or `→ ERROR` (a permanent
configuration error, such as an unsupported URI).

* **Reconnects** use exponential backoff with jitter, capped at `CAMERA_MAX_BACKOFF`
  seconds.
* **DEGRADED** means one of the following held for a few seconds (the CAMERA_DEGRADED alert
  additionally requires it to last `min_duration_s`, 30 s by default):
  * the input rate fell below half the source's native rate;
  * the processing rate fell well below the target (the worker is overloaded);
  * sharpness collapsed relative to the camera's own baseline (blur, defocus or rain);
  * the image is very dark.
* **Health intelligence.** `GET /api/cameras/{id}/health` turns the recorded health
  samples into a 0–100 score and a grade. The factors are availability, reconnects,
  processing and input fps, sharpness trend, brightness, plate-read rate, OCR confidence,
  frame drops and current status. Each factor comes with its impact on the score and a
  concrete recommendation.
* **Isolation.** A camera that fails never affects the others. Every status change is
  written to the camera log and `system_events`, and is broadcast as
  `camera_status_changed`. The CAMERA_OFFLINE and CAMERA_DEGRADED alert rules act on
  these status changes.

### Fault injection

Operators with `cameras:control` can test resilience on any camera. The camera page offers
**Inject fault** and **Simulate disconnect**, or you can call
`POST /api/cameras/{id}/simulate-fault` with `{"kind": "offline"|"blur"|"low_fps"|"dark", "seconds": 1–600}`.
The fault is applied inside the source, so the real health monitor and alert rules react
exactly as they would to a genuine failure. **Clear faults** ends it early.

## Live preview

* `GET /api/cameras/{id}/stream.mjpg?overlay=true&fps=6` streams the latest processed
  frame with detection, track and plate overlays drawn on it. Use `overlay=false` for the
  raw frame.
* `snapshot.jpg` returns a single frame.
* The Cameras page's overlay toggle switches between the two modes.
* The preview is served from the worker's latest frame, so viewing it adds no decode
  load.

## Production notes

* Put cameras on an isolated VLAN and give NIRNAY read-only ONVIF/RTSP accounts.
* Prefer an H.264 stream of at least 720p for ANPR at typical junction distances. By
  default OCR is attempted only on vehicles at least 90 px wide in the frame
  (`min_plate_vehicle_width`). Frame the camera so plates are as large as possible.
* Throughput depends heavily on scene density. On the 2-CPU test machine, six demo cameras
  ran at 3–5 processed fps each, and a camera reported DEGRADED under a synthetic jam.
* To scale out, run several `--role streams` workers with distinct `WORKER_SHARD` values
  (`0/2`, `1/2`, …). Each worker runs a disjoint, stable share of the cameras. Run exactly
  one `--role ingest` (or `all`) worker, because it also hosts the scheduler.
