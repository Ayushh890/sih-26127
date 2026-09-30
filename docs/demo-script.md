# Five-minute demo script

This script shows NIRNAY end to end on the built-in synthetic network: six **DEMO** cameras
on a Lucknow central corridor. Every frame goes through the real pipeline (detection,
tracking, plate detection, OCR, identity, trajectory, analytics, alerts); only the camera
source is synthetic. All demo data is labelled DEMO / synthetic in the UI and the API.

## Before the jury arrives

1. Start the platform with `./run.sh` and wait for <http://localhost:3000>.
2. Log in as `operator` (password `nirnay-demo`, or your `DEMO_PASSWORD`).
3. On the Command Center, check that the **Demo timeline** panel shows the scenario
   position, and that all six cameras are ONLINE.
4. Start about **10 minutes early**. The scenario runs in 600-second cycles, and every
   scripted event repeats in every cycle. After one full cycle all the alerts below exist,
   and the current cycle replays the events live while you present. To start from t = 0,
   press **Restart demo** on the Command Center. This purges demo data only; live data is
   never touched.

### Scripted events (seconds into each cycle)

| t (s) | event | camera | what the platform should do |
|---|---|---|---|
| 30 | Tracked sedan **UP32GM2024** starts route A (CAM-01 → 02 → 03 → 04 → 06) | CAM-01 | One global vehicle and a growing trajectory; next-camera predictions confirmed along the route |
| 40, 235, 430 | **UP32KT7788** circles the block | CAM-05 | REPEATED_SIGHTING (LOW) on the 3rd pass |
| 60 | SUV with an obscured plate on route A | CAM-01 … | Linked by appearance only, at LOW/MEDIUM confidence, never HIGH |
| 100 → 145 | **UP32HN4412** seen at CAM-01, then 45 s later at CAM-06 on a different vehicle (a cloned plate) | CAM-01, CAM-06 | IMPOSSIBLE_TRAVEL (HIGH) with the travel-time arithmetic |
| 180 – 510 | Congestion builds and clears | CAM-03 | Congestion score rises to HEAVY/SEVERE with an explanation, then falls; congestion alerts auto-resolve |
| 210 | **UP32FW3190** drives against the one-way | CAM-04 | WRONG_WAY (HIGH) |

The Command Center's timeline panel lists these events with their wall-clock times and
marks each one as done or upcoming.

## The script (≈ 5 minutes)

**1. The problem, in one screen (0:00–0:30).** Command Center.
"Each camera only sees a vehicle for a few seconds. NIRNAY turns many cameras into one
city-wide view." Point out:
* the map with cameras coloured by congestion score;
* the KPI row;
* the live-detections feed, where every row carries a match score and confidence level;
* the open alerts.

Note the DEMO/synthetic banner: synthetic data is never mixed silently with live data.

**2. Real cameras, real pipeline (0:30–0:55).** Go to Cameras and open CAM-03.
* The live MJPEG preview, with the **overlay** toggle switching between the detection,
  track and plate overlay and the raw frame.
* The input and processing fps, queue and latency.
* "The same page works for RTSP, HTTP/MJPEG, webcam and file sources, and ONVIF discovery
  is under Add camera."

**3. One vehicle, many cameras (0:55–1:30).** Vehicle Search: search `UP32GM2024` and
open the vehicle.
* The trajectory across CAM-01 → CAM-06, each hop with its score and reasons (plate,
  appearance, time against baseline, route).
* Press **▶ Play Journey**. The replay follows the real road geometry on the offline map.
* The **predicted next camera** and its status (PENDING, CONFIRMED or DEVIATED), plus the
  network-wide prediction accuracy.

**4. Explainable ANPR (1:30–2:00).** Click one sighting to open the observation detail.
* Every OCR read with its raw text, the Indian-format corrections (e.g. `O→0` in a digit
  position) and the temporal vote that produced the final plate.
* The evidence package: vehicle and plate crops plus the before, detection and after
  frames. Press **Verify integrity** to re-hash the files against the SHA-256 manifest.

**5. Honest uncertainty (2:00–2:20).** Vehicle Search → *unreadable plate* filter, and open
the obscured-plate SUV.
* It was linked by appearance and timing only, so the link is capped at MEDIUM (often
  LOW) and the reason says so.
* "We never present a guess as a certainty. When two candidates are too close to call,
  NIRNAY refuses to link at all."

**6. Cloned plate (2:20–2:50).** Alerts → IMPOSSIBLE_TRAVEL for `UP32HN4412`.
* The explanation: both sightings, the shortest road distance, and the minimum physically
  possible time against the observed 45 s.
* Both evidence packages side by side.
* **Acknowledge** with a note. This is logged in the alert's history and in the audit log.

**7. Wrong-way driver (2:50–3:05).** Alerts → WRONG_WAY at CAM-04 for `UP32FW3190`.
It shows the direction of motion against the camera's configured one-way flow, and the
number of track frames it is based on.

**8. Congestion you can explain (3:05–3:40).** Analytics for CAM-03, while the jam is
active (t = 180–510 s) or from the previous cycle.
* The congestion score and level with its weighted components: speed, occupancy, queue,
  volume and travel time.
* The travel-time anomaly on the segments into CAM-03.
* Hotspots and the OD matrix.
* Then **Emergency Corridor**: plan a route from CAM-01 to CAM-06. Segment costs are
  inflated by congestion, so the plan prefers a way around the jam when the network has
  one. The page is clearly labelled **simulation only**. NIRNAY never controls
  traffic signals.

**9. Repeated sightings (3:40–3:55).** Alerts → REPEATED_SIGHTING for `UP32KT7788` at
CAM-05: three passes within the window, each sighting listed with its time and evidence.

**10. Resilience (3:55–4:25).** Cameras → CAM-02 → **Simulate disconnect** (30 s).
* The camera goes OFFLINE, reconnects with backoff and returns ONLINE; the status strip
  updates live.
* A CAMERA_OFFLINE alert opens, then auto-resolves on recovery (recorded in its history as
  `system`).
* The other five cameras are unaffected.
* Show the camera's **Health** tab: the score, the factors and a concrete recommendation.

**11. Privacy and governance (4:25–5:00).** Log out and log in as `analyst`.
* Plates now appear as `PSN-…` pseudonyms everywhere, including the live feed, and
  evidence cannot be opened. Aggregate analytics still work.
* Log in as `admin` and open the **Audit Log**. It shows the searches you ran, the
  acknowledgement and the evidence verification.
* Close with: "Vehicles and traffic only. There is no face recognition and no person
  identification. Alerts come only from explicit, configurable rules. Retention, privacy
  and every threshold are in Settings, and every change is audited."

## If something does not look right

* **An alert is missing.** Check the demo timeline. The event may not have happened yet
  in this cycle, and the alert from the previous cycle may have been resolved. Rules can
  be disabled in Settings → alerts.
* **A camera shows DEGRADED.** The machine is CPU-bound (processing fps below target).
  This is the health monitor working correctly. Lower `DEMO_PROCESSING_FPS`, or use
  `DEMO_SOURCE=recorded`.
* **You want a clean slate.** Use **Restart demo**. It purges demo data only.
