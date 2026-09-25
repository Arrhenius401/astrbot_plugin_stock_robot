"""stock_robot HTTP 客户端与用户文案 —— 不依赖 astrbot，可独立单测。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:25618"
DEFAULT_TIMEOUT_SECONDS = 100
TIMEOUT_MARGIN = 15
MIN_EFFECTIVE_TIMEOUT = 10

ReportKind = Literal["stock", "index"]


@dataclass(frozen=True)
class AnalyzeOutcome:
    """一次分析请求的结果；失败时 user_message 为可直接发送给用户的文案。"""

    ok: bool
    user_message: str = ""


def resolve_effective_timeout(config_timeout: int, tool_timeout: int | None) -> int:
    """收敛生效超时：min(配置值, AstrBot 工具超时 − 15)，下限 10 秒。"""
    if tool_timeout is None:
        return max(MIN_EFFECTIVE_TIMEOUT, config_timeout)
    return max(MIN_EFFECTIVE_TIMEOUT, min(config_timeout, tool_timeout - TIMEOUT_MARGIN))


def progress_message(symbol: str, effective_timeout: int) -> str:
    """分析开始前的进度提示，并展示本次实际生效的超时。"""
    return (
        f"⏳ 正在分析 {symbol}…首次分析约需 30~90 秒，请稍等"
        f"（超时上限 {effective_timeout} 秒）"
    )


def fallback_image_message(web_url: str) -> str:
    """报告图片不可用时的兜底提示（附 Web UI 报告库链接）。"""
    base = web_url.rstrip("/")
    return f"❌ 报告图片生成失败，可在 Web UI 报告库查看：{base}/#report-library"


class StockRobotClient:
    """stock_robot 服务的异步 HTTP 客户端。

    所有方法都不向上抛异常：失败以 AnalyzeOutcome(ok=False) 或 None 返回，
    由调用方转换为用户可读消息。
    """

    def __init__(self, base_url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def analyze_stock(self, symbol: str, effective_timeout: int) -> AnalyzeOutcome:
        return await self._analyze(
            "/api/v1/analyze", symbol, effective_timeout, is_index=False
        )

    async def analyze_index(self, symbol: str, effective_timeout: int) -> AnalyzeOutcome:
        return await self._analyze(
            "/api/v1/index", symbol, effective_timeout, is_index=True
        )

    async def latest_report_id(
        self, kind: ReportKind, query: str, effective_timeout: int
    ) -> str | None:
        """定位最新一条报告（报告库按时间倒序，取第一条）；失败返回 None。"""
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/v1/reports",
                params={"type": kind, "query": query},
                timeout=effective_timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("报告库查询失败: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("报告库查询返回 HTTP %s", resp.status_code)
            return None
        try:
            reports = resp.json().get("reports") or []
        except ValueError as exc:
            logger.warning("报告库响应解析失败: %s", exc)
            return None
        if not reports:
            logger.warning("报告库未找到 %s 的报告: %s", kind, query)
            return None
        report_id = reports[0].get("id")
        return str(report_id) if report_id else None

    async def download_report(self, report_id: str, effective_timeout: int) -> str | None:
        """下载报告 Markdown 原文；失败返回 None。"""
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/v1/reports/{report_id}/download",
                timeout=effective_timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("报告下载失败: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("报告下载返回 HTTP %s", resp.status_code)
            return None
        return resp.text

    # ------------------------------------------------------------------
    # 内部

    async def _analyze(
        self, path: str, symbol: str, effective_timeout: int, *, is_index: bool
    ) -> AnalyzeOutcome:
        try:
            resp = await self._http.post(
                f"{self._base_url}{path}",
                json={"symbol": symbol},
                timeout=effective_timeout,
            )
        except httpx.TimeoutException:
            logger.warning(
                "分析请求超时: %s %s（上限 %s 秒）", path, symbol, effective_timeout
            )
            return AnalyzeOutcome(
                False,
                f"⏱️ 分析超时（超过 {effective_timeout} 秒）。"
                "首次分析需拉取数据较慢，可稍后重试或调大插件超时配置",
            )
        except httpx.HTTPError as exc:
            logger.warning("无法连接分析服务 %s: %s", self._base_url, exc)
            return AnalyzeOutcome(
                False, f"❌ 分析服务未启动（{self._base_url}），请先运行 stock-robot run"
            )

        if resp.status_code == 422:
            if is_index:
                return AnalyzeOutcome(
                    False,
                    f"❌ 无法识别指数代码 {symbol}，请提供 6 位指数代码（如 000300、399006）",
                )
            return AnalyzeOutcome(
                False, f"❌ 无法识别代码 {symbol}，请提供 6 位股票代码（如 600519）"
            )

        if resp.status_code >= 400:
            detail = self._error_detail(resp)
            logger.warning("分析请求失败 HTTP %s: %s", resp.status_code, detail)
            return AnalyzeOutcome(False, f"❌ 分析失败：{detail}")

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("分析响应不是有效 JSON: %s（正文 %.200s）", exc, resp.text)
            return AnalyzeOutcome(False, "❌ 分析结果异常，已记录日志")

        if not isinstance(data, dict):
            logger.warning("分析响应结构异常: %.200s", resp.text)
            return AnalyzeOutcome(False, "❌ 分析结果异常，已记录日志")
        
        # 指数专用校验
        if is_index and not (data.get("reports") or []):
            detail = "；".join(str(e) for e in (data.get("errors") or [])) or "无可用结果"
            logger.warning("指数分析无结果: %s", detail)
            return AnalyzeOutcome(False, f"❌ 指数分析失败：{detail}")

        return AnalyzeOutcome(True)

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return resp.text[:200] or f"HTTP {resp.status_code}"
        if not isinstance(payload, dict):
            return resp.text[:200] or f"HTTP {resp.status_code}"
        detail = payload.get("detail") or payload.get("error")
        return str(detail)[:200] if detail else f"HTTP {resp.status_code}"
