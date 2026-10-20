"""独立服务的下载、锁定安装及中断恢复，使用 AstrBot 框架日志。"""

from __future__ import annotations

import asyncio
import codecs
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
import yaml
from astrbot.api import logger

from .disk_support import RotatingLog, safe_path

DEFAULT_ARCHIVE_URL: str | None = None
RELEASE_API_URL = (
    "https://api.github.com/repos/Arrhenius401/stock_robot/releases/latest"
)
LATEST_RELEASE_URL = "https://github.com/Arrhenius401/stock_robot/releases/latest"
SOURCE_ARCHIVE_URL = (
    "https://codeload.github.com/Arrhenius401/stock_robot/zip/refs/tags/"
)
INSTALL_TIMEOUT_SECONDS = 1800
DEFAULT_PACKAGE_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
DOWNLOAD_TIMEOUT_SECONDS = 300
Runner = Callable[[list[str], Path], Awaitable[tuple[int, str]]]


class InstallProgress:
    """真实阶段与脱敏输出；阶段序号不代表完成百分比。"""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.started = time.monotonic()
        self.stage = 0
        self.label = "准备安装"
        self.latest = ""
        self.latest_at: float | None = None

    def describe(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        prefix = f"[{self.stage}/5] " if self.stage else ""
        return f"{prefix}{self.label}，已用时 {elapsed // 60} 分 {elapsed % 60} 秒"

    def emit(self, message: str) -> None:
        safe = redact(message)
        logger.info("%s", safe)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with RotatingLog(self.log_path) as output:
                output.write(safe + "\n")
        except OSError as exc:
            logger.warning("安装进度日志写入失败：%s", type(exc).__name__)

    def enter(self, stage: int, label: str) -> None:
        if stage == 1:
            self.started = time.monotonic()
        self.stage, self.label, self.latest = stage, label, ""
        self.latest_at = None
        self.emit(self.describe())

    def record(self, line: str) -> None:
        if line.strip():
            self.latest = redact(line.strip())[:512]
            self.latest_at = time.monotonic()

    def heartbeat(self) -> None:
        detail = "；等待安装程序输出"
        if self.latest_at is not None:
            age = int(time.monotonic() - self.latest_at)
            detail = f"；最近输出（{age} 秒前）：{self.latest}"
        self.emit(self.describe() + detail)


@dataclass(frozen=True)
class StepResult:
    ok: bool
    detail: str = ""


def redact(text: str) -> str:
    """去除 URL 认证、查询串和常见凭据，安装输出也不直接暴露。"""
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[认证已隐藏]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[参数已隐藏]", text)
    text = re.sub(
        r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)\S+",
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password|authorization)[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
        r"\1\2[已隐藏]\2",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password|authorization)[\"']?\s*[:=]\s*)[^\s,}]+",
        r"\1[已隐藏]",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)


def runtime_path(instance: Path) -> Path:
    """解释活动版本指针；旧版根目录布局继续可用。"""
    pointer = instance / "active-runtime.json"
    if not pointer.exists():
        return instance
    if not safe_path(pointer):
        raise ValueError("活动版本指针路径不安全")
    state = json.loads(pointer.read_text("utf-8"))
    if not isinstance(state, dict):
        raise TypeError("活动版本指针格式无效")
    relative = state.get("active")
    if relative == ".":
        return instance
    if not isinstance(relative, str) or not re.fullmatch(r"\.releases/[A-Za-z0-9_-]+", relative):
        raise ValueError("活动版本目录无效")
    runtime = instance / relative
    if not safe_path(runtime):
        raise ValueError("活动版本目录包含链接")
    return runtime


def rollback_runtime(instance: Path) -> bool:
    """原子恢复活动版本；环境始终留在安装时的固定路径。"""
    pointer = instance / "active-runtime.json"
    if not pointer.is_file():
        return False
    current = runtime_path(instance)
    state = json.loads(pointer.read_text("utf-8"))
    previous = state.get("previous")
    if previous != "." and (
        not isinstance(previous, str)
        or not re.fullmatch(r"\.releases/[A-Za-z0-9_-]+", previous)
    ):
        raise ValueError("旧版指针无效")
    if not safe_path(instance / previous):
        raise ValueError("旧版目录路径不安全")
    _write_json(pointer, {"active": previous,
                          "previous": current.relative_to(instance).as_posix(),
                          "rolled_back": True})
    return True


def runtime_pending(instance: Path) -> bool:
    pointer = instance / "active-runtime.json"
    if not pointer.exists():
        return False
    runtime_path(instance)
    return json.loads(pointer.read_text("utf-8")).get("pending_health") is True


def confirm_runtime(instance: Path) -> None:
    pointer = instance / "active-runtime.json"
    if pointer.exists():
        runtime_path(instance)
        state = json.loads(pointer.read_text("utf-8"))
        state["pending_health"] = False
        _write_json(pointer, state)


def prune_runtimes(instance: Path) -> None:
    """只清理自举器拥有、且不属于活动与回滚版本的目录。"""
    releases = instance / ".releases"
    if not releases.is_dir() or not safe_path(releases):
        return
    protected = {runtime_path(instance)}
    pointer = instance / "active-runtime.json"
    if pointer.exists():
        state = json.loads(pointer.read_text("utf-8"))
        previous = state.get("previous")
        if previous == "." or (isinstance(previous, str)
                               and re.fullmatch(r"\.releases/[A-Za-z0-9_-]+", previous)):
            protected.add(instance / previous)
    for candidate in releases.iterdir():
        if (candidate in protected or not candidate.name.startswith("release-")
                or not candidate.is_dir() or not safe_path(candidate)):
            continue
        try:
            owner = json.loads((candidate / ".bootstrap-runtime.json").read_text("utf-8"))
            if owner != {"owner": "stock-robot-bootstrap-v1"}:
                continue
            # safe_path 已验证所有父路径，候选必为 releases 的直接子目录。
            shutil.rmtree(candidate)
        except (OSError, ValueError) as error:
            logger.warning("旧版本目录未清理：%s（%s）", candidate, type(error).__name__)


def venv_python(instance: Path) -> Path:
    return (
        runtime_path(instance)
        / "venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )


def venv_launcher(instance: Path) -> Path:
    return (
        runtime_path(instance)
        / "venv"
        / ("Scripts/stock-robot.exe" if sys.platform == "win32" else "bin/stock-robot")
    )


def spawn_kwargs() -> dict[str, Any]:
    return (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimit),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """挂起创建、挂接 Job 后恢复，避免子进程先逃离所有权边界。"""

    def __init__(self) -> None:
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        kernel = self._kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
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
        information.BasicLimitInformation.LimitFlags = (
            0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
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


async def _watch_parent_exit(
    process: asyncio.subprocess.Process, job: _WindowsJob
) -> None:
    try:
        # Process.wait 在后代继承 PIPE 时可能等 EOF；returncode 可直接观察父退出。
        while process.returncode is None:
            await asyncio.sleep(0.05)
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
        options["creationflags"] = (
            options.get("creationflags", 0) | 0x4
        )  # CREATE_SUSPENDED
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
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
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


def _command_environment(cmd: list[str]) -> dict[str, str] | None:
    """仅显式指定包源的安装子进程隔离索引设置，不修改主进程环境。"""
    if "install" not in cmd or not {"--index-url", "--default-index"}.intersection(cmd):
        return None
    environment = dict(os.environ)
    for name in (
        "UV_INDEX", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_NO_INDEX", "UV_CONFIG_FILE",
        "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_NO_INDEX",
    ):
        environment.pop(name, None)
    # pip 的空设备配置禁用配置文件读取；uv 同时在命令中使用 --no-config。
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


async def run_command(
    cmd: list[str],
    cwd: Path,
    *,
    log_path: Path,
    timeout: float | None = None,
    progress: InstallProgress | None = None,
) -> tuple[int, str]:
    """实时记录脱敏输出；取消与超时均等待自建子进程回收。"""
    timeout = INSTALL_TIMEOUT_SECONDS if timeout is None else timeout
    tail: deque[str] = deque(maxlen=30)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation = asyncio.create_task(
        spawn_process(
            *cmd,
            cwd=cwd,
            env=_command_environment(cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await creation
        await reap_process(process)
        raise

    async def consume() -> None:
        assert process.stdout is not None
        # 分块而非 readline，避免无换行安装输出超过 StreamReader 上限。
        with RotatingLog(log_path) as output:
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
                    safe = (
                        "[过长安装日志行已略去]"
                        if dropping or len(line) > 8192
                        else redact(line)
                    )
                    dropping = False
                    try:
                        output.write(safe + "\n")
                    except OSError as error:
                        logger.warning(
                            "安装日志写入失败，继续排空输出: %s", type(error).__name__
                        )
                    tail.append(safe)
                    if progress is not None:
                        progress.record(safe)
            pending += decoder.decode(b"", final=True)
            if pending or dropping:
                safe = "[过长安装日志行已略去]" if dropping else redact(pending)
                try:
                    output.write(safe + "\n")
                except OSError as error:
                    logger.warning(
                        "安装日志写入失败，继续排空输出: %s", type(error).__name__
                    )
                tail.append(safe)
                if progress is not None:
                    progress.record(safe)
            await process.wait()

    try:
        await asyncio.wait_for(consume(), timeout)
    except asyncio.CancelledError:
        await reap_process(process)
        raise
    except TimeoutError:
        await reap_process(process)
        return 1, f"安装命令超时（上限 {timeout:g} 秒）；最近输出：" + "\n".join(tail)
    except OSError:
        await reap_process(process)
        raise
    return process.returncode or 0, "\n".join(tail)


def _source_info(instance: Path) -> dict[str, Any] | None:
    try:
        instance = runtime_path(instance)
        info = json.loads((instance / "src/.bootstrap-source.json").read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("archive_url"), str):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", str(info.get("archive_sha256", ""))):
        return None
    return info if (instance / "src/pyproject.toml").is_file() else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
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
            reserved = {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            invalid_windows_name = any(
                part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
                for part in path.parts
            )
            if (
                not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or ":" in entry.filename
                or invalid_windows_name
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
            ):
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


def _tag_archive(tag: str) -> str:
    """只拼接有效固定标签，避免解码后的路径或控制字符改变下载目标。"""
    parts = tag.split("/")
    if (
        not tag
        or tag == "@"
        or ".." in tag
        or "@{" in tag
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", tag)
        or any(
            not part or part.startswith(".") or part.endswith((".", ".lock"))
            for part in parts
        )
    ):
        raise ValueError("正式发布版本标签无效")
    return SOURCE_ARCHIVE_URL + quote(tag, safe="")


def release_archive_url(tag: str) -> str:
    """返回指定正式版本的源码归档地址。"""
    return _tag_archive(tag)


def source_release_tag(instance: Path) -> str | None:
    """读取已安装源码对应的正式发布标签。"""
    source = _source_info(instance)
    tag = source.get("release_tag") if source else None
    return tag if isinstance(tag, str) and tag else None


def source_matches_release(instance: Path, release_tag: str) -> bool:
    """仅将明确记录的正式版视为目标版本。"""
    return source_release_tag(instance) == release_tag


def _rate_limited(response: httpx.Response) -> bool:
    """普通权限拒绝不能触发备用渠道，403 需要明确的限流证据。"""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get(
        "retry-after"
    ):
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    return (
        isinstance(body, dict)
        and "rate limit" in str(body.get("message", "")).casefold()
    )


async def _latest_release_web(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """官方 latest 的重定向即标签来源，不追踪任意跳转或解析页面内容。"""
    response = await client.get(LATEST_RELEASE_URL, follow_redirects=False)
    if response.status_code not in {301, 302, 303, 307, 308}:
        response.raise_for_status()
        raise ValueError("官方 latest 未返回正式版标签重定向")
    location = urlsplit(response.headers.get("location", ""))
    prefix = "/Arrhenius401/stock_robot/releases/tag/"
    if (
        location.scheme != "https"
        or location.netloc != "github.com"
        or not location.path.startswith(prefix)
        or location.query
        or location.fragment
    ):
        raise ValueError("官方 latest 返回了非预期的仓库标签地址")
    tag = unquote(location.path[len(prefix) :], errors="strict")
    return _tag_archive(tag), {"release_tag": tag, "release_resolution": "web"}


async def _latest_release(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """只解析正式Release，后续下载锁定到本次返回的标签。"""
    response = await client.get(
        RELEASE_API_URL, headers={"Accept": "application/vnd.github+json"}
    )
    if response.status_code == 404:
        raise ValueError(
            "stock_robot尚无正式发布版本，请等待发布或填写固定源码归档地址"
        )
    if _rate_limited(response):
        logger.warning("GitHub Release API 限流，改用官方 latest 标签重定向")
        try:
            return await _latest_release_web(client)
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(
                f"GitHub API限流，备用正式版查询失败：{redact(str(exc))}"
            ) from exc
    response.raise_for_status()
    release = response.json()
    if not isinstance(release, dict):
        raise TypeError("正式发布查询响应格式无效")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("返回版本不是正式发布版本")
    tag = release.get("tag_name")
    release_id = release.get("id")
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("正式发布版本标签缺失")
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("正式发布版本记录无效")
    return _tag_archive(tag), {
        "release_tag": tag,
        "release_id": release_id,
        "release_resolution": "api",
    }


async def download_source(
    archive_url: str | None,
    instance: Path,
    *,
    extras: str = "",
    release_tag: str | None = None,
) -> StepResult:
    if _source_info(instance):
        return StepResult(True, "复用已校验源码")
    if (instance / "src").exists():
        return StepResult(False, "现有源码缺少有效来源记录，请停用后移除 src 再重试")
    try:
        instance.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".bootstrap-", dir=instance
        ) as directory:
            temporary = Path(directory)
            archive = temporary / "source.zip"
            digest = hashlib.sha256()
            release_info: dict[str, Any] = (
                {"release_tag": release_tag, "release_resolution": "pinned"}
                if release_tag is not None
                else {}
            )
            async with (
                asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS),
                httpx.AsyncClient(timeout=30, follow_redirects=True) as client,
            ):
                if not archive_url:
                    archive_url, release_info = await _latest_release(client)
                    logger.info(
                        "准备安装stock_robot正式版：%s", release_info["release_tag"]
                    )
                async with client.stream("GET", archive_url) as response:
                    response.raise_for_status()
                    with archive.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            digest.update(chunk)
                            output.write(chunk)
            unpacked = temporary / "unpacked"
            _extract(archive, unpacked)
            source = next(unpacked.iterdir())
            required = ["requirements-core.lock.txt"]
            if extras == "rag":
                required.append("requirements-rag.lock.txt")
            if any(not (source / name).is_file() for name in required):
                raise ValueError("源码归档缺少所需锁定依赖清单")
            _write_json(
                source / ".bootstrap-source.json",
                {
                    "archive_url": archive_url,
                    "archive_sha256": digest.hexdigest(),
                    **release_info,
                },
            )
            for attempt in range(5):
                try:
                    source.replace(instance / "src")
                    break
                except PermissionError as error:
                    if getattr(error, "winerror", None) not in {5, 32} or attempt == 4:
                        raise
                    logger.debug("源码目录暂时被占用，第 %s 次重试", attempt + 1)
                    await asyncio.sleep(0.05)
        logger.info("自举源码下载完成：%s", redact(archive_url))
        return StepResult(True)
    except (
        httpx.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        reason = (
            f"源码下载超时（总上限 {DOWNLOAD_TIMEOUT_SECONDS} 秒，单次网络等待上限 30 秒）"
            if isinstance(exc, (TimeoutError, httpx.TimeoutException))
            else f"{type(exc).__name__}：{redact(str(exc))}"
        )
        logger.warning("源码准备失败：%s", reason)
        return StepResult(False, f"源码准备失败：{reason}")


def _source_failure(output: str) -> bool:
    """仅识别可换源的安装错误，未知错误保守失败。"""
    text = output.lower()
    if any(marker in text for marker in (
        "hash mismatch", "hashes", "hash does not match", "permission denied", "access is denied",
        "no space left", "disk full", "requires-python", "requires a different python",
        "not enough space", "disk quota exceeded", "certificate verify failed",
        "certificate_verify_failed", "certificate has expired", "sslcertverificationerror",
        "require a different python", "requires python", "requires python version",
        "not a supported wheel", "unsupported platform", "not supported on this platform",
        "failed building wheel", "failed to build", "安装命令超时",
    )):
        return False
    return any(marker in text for marker in (
        "connection refused", "connection reset", "connection aborted", "connecttimeout",
        "readtimeout", "timed out", "temporary failure in name resolution", "name or service not known",
        "failed to fetch", "failed to download", "network is unreachable", "could not fetch url",
        "not found in the package registry", "503 service unavailable", "502 bad gateway",
        "504 gateway", "403 forbidden", "404 not found", "429 too many requests",
    ))


async def make_env(
    instance: Path,
    extras: str,
    *,
    runner: Runner,
    package_index_url: str = "",
    progress: InstallProgress | None = None,
) -> StepResult:
    source = instance / "src"
    python = venv_python(instance)
    uv = shutil.which("uv")
    requirements = source / (
        "requirements-rag.lock.txt" if extras == "rag" else "requirements-core.lock.txt"
    )
    if not requirements.is_file():
        return StepResult(False, "源码缺少锁定依赖清单")
    if progress is not None:
        progress.enter(2, "创建环境")
    if not python.is_file():
        command = (
            [uv, "venv", "--python", sys.executable, str(instance / "venv")]
            if uv
            else [sys.executable, "-m", "venv", str(instance / "venv")]
        )
        code, output = await runner(command, instance)
        if code:
            return StepResult(False, f"创建环境失败：{redact(output)}")
    if not uv:
        code, _ = await runner([str(python), "-m", "pip", "--version"], instance)
        if code:
            code, output = await runner([str(python), "-m", "ensurepip"], instance)
            if code:
                return StepResult(False, f"补装 pip 失败：{redact(output)}")
    if progress is not None:
        progress.enter(3, "安装依赖")
    prefix = (
        [uv, "--no-config", "pip", "install", "--python", str(python)]
        if uv
        else [str(python), "-m", "pip", "install"]
    )
    requested = package_index_url.strip()
    selected = requested or DEFAULT_PACKAGE_INDEX_URL
    fallback = DEFAULT_PACKAGE_INDEX_URL if requested else ""
    # 同一个显式清华源不能重复尝试；空字符串表示沿用工具自己的设置。
    can_fallback = selected.rstrip("/") != fallback.rstrip("/")
    index_flag = "--default-index" if uv else "--index-url"
    for arguments, stage in [
        (["--require-hashes", "-r", str(requirements)], "锁定依赖安装"),
        (["--no-deps", "-e", "."], "项目安装"),
    ]:
        index = [index_flag, selected] if selected else []
        command = prefix if selected else [item for item in prefix if item != "--no-config"]
        code, output = await runner(command + index + arguments, source)
        if code and can_fallback and _source_failure(output):
            previous = redact(selected) if selected else "安装工具原有设置"
            selected = fallback
            can_fallback = False
            next_source = redact(selected) if selected else "安装工具原有设置"
            message = f"{stage}遇到包源问题，从 {previous} 切换至 {next_source}，仅重试一次"
            if progress is not None:
                progress.emit(message)
            else:
                logger.warning("%s", message)
            index = [index_flag, selected] if selected else []
            command = prefix if selected else [item for item in prefix if item != "--no-config"]
            code, output = await runner(command + index + arguments, source)
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
        valid = (
            isinstance(llm, dict)
            and llm.get("provider") in {"openai", "claude"}
            and bool(llm.get("api_key"))
            and bool(llm.get("model"))
        )
        effective = (
            dict(llm, enabled=True) if valid and llm is not None else {"enabled": False}
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            yaml.safe_dump(
                {"llm": effective, "api": {"host": "127.0.0.1", "port": port}},
                stream,
                allow_unicode=True,
            )
        temporary.replace(path)
        return StepResult(
            True, "" if valid else "模型未复制，请在自举服务 Web UI 配置模型"
        )
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
        marker = json.loads((runtime_path(instance) / "install-state.json").read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    source = _source_info(instance)
    return (
        extras in {"", "rag"}
        and isinstance(marker, dict)
        and source is not None
        and marker.get("version") == 1
        and marker.get("extras") == extras
        and marker.get("source") == source
        and marker.get("dependency_strategy") == "locked-requirements-v1"
        and venv_python(instance).is_file()
        and venv_launcher(instance).is_file()
    )


async def upgrade_instance(
    instance: Path,
    release_tag: str,
    extras: str,
    *,
    runner: Runner,
    install_timeout: float,
    package_index_url: str,
    progress: InstallProgress | None,
) -> StepResult:
    """在固定版本目录构建候选，原子切换指针；禁止搬迁虚拟环境。"""
    if _source_info(instance) is None:
        return StepResult(False, "当前实例来源记录不完整，不能自动升级")
    if source_matches_release(instance, release_tag) and instance_ready(instance, extras):
        return StepResult(True, "已是插件验证版本")
    previous = runtime_path(instance)
    releases = instance / ".releases"
    if not safe_path(releases):
        return StepResult(False, "版本目录路径不安全")
    releases.mkdir(exist_ok=True)
    prune_runtimes(instance)
    candidate = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
    _write_json(candidate / ".bootstrap-runtime.json", {"owner": "stock-robot-bootstrap-v1"})
    try:
        if progress is not None:
            progress.enter(1, f"下载已验证版本 {release_tag}")
        result = await ensure_instance(
            candidate,
            release_archive_url(release_tag),
            extras,
            runner=runner,
            install_timeout=install_timeout,
            package_index_url=package_index_url,
            progress=progress,
            expected_release_tag=release_tag,
        )
        if not result.ok:
            return StepResult(False, f"自动升级准备失败：{result.detail}")
        if not source_matches_release(candidate, release_tag):
            return StepResult(False, "自动升级候选版本与插件声明不一致")
        _write_json(instance / "active-runtime.json", {
            "active": candidate.relative_to(instance).as_posix(),
            "previous": previous.relative_to(instance).as_posix(),
            "pending_health": True,
        })
        logger.info("候选版本已启用，等待服务健康检查：%s", release_tag)
        return StepResult(True, f"候选版本 {release_tag} 已就绪")
    except (OSError, ValueError) as exc:
        logger.warning("候选版本准备失败，保留旧版：%s", redact(str(exc)))
        return StepResult(False, f"候选版本准备失败，保留旧版：{redact(str(exc))}")


async def ensure_instance(
    instance: Path,
    archive_url: str | None,
    extras: str,
    *,
    runner: Runner,
    install_timeout: float = INSTALL_TIMEOUT_SECONDS,
    package_index_url: str = "",
    progress: InstallProgress | None = None,
    expected_release_tag: str | None = None,
) -> StepResult:
    if not math.isfinite(install_timeout) or install_timeout <= 0:
        return StepResult(False, "安装总时限必须为大于 0 的有限秒数")
    if extras not in {"", "rag"}:
        return StepResult(False, "bootstrap_extras 仅接受空或 rag")
    if instance_ready(instance, extras):
        if expected_release_tag is None or source_matches_release(instance, expected_release_tag):
            return StepResult(True)
        return await upgrade_instance(
            instance,
            expected_release_tag,
            extras,
            runner=runner,
            install_timeout=install_timeout,
            package_index_url=package_index_url,
            progress=progress,
        )
    active = runtime_path(instance)
    if active != instance:
        # extras 改变或产物缺失时也在新的固定目录构建，旧环境不就地改装。
        repair_tag = expected_release_tag or source_release_tag(instance)
        if repair_tag is None:
            return StepResult(False, "活动版本缺少正式发布标签，不能自动修复")
        return await upgrade_instance(
            instance, repair_tag, extras, runner=runner,
            install_timeout=install_timeout, package_index_url=package_index_url,
            progress=progress,
        )
    try:
        async with asyncio.timeout(install_timeout):
            if progress is not None:
                progress.enter(1, "下载源码（已有校验源码将复用）")
            source_archive = (
                archive_url
                if archive_url is not None
                else (
                    release_archive_url(expected_release_tag)
                    if expected_release_tag is not None
                    else None
                )
            )
            result = await download_source(
                source_archive,
                instance,
                extras=extras,
                release_tag=expected_release_tag,
            )
            if not result.ok:
                return result
            if expected_release_tag is not None and not source_matches_release(instance, expected_release_tag):
                return StepResult(False, "现有源码与插件绑定版本不一致，请检查实例来源")
            result = await make_env(
                instance,
                extras,
                runner=runner,
                package_index_url=package_index_url,
                progress=progress,
            )
            if not result.ok:
                return result
            if progress is not None:
                progress.enter(4, "检查程序")
            code, output = await runner(
                [str(venv_launcher(instance)), "--help"], instance
            )
            if code:
                return StepResult(False, f"CLI 加载失败：{redact(output)}")
            _write_json(
                instance / "install-state.json",
                {
                    "version": 1,
                    "extras": extras,
                    "source": _source_info(instance),
                    "dependency_strategy": "locked-requirements-v1",
                },
            )
            return StepResult(
                instance_ready(instance, extras),
                "" if instance_ready(instance, extras) else "安装产物不完整",
            )
    except TimeoutError:
        detail = f"后台安装超时（总时限 {install_timeout:g} 秒）"
        if progress is not None:
            detail += f"：{progress.describe()}"
            if progress.latest:
                detail += f"；最近输出：{progress.latest}"
        detail += "；请检查包源和网络，可增加 install_timeout_seconds 后重载，已有源码和缓存会复用；详见 service.log"
        logger.warning("%s", detail)
        return StepResult(False, detail)
    except OSError as exc:
        logger.warning("实例准备失败：%s", redact(str(exc)))
        return StepResult(False, f"实例准备失败：{redact(str(exc))}")
