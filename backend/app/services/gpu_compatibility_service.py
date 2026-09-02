from __future__ import annotations

import re
from typing import Literal

from app.db.models import Env, Gpu, Node
from app.services.node_service import effective_gpu_compute_capability


GpuCompatibility = Literal[
    "native_supported",
    "same_major_compatible",
    "unsupported",
    "unknown",
]
SmTarget = tuple[int, int, str]


def pytorch_gpu_compatibility(env: Env | None, node: Node, gpu: Gpu) -> GpuCompatibility:
    """仅按 PyTorch 的 SM cubin 列表分类，compute/PTX 条目不参与判断。"""
    if env is None or not getattr(env, "pytorch_version", None):
        return "unknown"
    capability = effective_gpu_compute_capability(node, gpu)
    gpu_capability = parse_compute_capability(capability)
    if gpu_capability is None:
        return "unknown"
    targets = parse_sm_targets(getattr(env, "pytorch_arch_list", None) or [])
    if any((major, minor) == gpu_capability for major, minor, _ in targets):
        return "native_supported"
    # 普通 cubin 只保证同一主版本内从较低次版本向较高次版本兼容；a/f 等专用后缀不能套用该规则。
    if any(
        not suffix and major == gpu_capability[0] and minor <= gpu_capability[1]
        for major, minor, suffix in targets
    ):
        return "same_major_compatible"
    return "unsupported"


def parse_compute_capability(value: str | None) -> tuple[int, int] | None:
    """把 nvidia-smi 的 major.minor 形式转换为可按主、次版本比较的元组。"""
    parts = str(value or "").strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def parse_sm_targets(values: list[str]) -> list[SmTarget]:
    """只解析 sm 目标并保留 a/f 等后缀；compute/PTX 条目按产品策略完全忽略。"""
    targets: list[SmTarget] = []
    for value in values:
        match = re.fullmatch(r"sm_(\d+)([a-z]*)", str(value or "").strip().lower())
        if not match:
            continue
        architecture = int(match.group(1))
        major, minor = divmod(architecture, 10)
        targets.append((major, minor, match.group(2)))
    return targets
