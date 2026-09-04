from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析远端任务启动参数，runner 只接收调度器生成的受控字段。"""
    parser = argparse.ArgumentParser(description="NebulaGrid remote task runner")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--runtime-path", required=True)
    parser.add_argument("--status-path", required=True)
    parser.add_argument("--cancel-path", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--launch-id", required=True, type=int)
    parser.add_argument("--cuda-visible-devices", default="")
    return parser.parse_args()


def main() -> None:
    """后台启动任务 wrapper，并写入 PID 元数据供 master 侧 executor/guard 追踪。"""
    args = parse_args()
    log_path = Path(args.log_path)
    runtime_path = Path(args.runtime_path)
    status_path = Path(args.status_path)
    cancel_path = Path(args.cancel_path)
    wrapper_path = runtime_path.with_suffix(".sh")
    process: subprocess.Popen | None = None
    process_identity: dict[str, int | str | None] = {}
    base_payload = {"task_id": args.task_id, "launch_id": args.launch_id}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        if cancellation_requested(cancel_path, args.launch_id):
            acknowledge_cancelled_launch(runtime_path, status_path, base_payload)
            print(json.dumps({**base_payload, "state": "cancelled", "process_stopped": True}, ensure_ascii=False))
            return

        wrapper_path.write_text(build_wrapper(args), encoding="utf-8")
        wrapper_path.chmod(0o700)
        # 在创建任何用户进程前由目标节点自己的 bash 校验 wrapper；语法错误可安全归类为未启动失败。
        subprocess.run(
            ["/bin/bash", "-n", str(wrapper_path)],
            cwd=args.workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        # launching 记录必须早于 Popen 落盘；master 看到它后即使 SSH 断开，也不会把 allocation 提前释放。
        atomic_write_json(
            runtime_path,
            {
                **base_payload,
                "state": "launching",
                "workdir": args.workdir,
                "log_path": args.log_path,
                "runtime_path": args.runtime_path,
                "status_path": args.status_path,
            },
        )
        if cancellation_requested(cancel_path, args.launch_id):
            acknowledge_cancelled_launch(runtime_path, status_path, base_payload)
            print(json.dumps({**base_payload, "state": "cancelled", "process_stopped": True}, ensure_ascii=False))
            return

        env = os.environ.copy()
        if args.cuda_visible_devices:
            env["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices
        else:
            # CPU-only 任务显式隐藏 GPU，避免框架默认探测全部设备。
            env["CUDA_VISIBLE_DEVICES"] = ""
        boot_id = read_boot_id()
        if not boot_id:
            raise RuntimeError("cannot read node boot id before launch")
        process = subprocess.Popen(
            ["/bin/bash", str(wrapper_path)],
            cwd=args.workdir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # start_new_session=True 保证新进程的 PID 同时是 PGID；尽早保存身份，异常分支才能继续回收。
        process_identity = {
            "pid": process.pid,
            "pgid": process.pid,
            "process_start_time": read_process_start_time(process.pid),
            "boot_id": boot_id,
        }
        if process_identity["process_start_time"] is None:
            # runner 仍持有 Popen 句柄，此时必须立即进入异常回收；缺启动时钟的 running
            # 回执会让 master 无法防范 PID 复用，只能永久停在停止中。
            raise RuntimeError("cannot read process start time after launch")
        metadata = {
            **base_payload,
            **process_identity,
            "state": "running",
            "workdir": args.workdir,
            "log_path": args.log_path,
            "runtime_path": args.runtime_path,
            "status_path": args.status_path,
        }
        atomic_write_json(runtime_path, metadata)
        # 停止请求可能恰好落在 Popen 与 PID 落盘之间，因此启动后必须再次检查共享标记。
        if cancellation_requested(cancel_path, args.launch_id):
            completed_payload = load_explicit_completion(status_path, args.task_id, args.launch_id)
            if completed_payload:
                # 极短任务可能已在停止标记到达前自然结束；明确返回码优先，不能被 cancelled 覆盖。
                print(json.dumps(completed_payload, ensure_ascii=False))
                return
            process_stopped = terminate_process_group(process)
            completed_payload = load_explicit_completion(status_path, args.task_id, args.launch_id)
            if completed_payload:
                # wrapper 可能在 TERM 与状态写入之间完成，终止分支再次读取以封住该竞态窗口。
                print(json.dumps(completed_payload, ensure_ascii=False))
                return
            cancelled_payload = {**metadata, "state": "cancelled", "process_stopped": process_stopped}
            atomic_write_json(runtime_path, cancelled_payload)
            # Popen 后 status 由 wrapper 独占写入，避免此处在最后一次读取后覆盖刚落盘的明确返回码。
            print(json.dumps(cancelled_payload, ensure_ascii=False))
            return
        print(json.dumps(metadata, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - runner 必须先回收已创建的进程，再报告启动阶段失败。
        completed_payload = load_explicit_completion(status_path, args.task_id, args.launch_id)
        if completed_payload:
            print(json.dumps(completed_payload, ensure_ascii=False))
            return
        process_stopped = terminate_process_group(process) if process is not None else True
        completed_payload = load_explicit_completion(status_path, args.task_id, args.launch_id)
        if completed_payload:
            # 异常处理不能抹掉 wrapper 已经落盘的真实退出码。
            print(json.dumps(completed_payload, ensure_ascii=False))
            return
        failure_payload = {
            **base_payload,
            **process_identity,
            "state": "launch_failed",
            "return_code": None,
            "process_stopped": process_stopped,
            "error": str(exc),
        }
        try:
            atomic_write_json(runtime_path, failure_payload)
            if process is None:
                # 尚未创建 wrapper 时不存在并发写者，可以安全写 status 作为“未启动”回执。
                atomic_write_json(status_path, failure_payload)
        except OSError:
            pass
        print(json.dumps(failure_payload, ensure_ascii=False))
        raise SystemExit(1) from None


def atomic_write_json(path: Path, payload: dict) -> None:
    """先写同目录临时文件再替换，避免 master 读到半截 JSON 而误判启动或停止状态。"""
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(path)


def read_json_file(path: Path) -> dict:
    """读取共享控制文件；缺失、半写入或非对象内容都按无有效指令处理。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cancellation_requested(cancel_path: Path, launch_id: int) -> bool:
    """仅接受当前 allocation 对应的停止标记，防止任务 ID 重用时旧标记误停新一次运行。"""
    payload = read_json_file(cancel_path)
    value = payload.get("launch_id")
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == launch_id
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip()) == launch_id
    return False


def load_explicit_completion(status_path: Path, task_id: str, launch_id: int) -> dict:
    """读取同一任务/launch 的明确整数退出码；存在时任何停止或异常回执都不得覆盖它。"""
    payload = read_json_file(status_path)
    if payload.get("task_id") != task_id or payload.get("launch_id") != launch_id:
        return {}
    return_code = payload.get("return_code")
    if isinstance(return_code, bool) or not isinstance(return_code, int):
        return {}
    return payload


def acknowledge_cancelled_launch(runtime_path: Path, status_path: Path, base_payload: dict) -> None:
    """确认任务在创建进程前已被停止，并同时写 runtime/status 供 executor 完成两阶段归档。"""
    payload = {**base_payload, "state": "cancelled", "return_code": None, "process_stopped": True}
    atomic_write_json(runtime_path, payload)
    atomic_write_json(status_path, payload)


def read_process_start_time(pid: int) -> int | None:
    """记录 Linux /proc 启动时钟值，后续可用于区分原进程和被系统复用的相同 PID。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm 字段允许包含空格，因此从最后一个右括号后解析；starttime 是余下字段中的第 20 项。
        fields = raw[raw.rfind(")") + 2 :].split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def read_boot_id() -> str:
    """读取本次 Linux 启动的唯一 ID，防止节点重启后相同 PID/starttime 组合误杀新进程。"""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def terminate_process_group(process: subprocess.Popen | None) -> bool:
    """runner 启动窗口收到停止标记时先 TERM 后 KILL，并确认整个进程组没有存活成员。"""
    if process is None:
        return True
    # start_new_session=True 保证根 PID 同时也是 PGID；即使根进程先退出，也仍可核查遗留子进程。
    process_group_id = process.pid
    if not process_group_has_live_members(process_group_id):
        return True
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return not process_group_has_live_members(process_group_id)
    except PermissionError:
        return False
    if wait_for_process_group_exit(process_group_id, timeout=1.0):
        return True
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return not process_group_has_live_members(process_group_id)
    except PermissionError:
        return False
    return wait_for_process_group_exit(process_group_id, timeout=2.0)


def wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    """轮询 /proc 直到进程组无非僵尸成员；超时返回 False，让 master 保持 stopping。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_group_has_live_members(process_group_id):
            return True
        time.sleep(0.05)
    return not process_group_has_live_members(process_group_id)


def process_group_has_live_members(process_group_id: int) -> bool:
    """只有明确扫描为空才返回 False；/proc 权限或解析异常一律按仍可能存活处理。"""
    return process_group_state(process_group_id) != "empty"


def process_group_state(process_group_id: int) -> str:
    """返回 live/empty/unknown，避免 /proc 读取失败被误解释为进程组已经退出。"""
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return "unknown"
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            state = fields[0]
            member_group_id = int(fields[2])
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                # 枚举后进程恰好退出属于正常竞态，不影响其余成员检查。
                continue
            return "unknown"
        except (IndexError, ValueError):
            return "unknown"
        if member_group_id == process_group_id and state != "Z":
            return "live"
    return "empty"


def build_wrapper(args: argparse.Namespace) -> str:
    """生成等待用户命令完成的 wrapper，退出码会写入 status 文件。"""
    status_path = shell_quote(args.status_path)
    log_path = shell_quote(args.log_path)
    command = shell_quote(args.command)
    task_id = shell_quote(args.task_id)
    launch_id = shell_quote(str(args.launch_id))
    python_executable = shell_quote(sys.executable)
    outer_failure_payload = shell_quote(
        json.dumps(
            {
                "task_id": args.task_id,
                "launch_id": args.launch_id,
                "state": "wrapper_failed",
                "return_code": None,
                "process_stopped": False,
                "error": "wrapper relay failed before a user return code was recorded",
            },
            ensure_ascii=False,
        )
    )
    outer_status_prefix = shell_quote(f"{args.status_path}.wrapper")
    return f"""#!/bin/bash
set +e
mkdir -p "$(dirname {log_path})" "$(dirname {status_path})"
echo "[NebulaGrid] task started at $(date --iso-8601=seconds)" >> {log_path}
NEBULAGRID_COMMAND={command} NEBULAGRID_LOG_PATH={log_path} NEBULAGRID_STATUS_PATH={status_path} NEBULAGRID_TASK_ID={task_id} NEBULAGRID_LAUNCH_ID={launch_id} {python_executable} - <<'PY' 2>/dev/null
import errno
import json
import os
import pty
import select
import subprocess
import time
from pathlib import Path

command = os.environ["NEBULAGRID_COMMAND"]
log_path = os.environ["NEBULAGRID_LOG_PATH"]
status_path = Path(os.environ["NEBULAGRID_STATUS_PATH"])
task_id = os.environ.get("NEBULAGRID_TASK_ID", "")
launch_id = int(os.environ.get("NEBULAGRID_LAUNCH_ID", "0"))


def write_status(payload):
    # 状态文件必须原子替换；master 只会把整数 return_code 当作用户程序的明确结果。
    temporary_path = status_path.with_name(f"{{status_path.name}}.{{os.getpid()}}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(status_path)


def terminate_direct_child(process):
    # 基础设施异常时先尽力停止直接子进程；process_stopped 仍保持 false，完整进程组由 executor 核验。
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=0.5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass

# 这里不用普通 shell 重定向，而是让用户命令跑在伪终端里，再按 chunk 立即写入日志。
# 这样 Python、训练框架和进度条通常会按交互式输出刷新，避免等缓冲区满后才一次性落盘。
master_fd = None
slave_fd = None
log_fd = None
process = None
try:
    master_fd, slave_fd = pty.openpty()
    # 先打开日志再创建用户进程，避免最常见的日志权限错误发生在 Popen 之后。
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_SYNC", 0), 0o644)
    process = subprocess.Popen(
        ["/bin/bash", "-lc", command],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    slave_fd = None
    while True:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if master_fd in ready:
            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                os.write(log_fd, data)
            elif process.poll() is not None:
                break
        if process.poll() is not None:
            try:
                while True:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(log_fd, data)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
            break
    return_code = process.wait()
    # 一拿到用户进程返回码就先落状态；完成日志属于辅助信息，不能扩大返回码丢失窗口。
    write_status(
        {{
            "task_id": task_id,
            "launch_id": launch_id,
            "state": "finished",
            "return_code": return_code,
            "finished_at": time.time(),
        }}
    )
    try:
        os.write(
            log_fd,
            f"[NebulaGrid] task finished at {{time.strftime('%Y-%m-%dT%H:%M:%S%z')}} with code {{return_code}}\\n".encode(
                "utf-8"
            ),
        )
    except OSError:
        pass
except BaseException as exc:
    completed_return_code = process.poll() if process is not None else None
    if isinstance(completed_return_code, int):
        # PTY drain 或日志写入可能在用户进程已经退出后失败；此时 poll() 就是明确用户返回码，
        # 应先保存真实结果并让 relay 正常退出，不能把辅助日志故障降级成 wrapper_failed。
        try:
            write_status(
                {{
                    "task_id": task_id,
                    "launch_id": launch_id,
                    "state": "finished",
                    "return_code": completed_return_code,
                    "finished_at": time.time(),
                }}
            )
        except OSError:
            pass
        else:
            if log_fd is not None:
                try:
                    os.write(
                        log_fd,
                        f"[NebulaGrid] task finished with code {{completed_return_code}}; log relay warning: {{exc}}\\n".encode(
                            "utf-8"
                        ),
                    )
                except OSError:
                    pass
            raise SystemExit(0)
    # PTY、日志或转发器异常不是用户程序返回码。即使直接子进程已退出，也不能证明其后代均已退出。
    terminate_direct_child(process)
    failure_payload = {{
        "task_id": task_id,
        "launch_id": launch_id,
        "state": "wrapper_failed",
        "return_code": None,
        "process_stopped": False,
        "error": str(exc),
    }}
    try:
        write_status(failure_payload)
    except OSError:
        pass
    if log_fd is not None:
        try:
            os.write(log_fd, f"[NebulaGrid] wrapper infrastructure failed: {{exc}}\\n".encode("utf-8"))
        except OSError:
            pass
    raise
finally:
    for descriptor in (log_fd, master_fd, slave_fd):
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass
PY
relay_code=$?
if [ "$relay_code" -ne 0 ]; then
  # 嵌入 Python 若在导入或日志重定向前就无法启动，也必须留下无返回码失败回执；
  # 外层继续存活，使 executor 能凭原始 PID/session 安全回收整组进程。
  outer_status_tmp={outer_status_prefix}."$$"
  printf '%s\n' {outer_failure_payload} > "$outer_status_tmp" && mv -f "$outer_status_tmp" {status_path}
  # 保留最初的 wrapper PID/session 作为可信回收锚点；executor 确认并终止整个进程组后才会归档。
  while true; do sleep 60; done
fi
exit 0
"""


def shell_quote(value: str) -> str:
    """轻量 shell 引号，避免日志路径中空格破坏 wrapper。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    main()
