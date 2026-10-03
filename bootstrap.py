"""独立服务的下载、锁定安装及中断恢复，不依赖 AstrBot。"""
from __future__ import annotations

import asyncio
import codecs
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)
DEFAULT_ARCHIVE_URL: str | None = None
INSTALL_TIMEOUT_SECONDS = 1200
DOWNLOAD_TIMEOUT_SECONDS = 300
Runner = Callable[[list[str], Path], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class StepResult:
    ok: bool
    detail: str = ""


def redact(text: str) -> str:
    """去除 URL 认证、查询串和常见凭据，安装输出也不直接暴露。"""
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[认证已隐藏]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[参数已隐藏]", text)
    text = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)\S+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|authorization)[\"']?\s*[:=]\s*)([\"'])(.*?)\2", r"\1\2[已隐藏]\2", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|authorization)[\"']?\s*[:=]\s*)[^\s,}]+", r"\1[已隐藏]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)


def venv_python(instance: Path) -> Path:
    return instance / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def venv_launcher(instance: Path) -> Path:
    return instance / "venv" / ("Scripts/stock-robot.exe" if sys.platform == "win32" else "bin/stock-robot")


def spawn_kwargs() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimit), ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """挂起创建、挂接 Job 后恢复，避免子进程先逃离所有权边界。"""

    def __init__(self) -> None:
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        kernel = self._kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self._ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self._ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _JobExtendedLimit()
        information.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: asyncio.subprocess.Process) -> None:
        # SET_QUOTA | TERMINATE | SUSPEND_RESUME，句柄仅覆盖此次创建的 PID。
        handle = self._kernel.OpenProcess(0x100 | 0x1 | 0x800, False, process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            status = self._ntdll.NtResumeProcess(handle)
            if status:
                raise ctypes.WinError(self._ntdll.RtlNtStatusToDosError(status))
        finally:
            if not self._kernel.CloseHandle(handle):
                logger.error("创建阶段进程句柄关闭失败：%s", process.pid)

    def close(self) -> None:
        if self._handle:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None


# 清理失败保留进程和 Job 引用，不能把仍拥有的对象误报为已清理。
_owned_jobs: dict[asyncio.subprocess.Process, _WindowsJob] = {}
_job_watchers: set[asyncio.Task[None]] = set()


async def _watch_parent_exit(process: asyncio.subprocess.Process, job: _WindowsJob) -> None:
    try:
        # Process.wait 在后代继承 PIPE 时可能等 EOF；returncode 可直接观察父退出。
        while process.returncode is None:
            await asyncio.sleep(.05)
        job.close()
        await asyncio.wait_for(process.wait(), 5)
        _owned_jobs.pop(process, None)
    except TimeoutError:
        logger.error("自建进程退出确认超时：%s", process.pid)
    except OSError as exc:
        logger.error("自建 Job 回收失败：%s", redact(str(exc)))


async def spawn_process(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """统一所有权创建；Windows 子进程从运行前即受 Job 约束。"""
    options = {**spawn_kwargs(), **kwargs}
    job = _WindowsJob() if sys.platform == "win32" else None
    if job is not None:
        options["creationflags"] = options.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    creation = asyncio.create_task(asyncio.create_subprocess_exec(*args, **options))
    process: asyncio.subprocess.Process | None = None
    try:
        try:
            process = await asyncio.shield(creation)
        except asyncio.CancelledError:
            process = await creation
            if job is not None:
                _owned_jobs[process] = job
                job.assign_and_resume(process)
            await reap_process(process)
            raise
        assert process is not None
        if job is not None:
            _owned_jobs[process] = job
            job.assign_and_resume(process)
            watcher = asyncio.create_task(_watch_parent_exit(process, job))
            _job_watchers.add(watcher)
            watcher.add_done_callback(_job_watchers.discard)
        return process
    except OSError:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await reap_process(process)
        elif job is not None:
            job.close()
        raise


async def reap_process(process: asyncio.subprocess.Process) -> None:
    """只回收此句柄对应的进程树；不按名称寻找外部服务。"""
    if sys.platform == "win32":
        job = _owned_jobs.get(process)
        if job is not None:
            job.close()
        elif process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    logger.debug("进程已退出：%s", process.pid)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            logger.debug("进程组已退出：%s", process.pid)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            logger.debug("进程组未及时退出，强制回收：%s", process.pid)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            logger.debug("进程组已回收：%s", process.pid)
    await asyncio.wait_for(process.wait(), 5)
    _owned_jobs.pop(process, None)


async def run_command(cmd: list[str], cwd: Path, *, log_path: Path) -> tuple[int, str]:
    """实时记录脱敏输出；取消与超时均等待自建子进程回收。"""
    tail: deque[str] = deque(maxlen=30)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation = asyncio.create_task(spawn_process(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    ))
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await creation
        await reap_process(process)
        raise
    async def consume() -> None:
        assert process.stdout is not None
        # 分块而非 readline，避免无换行安装输出超过 StreamReader 上限。
        with log_path.open("a", encoding="utf-8") as output:
            pending = ""
            dropping = False
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := await process.stdout.read(4096):
                pending += decoder.decode(chunk)
                while "\n" in pending or len(pending) > 8192:
                    line, separator, remaining = pending.partition("\n")
                    if not separator:
                        # 不拆开超长行，否则凭据前缀与值分离后可能绕过脱敏。
                        pending = ""
                        dropping = True
                        break
                    pending = remaining
                    safe = "[过长安装日志行已略去]" if dropping or len(line) > 8192 else redact(line)
                    dropping = False
                    output.write(safe + "\n")
                    output.flush()
                    tail.append(safe)
            pending += decoder.decode(b"", final=True)
            if pending or dropping:
                safe = "[过长安装日志行已略去]" if dropping else redact(pending)
                output.write(safe + "\n")
                tail.append(safe)
            await process.wait()
    try:
        await asyncio.wait_for(consume(), INSTALL_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        await reap_process(process)
        raise
    except TimeoutError:
        await reap_process(process)
        return 1, "安装命令超过截止时间"
    except OSError:
        await reap_process(process)
        raise
    return process.returncode or 0, "\n".join(tail)


def _source_info(instance: Path) -> dict[str, str] | None:
    try:
        info = json.loads((instance / "src/.bootstrap-source.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("archive_url"), str):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(info.get("archive_sha256", ""))):
        return None
    return info if (instance / "src/pyproject.toml").is_file() else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _extract(bundle_path: Path, target: Path) -> None:
    """完整检查成员后才解压，禁止平台路径、链接及重复目标。"""
    with zipfile.ZipFile(bundle_path) as bundle:
        entries = bundle.infolist()
        roots: set[str] = set()
        seen: set[str] = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
            invalid_windows_name = any(
                part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
                for part in path.parts
            )
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or invalid_windows_name
                    or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})):
                raise ValueError("归档包含不安全成员")
            roots.add(path.parts[0])
            key = entry.filename.rstrip("/").casefold()
            if key in seen:
                raise ValueError("归档包含重复目标")
            seen.add(key)
        if len(roots) != 1:
            raise ValueError("归档必须只有一个顶层目录")
        bundle.extractall(target)
        root = target / next(iter(roots))
        if not (root / "pyproject.toml").is_file():
            raise ValueError("归档缺少 pyproject.toml")


async def download_source(archive_url: str | None, instance: Path) -> StepResult:
    if _source_info(instance):
        return StepResult(True, "复用已校验源码")
    if (instance / "src").exists():
        return StepResult(False, "现有源码缺少有效来源记录，请停用后移除 src 再重试")
    if not archive_url:
        return StepResult(False, "尚无已验收默认归档，请配置固定源码归档地址")
    try:
        instance.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=instance) as directory:
            temporary = Path(directory)
            archive = temporary / "source.zip"
            digest = hashlib.sha256()
            async with (
                asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS),
                httpx.AsyncClient(timeout=30, follow_redirects=True) as client,
                client.stream("GET", archive_url) as response,
            ):
                response.raise_for_status()
                with archive.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        digest.update(chunk)
                        output.write(chunk)
            unpacked = temporary / "unpacked"
            _extract(archive, unpacked)
            source = next(unpacked.iterdir())
            _write_json(source / ".bootstrap-source.json", {"archive_url": archive_url, "archive_sha256": digest.hexdigest()})
            source.replace(instance / "src")
        logger.info("自举源码下载完成：%s", redact(archive_url))
        return StepResult(True)
    except (httpx.HTTPError, TimeoutError, OSError, ValueError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        logger.warning("源码准备失败：%s", redact(str(exc)))
        return StepResult(False, f"源码准备失败：{redact(str(exc))}")


async def make_env(instance: Path, extras: str, *, runner: Runner) -> StepResult:
    source = instance / "src"
    python = venv_python(instance)
    uv = shutil.which("uv")
    requirements = source / ("requirements-rag.lock.txt" if extras == "rag" else "requirements-core.lock.txt")
    if not requirements.is_file():
        return StepResult(False, "源码缺少锁定依赖清单")
    if not python.is_file():
        command = [uv, "venv", "--python", sys.executable, str(instance / "venv")] if uv else [sys.executable, "-m", "venv", str(instance / "venv")]
        code, output = await runner(command, instance)
        if code:
            return StepResult(False, f"创建环境失败：{redact(output)}")
    if not uv:
        code, _ = await runner([str(python), "-m", "pip", "--version"], instance)
        if code:
            code, output = await runner([str(python), "-m", "ensurepip"], instance)
            if code:
                return StepResult(False, f"补装 pip 失败：{redact(output)}")
    prefix = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
    for arguments, stage in [(["--require-hashes", "-r", str(requirements)], "锁定依赖安装"), (["--no-deps", "-e", "."], "项目安装")]:
        code, output = await runner(prefix + arguments, source)
        if code:
            return StepResult(False, f"{stage}失败：{redact(output)}")
    return StepResult(True)


def write_config(instance: Path, llm: dict[str, Any] | None, port: int) -> StepResult:
    path = instance / ".stock_robot/config.yaml"
    temporary: Path | None = None
    try:
        if path.exists():
            existing = yaml.safe_load(path.read_text("utf-8"))
            if not isinstance(existing, dict):
                return StepResult(False, "已有配置不是 YAML 对象，请修复原配置")
            return StepResult(True, "保留已有配置")
        valid = isinstance(llm, dict) and llm.get("provider") in {"openai", "claude"} and bool(llm.get("api_key")) and bool(llm.get("model"))
        effective = dict(llm, enabled=True) if valid and llm is not None else {"enabled": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            yaml.safe_dump({"llm": effective, "api": {"host": "127.0.0.1", "port": port}}, stream, allow_unicode=True)
        temporary.replace(path)
        return StepResult(True, "" if valid else "模型未复制，请在自举服务 Web UI 配置模型")
    except (OSError, yaml.YAMLError):
        logger.warning("配置创建或解析失败，请检查原配置文件")
        return StepResult(False, "配置创建或解析失败，请检查原配置文件")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.debug("配置临时文件未清理：%s", temporary)


def instance_ready(instance: Path, extras: str) -> bool:
    try:
        marker = json.loads((instance / "install-state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    source = _source_info(instance)
    return (extras in {"", "rag"} and isinstance(marker, dict) and source is not None
            and marker.get("version") == 1 and marker.get("extras") == extras
            and marker.get("source") == source and marker.get("dependency_strategy") == "locked-requirements-v1"
            and venv_python(instance).is_file() and venv_launcher(instance).is_file())


async def ensure_instance(instance: Path, archive_url: str | None, extras: str, *, runner: Runner) -> StepResult:
    if extras not in {"", "rag"}:
        return StepResult(False, "bootstrap_extras 仅接受空或 rag")
    if instance_ready(instance, extras):
        return StepResult(True)
    try:
        async with asyncio.timeout(INSTALL_TIMEOUT_SECONDS):
            result = await download_source(archive_url, instance)
            if not result.ok:
                return result
            result = await make_env(instance, extras, runner=runner)
            if not result.ok:
                return result
            code, output = await runner([str(venv_launcher(instance)), "--help"], instance)
            if code:
                return StepResult(False, f"CLI 加载失败：{redact(output)}")
            _write_json(instance / "install-state.json", {"version": 1, "extras": extras, "source": _source_info(instance), "dependency_strategy": "locked-requirements-v1"})
            return StepResult(instance_ready(instance, extras), "" if instance_ready(instance, extras) else "安装产物不完整")
    except (OSError, TimeoutError) as exc:
        logger.warning("实例准备失败：%s", redact(str(exc)))
        return StepResult(False, f"实例准备失败：{redact(str(exc))}")
