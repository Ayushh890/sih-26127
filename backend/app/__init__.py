"""NIRNAY backend package."""
import os as _os

from app.core.config import effective_cpu_count as _effective_cpu_count

# Native thread pools (OpenBLAS/OpenMP via numpy/scipy, OpenCV) size themselves to the host's
# core count, which in containers is often the whole machine (64+ cores) rather than the CPU
# quota. Every process would then spawn dozens of idle threads and can hit the container's
# thread limit. Cap them before numpy/cv2 are imported; ONNX Runtime is capped separately
# (INFERENCE_THREADS). Explicit environment values win.
_n = str(max(1, min(4, _effective_cpu_count() // 2)))
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OPENCV_FOR_THREADS_NUM"):
    _os.environ.setdefault(_var, _n)
