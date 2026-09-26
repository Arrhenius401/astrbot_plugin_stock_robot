"""AstrBot 插件：把 stock_robot 的个股/指数分析封装为 LLM 工具，报告以图片发送。"""
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    ReportKind,
    StockRobotClient,
    fallback_image_message,
    progress_message,
    resolve_effective_timeout,
)


@register(
    "astrbot_plugin_stock_robot",
    "Arrhenius401",
    "调用本机 stock_robot 服务完成个股/指数分析，并把完整研报渲染为图片发送",
    "1.0.0",
)
class StockRobotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        base_url = str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        web_url = str(self.config.get("web_url") or "").rstrip("/") or base_url
        self._base_url = base_url
        self._web_url = web_url
        self._timeout_seconds = int(
            self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
        )
        self._client = StockRobotClient(base_url, self._timeout_seconds)

    async def terminate(self):
        """插件卸载/停用时关闭 HTTP 客户端。"""
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
        return note[-1] if note else "分析流程已结束。"

    @filter.llm_tool(name="analyze_index")
    async def analyze_index(self, event: AstrMessageEvent, symbol: str):
        """分析一个 A 股指数，并把完整研报以图片发送给用户。

        当用户询问大盘或指数（如上证指数、沪深 300、中证 500）的走势与估值时使用。

        Args:
            symbol(string): 6 位指数代码，例如 000300、399006
        """
        note: list[str] = []
        await self._run_analysis(event, "index", symbol, note)
        return note[-1] if note else "分析流程已结束。"

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
        """执行分析流程：进度 → 分析 → 报告取回 → 图片发送；失败走兜底。

        消息一律用 `await event.send(...)` 显式发送，不用生成器 yield：
        AstrBot 的权限代理（_PermissionGuardedTool）会完整消费插件的异步生成器，
        并且只保留最后一个 yield，多段 yield 会被静默丢弃。
        """
        effective_timeout = self._resolve_effective_timeout(event)
        await event.send(event.plain_result(progress_message(symbol, effective_timeout)))

        if kind == "stock":
            outcome = await self._client.analyze_stock(symbol, effective_timeout)
        else:
            outcome = await self._client.analyze_index(symbol, effective_timeout)
        if not outcome.ok:
            await event.send(event.plain_result(outcome.user_message))
            note.append("分析失败，已直接告知用户失败原因，请勿编造分析内容。")
            return

        report_id = await self._client.latest_report_id(kind, symbol, effective_timeout)
        markdown = (
            await self._client.download_report(report_id, effective_timeout)
            if report_id
            else None
        )
        if markdown is None:
            logger.warning("报告定位或下载失败: kind=%s symbol=%s", kind, symbol)
            await event.send(event.plain_result(fallback_image_message(self._web_url)))
            note.append("报告图片生成失败，已向用户发送兜底链接。")
            return

        try:
            url = await self.text_to_image(markdown)
        except Exception:  # noqa: BLE001 — 渲染属于外部能力，失败降级为链接
            logger.warning("报告转图失败", exc_info=True)
            url = ""
        if not url:
            await event.send(event.plain_result(fallback_image_message(self._web_url)))
            note.append("报告图片生成失败，已向用户发送兜底链接。")
            return

        await event.send(event.image_result(url))
        note.append("报告图片已直接发送给用户，请勿重复输出内容，用一句话简短收尾。")
