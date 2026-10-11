"""AstrBot 插件：把 stock_robot 的个股/指数分析封装为 LLM 工具，报告以图片发送。"""

import asyncio
import inspect
import math
from functools import partial
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

from .bootstrap import (
    DEFAULT_ARCHIVE_URL,
    INSTALL_TIMEOUT_SECONDS,
    InstallProgress,
    run_command,
)
from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    ReportKind,
    StockRobotClient,
    progress_message,
    public_error_detail,
    resolve_effective_timeout,
)
from .font_support import ensure_chinese_font
from .launcher import ServiceLauncher
from .report_delivery import ReportDelivery
from .report_renderer import ReportRenderer


async def read_llm_config(context: Context) -> dict | None:
    """只在首次配置时复制默认模型，不随聊天会话同步。"""
    try:
        provider = await context.get_using_provider_async(umo=None)
        if provider is None:
            return None
        config = provider.provider_config
        provider_type = config.get("type")
        if not isinstance(provider_type, str):
            return None
        kind = {
            "openai_chat_completion": "openai",
            "anthropic_chat_completion": "claude",
        }.get(provider_type)
        if kind is None or config.get("api_version"):
            logger.warning("默认模型类型无法复制，请在分析服务 Web UI 配置模型")
            return None
        keys = provider.get_keys()
        key = provider.get_current_key()
        model = provider.get_model()
        if inspect.isawaitable(key):
            key = await key
        if inspect.isawaitable(model):
            model = await model
        if (
            not isinstance(keys, (list, tuple))
            or not keys
            or not isinstance(key, str)
            or not key.strip()
            or not isinstance(model, str)
            or not model.strip()
        ):
            logger.warning("默认模型的 Key 或模型名为空，服务暂以关闭 LLM 的配置运行")
            return None
        client = getattr(provider, "client", None)
        base_url = (
            getattr(provider, "base_url", None)
            if kind == "claude"
            else getattr(client, "base_url", None)
        )
        return {
            "enabled": True,
            "provider": kind,
            "api_key": key,
            "model": model,
            "base_url": str(base_url or config.get("api_base") or ""),
        }
    except Exception as exc:  # noqa: BLE001 — AstrBot 模型接口隔离，失败允许无模型启动
        logger.warning(
            "读取默认模型失败（%s），请在分析服务 Web UI 配置模型", type(exc).__name__
        )
        return None


class StockRobotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        base_url = str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._base_url = base_url
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_stock_robot"
        self._delivery = ReportDelivery(
            data_dir / "delivery_tmp", ReportRenderer(self.text_to_image)
        )
        self._analysis_locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}
        self._analysis_tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._timeout_seconds = int(
            self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
        )
        self._launcher: ServiceLauncher | None = None
        self._startup_timeout = float(self.config.get("startup_timeout_seconds", 60))
        if not math.isfinite(self._startup_timeout) or self._startup_timeout <= 0:
            raise ValueError("startup_timeout_seconds 必须大于 0")
        self._install_timeout = float(
            self.config.get("install_timeout_seconds", INSTALL_TIMEOUT_SECONDS)
        )
        if not math.isfinite(self._install_timeout) or self._install_timeout <= 0:
            raise ValueError("install_timeout_seconds 必须为大于 0 的有限秒数")
        self._client = StockRobotClient(base_url, self._timeout_seconds)

    async def initialize(self):
        """准备任务由 launcher 持有，插件加载立即返回。"""
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_stock_robot"
        try:
            await asyncio.to_thread(ensure_chinese_font, Path(get_astrbot_data_path()))
        except Exception as error:  # noqa: BLE001 — 路径/线程准备失败也不阻止插件加载
            logger.warning(
                "无法准备 AstrBot 中文字体: %s: %s", type(error).__name__, error
            )
        progress = InstallProgress(data_dir / "service.log")
        self._launcher = ServiceLauncher(
            base_url=self._base_url,
            archive_url=self.config.get("source_archive_url") or DEFAULT_ARCHIVE_URL,
            extras=str(self.config.get("bootstrap_extras") or ""),
            auto_install=bool(self.config.get("auto_install", True)),
            timeout=self._startup_timeout,
            data_dir=data_dir,
            llm=None,
            runner=partial(
                run_command,
                log_path=data_dir / "service.log",
                timeout=self._install_timeout,
                progress=progress,
            ),
            install_timeout=self._install_timeout,
            package_index_url=str(self.config.get("package_index_url") or "").strip(),
            progress=progress,
            llm_loader=partial(read_llm_config, self.context),
        )
        self._launcher.start_background()

    async def terminate(self):
        """先回收任务和自建服务，HTTP 客户端始终关闭。"""
        self._stopping = True
        try:
            try:
                for task in list(self._analysis_tasks):
                    task.cancel()
                await asyncio.gather(
                    *list(self._analysis_tasks), return_exceptions=True
                )
                await self._delivery.stop()
            finally:
                if self._launcher is not None:
                    await self._launcher.stop()
        finally:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # LLM 工具

    @filter.llm_tool(name="analyze_stock")
    async def analyze_stock(self, event: AstrMessageEvent, symbol: str):
        """分析一只 A 股个股，并把完整研报以图片发送给用户。

        当用户询问某只 A 股个股的分析、评价、是否值得关注或买入时使用。

        Args:
            symbol(string): 6 位股票代码，例如 600519
        """
        note: list[str] = []
        await self._run_analysis(event, "stock", symbol, note)
        return note[-1] if note else "流程已结束。只回一句简短确认，不要再调用工具。"

    @filter.llm_tool(name="analyze_index")
    async def analyze_index(self, event: AstrMessageEvent, symbol: str):
        """分析一个 A 股指数，并把完整研报以图片发送给用户。

        当用户询问大盘或指数（如上证指数、沪深 300、中证 500）的走势与估值时使用。

        Args:
            symbol(string): 6 位指数代码，例如 000300、399006
        """
        note: list[str] = []
        await self._run_analysis(event, "index", symbol, note)
        return note[-1] if note else "流程已结束。只回一句简短确认，不要再调用工具。"

    # ------------------------------------------------------------------
    # 流程与辅助

    def _resolve_effective_timeout(self, event: AstrMessageEvent) -> int:
        """读取 AstrBot 工具超时并收敛；读取失败回退插件配置值。"""
        tool_timeout: int | None = None
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            provider_settings = (
                cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
            )
            raw = provider_settings.get("tool_call_timeout")
            tool_timeout = int(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — 配置读取失败按插件配置超时处理
            logger.warning(
                "读取 AstrBot tool_call_timeout 失败，使用插件配置超时", exc_info=True
            )
        return resolve_effective_timeout(self._timeout_seconds, tool_timeout)

    async def _run_analysis(
        self,
        event: AstrMessageEvent,
        kind: ReportKind,
        symbol: str,
        note: list[str],
    ) -> None:
        """分析／取报告使用工具截止时间；完整报告由受管理任务独立发送。"""
        if self._stopping or not self._delivery.reserve():
            await event.send(
                event.plain_result("⏳ 报告任务较多或插件正在停用，请稍后再试。")
            )
            note.append("当前繁忙，已提示用户。只回简短确认，不要再次调用工具。")
            return
        effective_timeout = self._resolve_effective_timeout(event)
        deadline = asyncio.get_running_loop().time() + effective_timeout - 5
        submitted = False
        key = (kind, symbol)
        lock, users = self._analysis_locks.get(key, (asyncio.Lock(), 0))
        self._analysis_locks[key] = (lock, users + 1)
        task = asyncio.current_task()
        if task is not None:
            self._analysis_tasks.add(task)
        try:
            async with asyncio.timeout(max(0, effective_timeout - 5)):
                # 同一标的的分析与取报告互斥，避免取到另一次调用的“最新报告”。
                async with lock:
                    markdown = await self._perform_analysis(
                        event, kind, symbol, note, deadline
                    )
            if markdown is not None:
                self._delivery.submit(event, markdown, symbol, kind)
                submitted = True
                note.append(
                    "报告已生成，正在后台发送。只回一句简短确认，不要声称已送达，"
                    "不要复述报告内容，不要再调用工具。"
                )
        except TimeoutError:
            await asyncio.wait_for(
                event.send(
                    event.plain_result(
                        "❌ 本次调用时间已耗尽，请稍后重试；后台准备任务会继续。"
                    )
                ),
                timeout=5,
            )
            note.append(
                "调用时间已耗尽，原因已发给用户。只回简短确认，不要再调用工具。"
            )
        finally:
            if not submitted:
                self._delivery.release()
            if task is not None:
                self._analysis_tasks.discard(task)
            _, users = self._analysis_locks[key]
            if users == 1:
                self._analysis_locks.pop(key, None)
            else:
                self._analysis_locks[key] = (lock, users - 1)

    async def _perform_analysis(
        self,
        event: AstrMessageEvent,
        kind: ReportKind,
        symbol: str,
        note: list[str],
        deadline: float,
    ) -> str | None:
        """执行分析流程：进度 → 分析 → 报告取回；成功返回完整原文。

        消息一律用 `await event.send(...)` 显式发送，不用生成器 yield：
        AstrBot 的权限代理（_PermissionGuardedTool）会完整消费插件的异步生成器，
        并且只保留最后一个 yield，多段 yield 会被静默丢弃。
        """

        def remaining() -> int:
            return max(1, int(deadline - asyncio.get_running_loop().time()))

        await event.send(event.plain_result("正在准备分析服务，请稍候…"))
        if self._launcher is not None:
            outcome = await self._launcher.ensure_ready(
                wait_timeout=max(
                    0,
                    min(
                        self._startup_timeout,
                        deadline - asyncio.get_running_loop().time(),
                    ),
                )
            )
            if not outcome.ok:
                message = (
                    f"⏳ 分析服务准备中：{public_error_detail(outcome.reason)}"
                    if getattr(outcome, "pending", False)
                    else f"❌ 分析服务未就绪：{public_error_detail(outcome.reason)}"
                )
                await event.send(event.plain_result(message))
                note.append(
                    "服务未就绪，原因已发给用户。只回简短确认，不要再调用工具。"
                )
                return
        effective_timeout = remaining()
        await event.send(
            event.plain_result(progress_message(symbol, effective_timeout))
        )

        if kind == "stock":
            outcome = await self._client.analyze_stock(symbol, effective_timeout)
        else:
            outcome = await self._client.analyze_index(symbol, effective_timeout)
        if not outcome.ok:
            await event.send(event.plain_result(outcome.user_message))
            note.append(
                "分析失败，原因已直接发给用户。只回一句简短确认，不要编造分析内容，不要再调用工具。"
            )
            return

        report_id = await self._client.latest_report_id(kind, symbol, remaining())
        markdown = (
            await self._client.download_report(report_id, remaining())
            if report_id
            else None
        )
        if markdown is None or not markdown.strip():
            logger.warning("报告定位或下载失败: kind=%s symbol=%s", kind, symbol)
            await event.send(
                event.plain_result(
                    "❌ 分析已完成，但未能取回报告，请检查插件日志或稍后重试。"
                )
            )
            note.append(
                "报告取回失败，原因已发给用户。只回一句简短确认，不要再调用工具。"
            )
            return
        return markdown
