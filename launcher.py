"""独立分析服务的单任务准备和自有进程生命周期。"""

from __future__ import annotations

import asyncio
import codecs
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO
from urllib.parse import urlsplit

import httpx
import yaml
from astrbot.api import logger

from .bootstrap import (
    INSTALL_TIMEOUT_SECONDS,
    InstallProgress,
    Runner,
    StepResult,
    ensure_instance,
    instance_ready,
    reap_process,
    redact,
    spawn_process,
    venv_launcher,
    write_config,
)

State = Literal["idle", "starting", "ready", "failed", "stopped"]


@dataclass(frozen=True)
class ReadyOutcome:
    ok: bool
    reason: str = ""
    pending: bool = False


async def probe_health(base_url: str) -> bool:
    """仅接受分析服务的结构化健康响应。"""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{base_url.rstrip('/')}/health")
        payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


class ServiceLauncher:
    def __init__(
        self,
        base_url: str,
        archive_url: str | None,
        extras: str,
        auto_install: bool,
        timeout: float,
        data_dir: Path,
        llm: dict[str, Any] | None = None,
        runner: Runner | None = None,
        llm_loader: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        install_timeout: float = INSTALL_TIMEOUT_SECONDS,
        package_index_url: str = "",
        progress: InstallProgress | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是有效 HTTP/HTTPS 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 不能包含认证信息、查询串或片段")
        self._host = parsed.hostname
        self._port = (
            parsed.port
            if parsed.port is not None
            else (443 if parsed.scheme == "https" else 80)
        )
        if not 1 <= self._port <= 65535:
            raise ValueError("服务端口必须位于 1 到 65535")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("启动等待上限必须大于 0")
        if not math.isfinite(install_timeout) or install_timeout <= 0:
            raise ValueError("install_timeout_seconds 必须为大于 0 的有限秒数")
        if package_index_url:
            index = urlsplit(package_index_url)
            if index.scheme not in {"http", "https"} or not index.hostname:
                raise ValueError("package_index_url 必须是有效 HTTP/HTTPS 包源地址")
        self._can_start = (
            parsed.scheme == "http"
            and self._host in {"localhost", "127.0.0.1", "::1"}
            and parsed.path in {"", "/"}
        )
        self.base_url = base_url.rstrip("/")
        self.archive_url = archive_url
        self.extras = extras
        self.auto_install = auto_install
        self.timeout = timeout
        self.install_timeout = install_timeout
        self.package_index_url = package_index_url
        self.progress = progress or InstallProgress(data_dir / "service.log")
        self.data_dir = data_dir
        self.instance = data_dir / "instance"
        self._llm = llm
        self._llm_loader = llm_loader
        self._runner = runner
        self._task: asyncio.Task[ReadyOutcome] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._log: TextIO | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._state: State = "idle"
        self._reason = ""
        self._stop_lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    def _get_or_create_task(
        self, allow_install: bool
    ) -> asyncio.Task[ReadyOutcome] | None:
        """同一事件循环内，无 await 的区段保证只创建一个准备任务。"""
        if self._stopped:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._prepare(allow_install))
            self._task.add_done_callback(self._observe_task)
        return self._task

    def start_background(self) -> None:
        self._get_or_create_task(allow_install=True)

    @staticmethod
    def _observe_task(task: asyncio.Task[ReadyOutcome]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("服务准备任务异常：%s", type(error).__name__)

    async def ensure_ready(self, *, wait_timeout: float) -> ReadyOutcome:
        if self._stopped:
            return ReadyOutcome(False, "插件已停用")
        if wait_timeout <= 0:
            return ReadyOutcome(False, "本次工具调用没有剩余等待时间")
        task = self._get_or_create_task(allow_install=False)
        if task is None:
            return ReadyOutcome(False, "插件已停用")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
        except TimeoutError:
            return ReadyOutcome(
                False,
                f"{self.progress.describe()}；本次等待结束，后台继续准备，请稍后再试；详见 service.log",
                pending=True,
            )

    def _failure(self, reason: str) -> ReadyOutcome:
        if self.progress.stage and not reason.startswith("后台安装超时"):
            reason = f"{self.progress.describe()}；{reason}"
            if self.progress.stage == 1:
                reason += (
                    "；请检查 GitHub 源码地址与网络后重载；详见插件日志和 service.log"
                )
            elif self.progress.stage == 3:
                reason += (
                    "；请检查 package_index_url 和安装输出后重载；详见 service.log"
                )
            else:
                reason += "；详见插件日志和 service.log"
        self._reason = redact(reason)
        if not self._stopped:
            self._state = "failed"
        logger.warning("服务未就绪：%s", self._reason)
        return ReadyOutcome(False, self._reason)

    async def _load_llm(self) -> dict[str, Any] | None:
        if self._llm_loader is None:
            return self._llm
        try:
            async with asyncio.timeout(10):
                while True:
                    value = await self._llm_loader()
                    if value is not None:
                        return value
                    await asyncio.sleep(0.25)
        except TimeoutError:
            logger.warning("默认模型未在限时内就绪，将创建降级配置")
        except Exception as exc:  # noqa: BLE001 — 隔离第三方供应商配置接口
            logger.warning("读取默认模型失败：%s", type(exc).__name__)
        return None

    async def _configure(self, allow_install: bool) -> StepResult:
        path = self.instance / ".stock_robot" / "config.yaml"
        if not path.is_file():
            if not allow_install:
                return StepResult(False, "配置文件缺失，请重载插件准备首次配置")
            result = write_config(self.instance, await self._load_llm(), self._port)
            if not result.ok:
                return result
            if result.detail:
                logger.warning("%s；配置地址：%s", result.detail, self.base_url)
        try:
            config = yaml.safe_load(path.read_text("utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return StepResult(
                False, f"配置文件无法解析，请修复原文件（{type(exc).__name__}）"
            )
        if not isinstance(config, dict):
            return StepResult(False, "配置文件必须为 YAML 对象，请修复原文件")
        for key in ("llm", "api"):
            if key in config and not isinstance(config[key], dict):
                return StepResult(False, f"配置文件 {key} 必须为对象，请修复原文件")
        return StepResult(True)

    async def _prepare(self, allow_install: bool) -> ReadyOutcome:
        heartbeat: asyncio.Task[None] | None = None
        try:
            if self._stopped:
                return ReadyOutcome(False, "插件已停用")
            if await probe_health(self.base_url):
                self._state = "ready"
                self._reason = ""
                return ReadyOutcome(True)
            if not self._can_start or not self.auto_install:
                return self._failure(
                    "分析服务未启动，此地址或配置仅允许复用正在运行的服务"
                )
            if self._process is not None:
                if self._process.returncode is None:
                    return self._failure("自建进程仍运行，健康探测失败，请查看插件日志")
                await self._cleanup_process()
            self._state = "starting"
            heartbeat = asyncio.create_task(self._report_progress())
            if not instance_ready(self.instance, self.extras):
                if not allow_install:
                    return self._failure(
                        self._reason or "安装尚未完成，请重载插件后查看安装日志"
                    )
                if self._runner is None:
                    return self._failure("未配置安装命令执行器")
                result = await ensure_instance(
                    self.instance,
                    self.archive_url,
                    self.extras,
                    runner=self._runner,
                    install_timeout=self.install_timeout,
                    package_index_url=self.package_index_url,
                    progress=self.progress,
                )
                if not result.ok:
                    return self._failure(result.detail)
            self.progress.enter(4, "检查程序与配置")
            result = await self._configure(allow_install)
            if not result.ok:
                return self._failure(result.detail)
            if self._stopped:
                return ReadyOutcome(False, "插件已停用")
            self.progress.enter(5, "启动服务")
            outcome = await self._start()
            if outcome.ok:
                self.progress.emit("分析服务已就绪；" + self.progress.describe())
            return outcome
        except Exception as exc:  # noqa: BLE001 — 服务准备隔离边界，后台失败转为可见诊断
            return self._failure(
                f"服务准备失败：{type(exc).__name__}：{redact(str(exc))}"
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _report_progress(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.progress.heartbeat()

    async def _cleanup_process(self) -> None:
        """只使用保存的句柄；退出未确认时保留所有权供再次清理。"""
        proc = self._process
        if proc is not None:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    logger.debug("自建服务已退出")
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 5)
            else:
                await proc.wait()
            # 父进程先退出时，仍需回收封装持有的子进程树所有权。
            await reap_process(proc)
            self._process = None
        if self._log_task is not None:
            try:
                await asyncio.wait_for(self._log_task, 5)
            except TimeoutError:
                logger.warning("服务日志读取未及时结束，已取消日志任务")
            except OSError as exc:
                logger.warning("服务日志写入失败：%s", type(exc).__name__)
            finally:
                self._log_task = None
        if self._log is not None:
            self._log.close()
            self._log = None

    async def _pump_log(self, reader: asyncio.StreamReader, output: TextIO) -> None:
        """分块读取完整行再脱敏；过长行整行略去，避免凭据跨块漏出。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        dropping = False
        write_failed = False

        def write_line(line: str) -> None:
            nonlocal write_failed
            if write_failed:
                return
            try:
                self.progress.record(line)
                output.write(line + "\n")
                output.flush()
            except OSError as exc:
                # 写盘失败仍持续排空管道，避免子进程因 stdout 堵塞无法退出。
                write_failed = True
                logger.error("服务日志写入失败：%s", type(exc).__name__)

        def consume(text: str) -> None:
            nonlocal pending, dropping
            segments = text.split("\n")
            for index, segment in enumerate(segments):
                if not dropping:
                    pending += segment
                    if len(pending) > 8192:
                        pending = ""
                        dropping = True
                if index < len(segments) - 1:
                    write_line(
                        "[过长服务日志行已略去]" if dropping else redact(pending)
                    )
                    pending = ""
                    dropping = False

        while chunk := await reader.read(4096):
            consume(decoder.decode(chunk))
        consume(decoder.decode(b"", final=True))
        if dropping or pending:
            write_line("[过长服务日志行已略去]" if dropping else redact(pending))

    def _register_process(self, proc: asyncio.subprocess.Process) -> None:
        """取得创建结果后立即登记消费者，取消创建也走相同清理路径。"""
        self._process = proc
        if proc.stdout is None or self._log is None:
            raise RuntimeError("自建服务缺少日志管道")
        self._log_task = asyncio.create_task(self._pump_log(proc.stdout, self._log))
        self._log_task.add_done_callback(self._observe_log_task)

    @staticmethod
    def _observe_log_task(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("服务日志任务异常：%s", type(error).__name__)

    async def _start(self) -> ReadyOutcome:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.data_dir / "service.log").open("a", encoding="utf-8")
        creation = asyncio.create_task(
            spawn_process(
                str(venv_launcher(self.instance)),
                "run",
                "--host",
                self._host,
                "--port",
                str(self._port),
                cwd=self.instance,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        )
        try:
            try:
                proc = await asyncio.shield(creation)
            except asyncio.CancelledError:
                self._register_process(await creation)
                await self._cleanup_process()
                raise
            self._register_process(proc)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            while True:
                if proc.returncode is not None:
                    await self._cleanup_process()
                    return self._failure(
                        f"分析服务提前退出（退出码 {proc.returncode}），详见插件日志"
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._cleanup_process()
                    return self._failure(
                        f"分析服务启动超时（{self.timeout:g} 秒），详见插件日志"
                    )
                try:
                    healthy = await asyncio.wait_for(
                        probe_health(self.base_url), remaining
                    )
                except TimeoutError:
                    healthy = False
                if healthy:
                    self._state = "ready"
                    self._reason = ""
                    return ReadyOutcome(True)
                await asyncio.sleep(min(0.2, max(0, deadline - loop.time())))
        except BaseException:  # 包括取消在内均先回收，随后原样传播
            await self._cleanup_process()
            raise

    async def stop(self) -> None:
        async with self._stop_lock:
            self._stopped = True
            self._state = "stopped"
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    logger.debug("已取消服务准备任务")
            await self._cleanup_process()
