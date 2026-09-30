# NIRNAY models

All inference runs locally on CPU (or GPU when `onnxruntime-gpu` is installed and
`INFERENCE_DEVICE=gpu`) through ONNX Runtime. Nothing is sent to a cloud service.
Which file is used for which stage is configured in [`configs/models.yaml`](../configs/models.yaml);
no code references a model path directly.

```
python scripts/download_models.py            # download missing files (SHA-256 pinned)
python scripts/download_models.py --verify   # verify checksums only
```

The Docker image runs the download at build time (`--build-arg DOWNLOAD_MODELS=0` to skip,
e.g. in an air-gapped build where this folder is copied in instead).

| File | Stage | Source | Licence |
|---|---|---|---|
| `yolox_nano.onnx` | vehicle detection (COCO car / motorcycle / bus / truck) | [Megvii YOLOX 0.1.1rc0](https://github.com/Megvii-BaseDetection/YOLOX) | Apache-2.0 |
| `yolox_tiny.onnx` (optional) | higher-accuracy vehicle detector | same | Apache-2.0 |
| `yolo-v9-t-384-license-plates-end2end.onnx` | licence-plate detection | [ankandrew/open-image-models](https://github.com/ankandrew/open-image-models) | MIT |
| `cct_s_v2_global.onnx` + `cct_s_v2_global_plate_config.yaml` | plate OCR (character sequence + per-character confidence) | [ankandrew/fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) | MIT |
| `mobilenetv2-12.onnx` | base network for Re-ID features | [ONNX model zoo](https://github.com/onnx/models) (torchvision MobileNetV2) | Apache-2.0 |
| `reid_mobilenetv2_gap.onnx` | vehicle appearance embedding (1280-d) | **derived locally** by `download_models.py`: the MobileNetV2 graph with its global-average-pool output (node `464`) exposed | Apache-2.0 (derivative) |

## Fallbacks

If a model file is missing or fails to load the pipeline does not crash; it switches to the
fallback declared in `models.yaml` and reports it on the **System** page and in `/ready`:

| Stage | Fallback | Effect |
|---|---|---|
| vehicle detector | `motion` (background subtraction) | vehicles detected without class labels |
| plate detector | `contour` (edge/aspect heuristics) | lower plate recall |
| Re-ID | `color_histogram` | appearance matching is weaker; identity leans on plate + topology |

OCR has no fallback: without it plates are not read and identity relies on appearance and
topology only (every match then carries at most MEDIUM confidence).

## Known limitations of the default weights

- The COCO detector has no `auto_rickshaw` / `e-rickshaw` classes; they are usually detected as
  `car` or `motorcycle`. A detector fine-tuned on an Indian dataset (e.g. IDD) can be dropped in
  by editing `vehicle_detector.path` and the `classes` map.
- The OCR model is a *global* plate model, not trained specifically on Indian plates. NIRNAY
  compensates with Indian-format normalisation (confusion-aware correction of O/0, I/1, B/8 …
  by position), multi-frame temporal voting and a validity check, and it keeps the raw OCR text
  next to the normalised plate for audit.
- No face or person model is shipped or supported. NIRNAY identifies vehicles only.
