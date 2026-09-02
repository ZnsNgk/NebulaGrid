import logging
import shlex
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time_utils import local_datetime
from app.db.models import Env, EnvInstallJob, EnvPackage, Node
from app.db.session import SessionLocal
from app.services.env_service import env_model_to_info, refresh_env_pytorch_metadata, run_env_install_command, tail_text, write_env_operation_log
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def env_install_tick() -> None:
    """领取一个环境安装作业并执行，避免大包安装占用 API 请求线程。"""
    job_id = claim_next_job()
    if job_id is None:
        logger.info("env install worker observed 0 queued jobs")
        return
    run_install_job(job_id)


def claim_next_job() -> int | None:
    """用行锁领取 queued 作业，多 worker 部署时不会重复执行同一条安装任务。"""
    with SessionLocal() as db:
        job = db.scalar(
            select(EnvInstallJob)
            .where(EnvInstallJob.status == "queued")
            .order_by(EnvInstallJob.created_at.asc(), EnvInstallJob.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "running"
        job.started_at = local_datetime()
        db.commit()
        return job.id


def run_install_job(job_id: int) -> None:
    """执行已领取的安装作业，并把状态、返回码和日志统一写回数据库。"""
    with SessionLocal() as db:
        job = db.get(EnvInstallJob, job_id)
        if job is None or job.status == "cancelled":
            return
        env = db.get(Env, job.env_id)
        package = db.get(EnvPackage, job.package_id)
        if env is None or package is None:
            finish_job(db, job, package, "failed", None, "环境或安装包记录不存在")
            return
        package.status = "installing"
        db.commit()
        env_info = env_model_to_info(env, db)
        package_type = package.package_type
        command = job.command or build_default_package_command(package)
        cwd = Path(job.workdir) if job.workdir else None
        env_vars = build_install_env_vars(job, db)
    try:
        if job.target_node_id and job.mode == "compile":
            result = run_remote_install(job_id, command)
        else:
            result = run_env_install_command(env_info, package_type, command, cwd=cwd, env_vars=env_vars)
        status = "succeeded" if result.ok else "failed"
        return_code = result.return_code
        message = "环境安装作业完成" if result.ok else "环境安装作业失败"
    except Exception as exc:  # noqa: BLE001 - worker 必须把异常转为作业失败，避免队列卡死。
        logger.exception("env install job %s failed", job_id)
        status = "failed"
        return_code = None
        message = f"环境安装作业异常: {exc}"
    with SessionLocal() as db:
        job = db.get(EnvInstallJob, job_id)
        package = db.get(EnvPackage, job.package_id) if job is not None else None
        if job is not None:
            finish_job(db, job, package, status, return_code, message)
    if status == "succeeded":
        # 安装作业可能升级或移除 torch 依赖，完成后刷新 environments 中的兼容性元数据。
        refresh_env_pytorch_metadata(env_info.id)


def run_remote_install(job_id: int, command: str):
    """在指定计算节点执行 compile 安装；要求 NFS 路径和远端脚本已完成部署自检。"""
    with SessionLocal() as db:
        job = db.get(EnvInstallJob, job_id)
        if job is None:
            raise ValueError("install job not found")
        node = db.get(Node, job.target_node_id)
        env = db.get(Env, job.env_id)
        if node is None or env is None:
            raise ValueError("target node or env not found")
        env_vars = build_install_env_vars(job, db)
        remote_command = build_remote_install_command(env, job, command, env_vars)
        completed = subprocess.run(build_ssh_command(node, remote_command), check=False, capture_output=True, text=True, timeout=1800)
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
        return_code = parse_remote_return_code(output, completed.returncode)
        from app.schemas.envs import EnvLocalPackageInstallResult

        return EnvLocalPackageInstallResult(
            ok=return_code == 0,
            env_id=env.id,
            env_name=env.name,
            method=job.mode,
            command=command,
            return_code=return_code,
            stdout=tail_text(output, 12000),
            stderr="",
            log_path=job.log_path,
        )


def build_remote_install_command(env: Env, job: EnvInstallJob, command: str, env_vars: dict[str, str] | None = None) -> str:
    """构造远端安装命令，优先使用 env_installer.py；复杂 conda 命令退回 bash 激活执行。"""
    settings = get_settings()
    env_prefix = build_shell_env_prefix(env_vars or {})
    activation = build_remote_conda_activation(env, settings)
    command_parts = shlex.split(command)
    if command_parts[:4] == ["python", "-m", "pip", "install"] and len(command_parts) == 5 and not job.workdir:
        package_path = shlex.split(command)[-1]
        env_python = f"{env.path.rstrip('/')}/bin/python"
        installer = f"{settings.remote_code_root.rstrip('/')}/env_installer.py"
        installer_command = shlex.join([settings.miniconda_python, installer, "--python", env_python, "--package", package_path, "--log-path", job.log_path])
        return f"{activation} && {env_prefix}{installer_command}"
    workdir_prefix = f"cd {shlex.quote(job.workdir)} && " if job.workdir else ""
    return f"{activation} && {workdir_prefix}{env_prefix}{command}"


def build_remote_conda_activation(env: Env, settings) -> str:
    """先加载远端用户配置再激活目标环境，系统 nvcc 由 .bashrc 中的 PATH 提供。"""
    activate = Path(settings.miniconda_python).parent / "activate"
    return f"source ~/.bashrc && source {shlex.quote(str(activate))} && conda activate {shlex.quote(env.name)}"


def build_install_env_vars(job: EnvInstallJob, db: Session) -> dict[str, str]:
    """根据用户在编译弹窗中的 GPU 选择生成环境变量；默认模式不限制 CUDA 可见性。"""
    visibility = getattr(job, "gpu_visibility", None) or "default"
    if visibility == "default":
        return {}
    if visibility == "gpu":
        indices = [str(index) for index in (job.visible_gpu_indices or [])]
        return {"CUDA_VISIBLE_DEVICES": ",".join(indices)}
    if visibility == "cpu":
        gpu_count = count_target_gpus(job, db)
        return {"CUDA_VISIBLE_DEVICES": str(gpu_count + 1)}
    return {}


def count_target_gpus(job: EnvInstallJob, db: Session) -> int:
    """CPU 模式按目标节点 GPU 总数加一设置不可见编号，让 CUDA 程序自然回退到 CPU。"""
    if job.target_node_id:
        node = db.get(Node, job.target_node_id)
        return len(node.gpus or []) if node is not None else 0
    try:
        output = subprocess.check_output(["bash", "-lc", "nvidia-smi -L 2>/dev/null | wc -l"], text=True, timeout=5)
        return int(output.strip() or "0")
    except Exception:
        return 0


def build_shell_env_prefix(env_vars: dict[str, str]) -> str:
    """把环境变量转换成 shell 前缀，所有值都经 shlex.quote 处理以避免命令注入。"""
    if not env_vars:
        return ""
    return "".join(f"{key}={shlex.quote(value)} " for key, value in env_vars.items())


def build_ssh_command(node: Node, remote_command: str) -> list[str]:
    """构造批处理 SSH 命令，保持与 task executor 的连接参数一致。"""
    bash_command = f"bash -lc {shlex.quote(remote_command)}"
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        bash_command,
    ]


def parse_remote_return_code(output: str, default: int = 1) -> int:
    """解析远端 env_installer JSON 输出；非 JSON 输出视为失败并保留日志。"""
    import json

    try:
        payload = json.loads(output.splitlines()[-1])
        return int(payload.get("return_code", 1))
    except Exception:
        return default


def finish_job(
    db: Session,
    job: EnvInstallJob,
    package: EnvPackage | None,
    status: str,
    return_code: int | None,
    message: str,
) -> None:
    """写回安装作业终态，并同步包状态和环境操作日志。"""
    if job.status == "cancelled":
        # 运行中取消目前不能抢占正在执行的 pip/conda 进程，但不覆盖用户已经写入的取消终态。
        return
    job.status = status
    job.return_code = return_code
    job.finished_at = local_datetime()
    if package is not None:
        package.status = "installed" if status == "succeeded" else status
    env = db.get(Env, job.env_id)
    db.commit()
    if env is not None:
        write_env_operation_log(
            env_model_to_info(env, db),
            "package_install",
            message,
            job.created_by,
            {"job_id": job.id, "return_code": return_code, "status": status},
        )


def build_default_package_command(package: EnvPackage) -> str:
    """为旧库中缺少 command 的 queued 作业补一个保守安装命令。"""
    package_path = shlex.quote(package.file_path)
    if package.package_type in {"conda", "conda_archive", "tar_bz2"} or package.file_path.endswith(".tar.bz2"):
        return f"conda install --offline -y {package_path}"
    return f"python -m pip install {package_path}"


def main() -> None:
    """环境安装 worker 入口，与普通训练任务队列隔离运行。"""
    run_forever("nebulagrid-envworker", 5, env_install_tick)


if __name__ == "__main__":
    main()
