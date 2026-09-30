# Identity and trajectory engine

A camera only sees a vehicle for a few seconds. NIRNAY links those sightings into one
**global vehicle identity** (`global_vehicles`, codes `VEH-000123`) and an ordered,
confidence-annotated **trajectory**.

Every link records a score, a confidence level and the human-readable reasons behind it.
Uncertainty is shown, never hidden.

Code:

* `backend/app/services/identity.py` – matching.
* `topology.py` – the road graph.
* `travel_times.py` – baselines.
* `trajectory.py` – trajectory points and prediction.

## 1. Candidates

When a new observation is ingested, the candidates are:

* vehicles last seen within `max_link_window_s` (default 3600 s), most recent first, up to
  `candidate_limit` of them;
* vehicles with the same plate, even if they are older (the same identity continuing on a
  new journey).

Live and synthetic (demo) data never match each other.

## 2. Scoring one candidate

The new observation is compared with the candidate's **most recent** observation. Each
available signal gives a score between 0 and 1. The fused score is a weighted mean over
**only the signals that are available**, so the weights are renormalised. A missing plate,
for example, neither counts for nor against a match.

| signal | default weight | how it is scored |
|---|---|---|
| plate | 0.35 | Confusion-aware similarity (`0/O`, `1/I`, `8/B` … cost 0.35 instead of 1). The weight is scaled down when either OCR confidence is below `plate_min_confidence` (0.6). |
| appearance | 0.25 | Cosine similarity of the Re-ID embeddings, mapped linearly from `appearance_floor` (0.75 → 0) to `appearance_ceiling` (0.97 → 1). The weight is ×0.6 for the colour-histogram fallback. Embeddings from different models are never compared. |
| time | 0.15 | The elapsed time against the expected travel time for the shortest allowed path. 1.0 if the vehicle is at or faster than the baseline; log-normal decay if it is slower. |
| route | 0.15 | Adjacent cameras 1.0; a return via a loop 0.6; skipped cameras `1 − 0.3·skipped` (min 0.2), since a skip suggests a missed detection. |
| attributes | 0.10 | Vehicle class and colour agreement. Near-colours (white/silver, grey/black …) score 0.4; car/truck/bus confusions at a distance score 0.4. |

The weights, and every threshold below, can be edited in **Settings → identity**. Changes
are validated and audited.

### Hard constraints (the candidate is rejected)

* **Physically impossible.** The elapsed time is below the shortest allowed road path's
  `min_travel_s × (1 − time_tolerance)`. A reason such as *"physically impossible: 22s
  from CAM-01 to CAM-06 but the shortest road path (4.1 km) needs ≥ 180s"* is recorded.
* **No allowed path.** No directed path exists that avoids edges marked `allowed = false`,
  such as the wrong direction of a one-way road. A confident plate can still carry the
  identity, but only as a *new journey*.
* **Plates disagree.** Two confident plate reads differ, compared both with the previous
  sighting and with the vehicle's best-known plate. This stops a plate-less intermediate
  sighting from "changing" a vehicle's registration.
* **No identifying signal.** Timing and route alone are never enough. Either the plate or
  the appearance must reach `min_signal_for_link` (0.75).
* **New journey without a plate.** The gap exceeds `journey_gap_s` (1800 s), or the path
  has more than 2 hops, and there is no confident plate.

## 3. Decision

* **No match.** If no candidate passes the hard constraints and reaches
  `accept_threshold` (0.62), the observation starts a **new vehicle** (`NEW`). The closest
  candidate and the reason it lost are still recorded.
* **Confidence level.** Otherwise the best candidate is linked:
  * `HIGH` if the score is ≥ 0.80;
  * `MEDIUM` if it is ≥ 0.65;
  * `LOW` if it is lower.
* **No plate corroboration.** A link without a strong, confident plate match is capped at
  `unconfirmed_plate_max_level` (`MEDIUM`). Appearance and timing cannot tell two similar
  white hatchbacks apart, so the system never reports such a link as certain.
* **Ambiguity.** When the runner-up is within `ambiguity_margin` (0.05) and there is no
  strong plate, the observation is **not linked** at all, because a wrong link is worse than
  a missed one. With a strong plate, the link is made but downgraded, and the runner-up is
  reported.
* **Cloned plates.** Same-plate candidates rejected as physically impossible are returned
  as **conflicts**. The IMPOSSIBLE_TRAVEL rule turns them into an alert that shows both
  sightings, their evidence and the travel-time arithmetic.

The observation stores `match_score`, `confidence_level`, `match_reasons` and the full
component breakdown (scores, effective weights, path, dt, runner-up). The observation
detail page shows all of it.

## 4. Road topology

The topology is a directed graph of camera-to-camera edges (`camera_edges`). Each edge
has:

* `distance_m`;
* `min_travel_s`, the physically plausible minimum;
* `typical_travel_s`, the free-flow prior;
* `road_name` and `direction`;
* `allowed`;
* optional road geometry (a GeoJSON LineString).

Dijkstra over the allowed edges gives the shortest path, the number of hops and the
skipped cameras. Edit the topology on the **Road Topology** page, which also has a path
finder that explains why two cameras are unreachable.

The demo network (`configs/demo_network.json`) ships real road polylines, so replays follow
streets instead of straight lines. No internet map tiles are used.

### Travel-time baselines

* For each edge, the baseline is the **median** travel time of HIGH and MEDIUM links over
  the last `congestion.baseline_s` (6 h). At least 5 samples are needed; below that, the
  edge's `typical_travel_s` prior is used.
* When the baseline is compared against current conditions (analytics, anomaly rule), the
  current window (`congestion.window_s`) is excluded from the history, so a jam does not
  hide itself. Identity matching uses all of the history.
* The baselines feed the identity time score, the TRAVEL_TIME_ANOMALY rule (recent median
  ≥ 1.5 × baseline) and the travel-time component of the congestion score.

## 5. Trajectories and journeys

Each observation becomes a `trajectory_point` with:

* `seq` and `journey_index`;
* `from_camera_id`;
* segment travel time, distance and implied speed;
* `link_score`, `confidence_level` (`START` for the first point of a journey) and
  `explanation`.

A long gap or a plate-carried re-appearance starts a new journey.

`GET /api/vehicles/{ref}/trajectory` returns the points plus the road geometry between
them. The **Play Journey** control on the vehicle page replays this along the offline road
network at 1×–60× speed. Segments are coloured by confidence level and dashed where
cameras on the route did not see the vehicle. The timeline highlights each stop, with its
match explanation, as the marker reaches it.

## 6. Predicted next camera

After each sighting, `predict_next` picks the most likely next camera:

1. **Candidates.** The allowed exits from the current camera. The camera the vehicle came
   from is excluded when there are other exits.
2. **Probabilities.** Laplace-smoothed transition counts from the last 24 h of trajectories
   (a uniform prior when there is no history). The response states which basis was used.
3. **Time window.** From `now + min_travel_s` to
   `now + max(2.5 × typical_travel_s, min_travel_s + 60 s)`.

The prediction's status is:

| status | meaning |
|---|---|
| `PENDING` | the window is open |
| `CONFIRMED` | the vehicle was next linked at the predicted camera within the window (+30 s) |
| `DEVIATED` | the vehicle was next seen at a different camera or outside the window |
| `EXPIRED` | the window passed without a linked sighting |

`GET /api/vehicles/{ref}/prediction` also returns the network-wide confirmed/deviated
counts for the last 24 h, which the vehicle page shows. This is an honest measure of how
predictable the network is.

## 7. Repeated sightings

The REPEATED_SIGHTING rule counts one vehicle's sightings at the same camera within
`window_s`. A vehicle circling a block, such as the demo's UP32KT7788 at CAM-05, produces a
LOW-severity, explainable alert after `min_sightings` (3) passes.

## Limitations

* Appearance embeddings come from an ImageNet backbone, not a vehicle-Re-ID-trained model.
  Appearance alone therefore never produces a HIGH link.
* The graph models camera-to-camera links, not every road in between. Vehicles that stop
  for a long time between cameras become new journeys.
* The prediction window is based on travel-time priors, so under congestion predictions
  deviate or expire more often. This is shown, not hidden.
