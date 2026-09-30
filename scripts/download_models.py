#!/usr/bin/env python3
"""Download the default ONNX models into MODELS_DIR and prepare derived files.

    python scripts/download_models.py            # download missing models
    python scripts/download_models.py --verify   # only verify checksums

All models are permissively licensed (see models/README.md). Checksums are pinned so a
tampered or truncated download is detected. The Re-ID feature extractor is derived
locally from the ONNX-model-zoo MobileNetV2 classifier by exposing its global-average-
pooled 1280-d feature tensor (node "464") as the graph output.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = Path(os.environ.get("MODELS_DIR", ROOT / "models"))

MODELS = [
    ("yolox_nano.onnx", "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
     "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"),
    ("yolo-v9-t-384-license-plates-end2end.onnx",
     "https://github.com/ankandrew/open-image-models/releases/download/assets/yolo-v9-t-384-license-plates-end2end.onnx",
     "888397b96d761c89db40bc9c305838e8652660f5e282c2cadebbe8d2951a77a8"),
    ("cct_s_v2_global.onnx", "https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/cct_s_v2_global.onnx",
     "384bbbd2cea3ef54761d3df70822ef3a349ee1a112aeafddbe0e3ba06bc6e47b"),
    ("cct_s_v2_global_plate_config.yaml", "https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates/cct_s_v2_global_plate_config.yaml",
     "0335c74a305173bb6f393efed0fde03cadeaa0b649ed8e19f431016d8232d0a6"),
    ("mobilenetv2-12.onnx", "https://github.com/onnx/models/raw/main/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx",
     "c0c3f76d93fa3fd6580652a45618618a220fced18babf65774ed169de0432ad5"),
]
OPTIONAL = [
    ("yolox_tiny.onnx", "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx",
     "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"),
]
REID_OUT = "reid_mobilenetv2_gap.onnx"
REID_NODE = "464"  # output of GlobalAveragePool, shape [N, 1280, 1, 1]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, url: str, digest: str) -> bool:
    dest = MODELS_DIR / name
    if dest.exists() and sha256(dest) == digest:
        print(f"  ok        {name}")
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  download  {name}  <- {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED    {name}: {e}")
        tmp.unlink(missing_ok=True)
        return False
    got = sha256(tmp)
    if got != digest:
        print(f"  FAILED    {name}: checksum mismatch ({got})")
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def build_reid() -> bool:
    src, dst = MODELS_DIR / "mobilenetv2-12.onnx", MODELS_DIR / REID_OUT
    if dst.exists():
        print(f"  ok        {REID_OUT}")
        return True
    if not src.exists():
        print(f"  skip      {REID_OUT}: mobilenetv2-12.onnx missing")
        return False
    import onnx
    from onnx import utils

    model = onnx.load(str(src))
    tmp = MODELS_DIR / "_mnv2_tmp.onnx"
    onnx.save(model, str(tmp))
    utils.extract_model(str(tmp), str(dst), input_names=[model.graph.input[0].name], output_names=[REID_NODE])
    tmp.unlink(missing_ok=True)
    m = onnx.load(str(dst))
    m.graph.output[0].name = REID_NODE
    m.producer_name = "nirnay-reid-extract"
    m.doc_string = "MobileNetV2 (ONNX model zoo, Apache-2.0) truncated at global average pooling: 1280-d features."
    onnx.checker.check_model(m)
    onnx.save(m, str(dst))
    print(f"  built     {REID_OUT} (features from node {REID_NODE})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="verify checksums only")
    ap.add_argument("--with-optional", action="store_true", help="also fetch alternative models (YOLOX-tiny)")
    args = ap.parse_args()
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"models dir: {MODELS_DIR}")
    items = MODELS + (OPTIONAL if args.with_optional else [])
    if args.verify:
        bad = 0
        for name, _, digest in items:
            p = MODELS_DIR / name
            state = "missing" if not p.exists() else ("ok" if sha256(p) == digest else "CHECKSUM MISMATCH")
            bad += state != "ok"
            print(f"  {state:9s} {name}")
        return 1 if bad else 0
    ok = all([download(*m) for m in items])
    ok = build_reid() and ok
    print("done" if ok else "some models could not be prepared; the system falls back where it can (see /system)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
