"""ONNX Runtime session management and GPU/CPU provider selection."""
from __future__ import annotations

import threading
from pathlib import Path

import onnxruntime as ort

from app.core.logging import get_logger

log = get_logger("ml.runtime")

GPU_PROVIDERS = ("TensorrtExecutionProvider", "CUDAExecutionProvider", "ROCMExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider")

_sessions: dict[str, ort.InferenceSession] = {}
_lock = threading.Lock()


def select_providers(device: str) -> list[str]:
    available = ort.get_available_providers()
    if device in ("auto", "gpu"):
        gpu = [p for p in GPU_PROVIDERS if p in available and p != "TensorrtExecutionProvider"]
        if gpu:
            return [gpu[0], "CPUExecutionProvider"]
        if device == "gpu":
            log.warning("GPU inference requested but no GPU execution provider is available; using CPU")
    return ["CPUExecutionProvider"]


def get_session(path: Path, device: str, threads: int) -> ort.InferenceSession:
    key = f"{path}|{device}"
    with _lock:
        if key not in _sessions:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, threads)
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.log_severity_level = 3
            sess = ort.InferenceSession(str(path), sess_options=opts, providers=select_providers(device))
            log.info("loaded model %s providers=%s", path.name, sess.get_providers())
            _sessions[key] = sess
        return _sessions[key]


def processing_mode() -> str:
    """'GPU' if any loaded session runs on a GPU provider, else 'CPU'."""
    with _lock:
        for s in _sessions.values():
            if any(p in GPU_PROVIDERS for p in s.get_providers()):
                return "GPU"
    return "CPU"


def loaded_models() -> list[dict[str, object]]:
    with _lock:
        return [{"model": Path(k.split("|")[0]).name, "providers": s.get_providers()} for k, s in _sessions.items()]
