# ANPR pipeline

Each camera has one `AnprPipeline` (`backend/app/ml/pipelines/anpr_pipeline.py`) owned by
exactly one `StreamWorker` thread. Models are loaded once by the `ModelRegistry` and
shared. Each stage is guarded separately, so an OCR or Re-ID failure degrades that one
observation and never stops the camera.

```
frame ─► vehicle detection ─► ByteTrack ─► best vehicle view per track
                                   │
                                   ├─► plate detection (vehicle crop) ─► rectify ─► enhance ─► OCR (batched)
                                   │        └─► Indian-format normalisation (raw text kept) ─► temporal voting
                                   ├─► speed / direction / lane (camera geometry)
                                   └─► on track end: colour, Re-ID embedding, evidence ─► observation event
```

## Stages

### 1. Frame intake

The source's grabber thread keeps only the newest frames (`max_queue_size`). The worker
then analyses frames at `processing_fps`, with an optional `frame_skip`. Each frame also
feeds the health monitor with its sharpness (Laplacian variance) and brightness.

### 2. Vehicle detection

YOLOX-nano, trained on COCO, detects the `car`, `motorcycle`, `bus` and `truck` classes
above the camera's `confidence_threshold`. If the model cannot be loaded, a
background-subtraction `motion` detector takes over and reports vehicles without a class.

### 3. Tracking — ByteTrack

ByteTrack uses a two-stage association. Tracks are first matched to high-confidence
detections, and the remaining tracks are then matched to low-confidence ones. This keeps
briefly occluded or blurred vehicles on the same ID instead of creating duplicates. A
constant-velocity Kalman filter predicts track positions between the frames that are
analysed.

Tracks with fewer than `min_track_hits` frames are discarded. Tracks that never produced a
plate must also have enough motion samples, so fragments of a real track are not emitted
as extra vehicles.

### 4. Plate detection

A YOLOv9-t plate detector runs on the expanded vehicle crop. It only runs for vehicles at
least `min_plate_vehicle_width` pixels wide (90 by default), and for at most
`max_plate_attempts` frames per track. The fallback is a contour/aspect-ratio heuristic.

### 5. Rectification and enhancement

* **Rectify** – the dominant bright quadrilateral (the plate background) is found with
  Otsu thresholding and warped to an axis-aligned rectangle when it is skewed by more
  than 2°.
* **Enhance** – crops narrower than 96 px are upsampled with bicubic interpolation.
  Contrast is normalised with CLAHE on the L channel, and an unsharp mask is applied.

### 6. OCR

The CCT model from fast-plate-ocr reads each plate. It returns the character sequence
plus a confidence for each character. Plate crops from all tracks in a frame are sent to
the model as one batch.

### 7. Indian plate normalisation (`app/ml/ocr/normalize.py`)

* Supported formats:
  * standard `SS DD L{0,3} DDDD` (e.g. `UP32AB1234`, `MH12A1234`);
  * Delhi single-digit districts (`DL3CAB1234`);
  * Bharat series `YY BH DDDD L{1,2}` (`22BH1234AA`).
* The raw text is matched to the best format template. Characters in a digit slot that
  read as letters (O→0, I→1, B→8, S→5, …), and the reverse, are corrected in that
  position. State codes are checked against the official list.
* Nothing is discarded. The result keeps `raw_text`, `normalized_text`, `is_valid` and an
  explicit list of every correction (position, from, to, reason). All of these are
  stored with each plate read and shown in the observation detail view.
* `plate_similarity` is a confusion-aware edit distance: swapping 0/O, 1/I, 8/B and
  similar pairs costs 0.35 instead of 1. The identity engine and watchlist matching both
  use it.

### 8. Temporal voting (`app/ml/ocr/fusion.py`)

A track is usually read several times, up to `max_reads_per_track` reads:

1. Reads shorter than 6 characters are fragments of an occluded or badly cropped plate.
   They are kept as evidence but never vote.
2. The modal read length wins, weighted by confidence. Reads that form a valid Indian
   plate get a ×1.5 weight.
3. Within that length group, each character position is decided by a
   confidence-weighted vote.
4. The fused confidence combines per-character agreement with length agreement, so a
   track whose frames disagree shows a visibly lower confidence.

The vehicle's plate is only set when the fused confidence is high enough. Otherwise the
reads remain visible, but the observation is recorded as having no plate.

**Identity-switch guard.** Sometimes a tracker swaps two vehicles. If two or more
confident, valid reads of a clearly different plate arrive on one track (weighted
distance ≥ 3), the track is split, so one plate's evidence never ends up on another
vehicle.

### 9. Appearance embedding (Re-ID)

When a track ends, the best vehicle crop is embedded by MobileNetV2 into an
L2-normalised 1280-dimensional vector (global average pool). The fallback is an HSV
colour histogram. The dominant vehicle colour is estimated from the same crop.
Appearance is treated as evidence, never as proof; see
[trajectory-engine.md](trajectory-engine.md).

### 10. Speed, direction and lane (`app/ml/pipelines/geometry.py`)

* **Speed.** A pinhole model gives each vehicle a depth of `Z = f·W/w`, where `W` is the
  typical width for the vehicle class and `w` is its width in pixels. Speed is the
  least-squares slope of the track's ground-plane positions over time. If the camera has
  a calibrated homography, that is used instead. All speeds are labelled as estimates.
* **Direction.** The compass heading is derived from the camera's `direction`. Motion is
  classified as `with_flow`, `against_flow` or `stationary`.
* **Lane.** The lane is the majority lane over the track, using `lane_boundaries`.

### 11. Observation event

When a track leaves the scene (or reaches `max_track_duration_s`), one `observation`
event is published to the `ingest` channel. It contains:

* camera and track;
* first-seen, last-seen and observed-at times;
* class, colour, bbox and a subsampled image path;
* speed, motion, direction and lane;
* the fused plate with its raw text, confidence, votes and validity;
* every individual read with its corrections;
* the embedding;
* the evidence package reference.

`vehicle_detected` and `plate_read` events are also published live, so the UI updates
before the track ends.

## Evidence

Evidence packages are handled by `app/evidence/store.py`. Each package is stored under
`DATA_DIR/evidence/YYYY/MM/DD/<uuid>/` and contains:

* the `vehicle` and `plate` crops;
* the `before`, `detection` and `after` full frames (downscaled to 640 px wide);
* `manifest.json`, which records the SHA-256 of every file as stored.

The manifest's own SHA-256 is stored in the database, so tampering with either the files
or the manifest can be detected. `POST /api/evidence/{id}/verify` re-hashes everything.

With `EVIDENCE_ENCRYPTION=true`, files are stored Fernet-encrypted (`.jpg.enc`) and
decrypted only when served. Viewing, downloading and verifying evidence are all audited.
Roles in privacy mode cannot open evidence.

## Traffic and health samples

At the end of every `bucket_seconds` bucket (60 s by default), the pipeline emits a
`traffic_sample` for its camera. It contains:

* new tracks and plates read;
* mean and maximum vehicles in frame;
* occupancy (the share of the road area covered by vehicles);
* the mean number of queued vehicles (slower than `queue_speed_kmh`);
* mean speed and the number of speed samples.

Per-class counts come from the observations themselves. Density is derived from the
in-frame counts in analytics.

Health samples (input/processing fps, sharpness, brightness, drops) are emitted
periodically. These samples drive analytics, congestion scoring and camera health
intelligence.

## Tuning

| symptom | knob |
|---|---|
| Vehicles missed | lower `confidence_threshold`; raise `processing_fps` |
| Plates never attempted | lower `min_plate_vehicle_width`; use a higher-resolution stream |
| Too many wrong plates | raise `ocr.confidence_threshold` (Settings → OCR) or the per-camera `ocr_confidence_threshold` |
| Worker overloaded (DEGRADED, processing fps below target) | lower `processing_fps`, raise `frame_skip`, or shard cameras across workers |
| Unrealistic speeds | calibrate `focal_px` or supply a `homography` |

## What is not done

* No face or person detection.
* No make/model classification.
* No night-time IR-specific model.
* The default OCR is a global plate model, compensated by the normalisation and voting
  described above.

See [models/README.md](../models/README.md).
