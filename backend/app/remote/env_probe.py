import json
import platform
import subprocess
import sys


def framework_missing(error=None):
    """返回统一的框架缺失结构，避免主服务为导入失败再做特殊解析。"""
    return {"installed": False, "error": error}


def probe_torch():
    """检测 PyTorch 版本和 CUDA/cuDNN 状态；导入失败时把错误返回给页面展示。"""
    try:
        import torch

        cudnn_version = None
        if hasattr(torch.backends, "cudnn"):
            cudnn_version = torch.backends.cudnn.version()
        return {
            "installed": True,
            "version": getattr(torch, "__version__", None),
            "cuda": getattr(getattr(torch, "version", None), "cuda", None),
            "cudnn": cudnn_version,
            "cuda_available": bool(torch.cuda.is_available()),
            "gpu_count": int(torch.cuda.device_count()),
        }
    except Exception as exc:
        return framework_missing(str(exc))


def probe_tensorflow():
    """检测 TensorFlow 版本和 CUDA/cuDNN 状态；兼容不同 TensorFlow 构建信息字段。"""
    try:
        import tensorflow as tf

        build = {}
        try:
            build = tf.sysconfig.get_build_info()
        except Exception:
            build = {}
        return {
            "installed": True,
            "version": getattr(tf, "__version__", None),
            "cuda": build.get("cuda_version"),
            "cudnn": build.get("cudnn_version"),
            "cuda_available": bool(tf.config.list_physical_devices("GPU")),
            "gpu_count": len(tf.config.list_physical_devices("GPU")),
        }
    except Exception as exc:
        return framework_missing(str(exc))


def list_packages():
    """优先读取 conda list，确保包列表与 conda 环境视角一致；失败时再回退到 Python 元数据。"""
    try:
        conda_list = subprocess.run(
            ["conda", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return [
            {"name": item.get("name", ""), "version": item.get("version", "")}
            for item in json.loads(conda_list.stdout)
            if item.get("name")
        ]
    except Exception:
        pass
    try:
        from importlib import metadata as importlib_metadata
    except Exception:
        return []
    return [
        {"name": item.metadata.get("Name", item.name), "version": item.version}
        for item in importlib_metadata.distributions()
    ]


def main():
    """输出环境检测 JSON；调用方需要先完成 conda activate。"""
    packages = sorted(list_packages(), key=lambda item: item["name"].lower())
    print(
        json.dumps(
            {
                "python_executable": sys.executable,
                "python_version": platform.python_version(),
                "pytorch": probe_torch(),
                "tensorflow": probe_tensorflow(),
                "packages": packages,
                "package_count": len(packages),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
