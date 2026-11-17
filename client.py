"""stock_robot HTTP 客户端与用户文案，使用 AstrBot 框架日志。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from astrbot.api import logger

from .bootstrap import redact
from .pending_queue import normalize_index_query

DEFAULT_BASE_URL = "http://127.0.0.1:25618"
DEFAULT_TIMEOUT_SECONDS = 150
TIMEOUT_MARGIN = 15
MIN_EFFECTIVE_TIMEOUT = 10

ReportKind = Literal["stock", "index", "gold"]


@dataclass(frozen=True)
class AnalyzeOutcome:
    """一次分析请求的结果；失败时 user_message 为可直接发送给用户的文案。"""

    ok: bool
    user_message: str = ""
    markdown: str = ""
    ambiguous: bool = False
    model_note: str = ""
    resolved_symbol: str = ""


MODEL_NOTE = "只回简短确认，不得推断指数不存在，不得扩展或替换为 ETF、成分股或其他标的。"


@dataclass(frozen=True)
class ResolvedIndex:
    symbol: str
    name: str
    market: str
    return_type: str
    aliases: tuple[str, ...] = ()


class IndexResolveError(ValueError):
    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.model_note = MODEL_NOTE


@dataclass(frozen=True)
class ReportRef:
    id: str
    generated_at: float


def resolve_effective_timeout(config_timeout: int, tool_timeout: int | None) -> int:
    """收敛生效超时：min(配置值, AstrBot 工具超时 − 15)，下限 10 秒。"""
    if tool_timeout is None:
        return max(MIN_EFFECTIVE_TIMEOUT, config_timeout)
    return max(
        MIN_EFFECTIVE_TIMEOUT, min(config_timeout, tool_timeout - TIMEOUT_MARGIN)
    )


def progress_message(symbol: str) -> str:
    """提示服务准备和分析正在进行。"""
    return f"⏳ 正在准备服务并分析 {symbol}，请稍候。首次分析通常需要 30～150 秒。"


def public_error_detail(value: str) -> str:
    """用户提示不携带服务链接、IP 或凭据；详细错误保留在日志。"""
    text = redact(str(value))
    text = re.sub(r"https?://\S+", "[地址已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|\blocalhost(?::\d+)?\b",
        "[地址已隐藏]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[[0-9a-fA-F:]+\](?::\d+)?", "[地址已隐藏]", text)
    text = re.sub(
        r"\b[A-Za-z]:[\\/]\S+|(?<!\w)/(?:[\w.-]+/)+[\w.-]*", "[路径已隐藏]", text
    )
    return text.replace("\n", " ")[:200]


class StockRobotClient:
    """stock_robot 服务的异步 HTTP 客户端。

    所有方法都不向上抛异常：失败以 AnalyzeOutcome(ok=False) 或 None 返回，
    由调用方转换为用户可读消息。
    """

    def __init__(self, base_url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, *, cache_path: Path | None = None):
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http = httpx.AsyncClient()
        self._refresh_lock = asyncio.Lock()
        self._refresh_after = 0.0
        self._refresh_error: IndexResolveError | None = None
        self._index_cache: dict[str, str] = {}
        self._cache_path = cache_path
        if cache_path is not None:
            try:
                rows = json.loads(cache_path.read_text(encoding="utf-8"))
                for row in rows:
                    symbol = row["symbol"]
                    aliases = row["aliases"]
                    if not isinstance(symbol, str) or not isinstance(aliases, list) or any(
                            not isinstance(value, str) for value in aliases):
                        raise ValueError("指数身份缓存结构无效")
                    for value in (symbol, *aliases):
                        self._index_cache[normalize_index_query(value)] = symbol
            except (OSError, ValueError, TypeError, KeyError) as error:
                logger.debug("指数身份缓存不可用：%s", error)

    def cached_index(self, query: str) -> str | None:
        return self._index_cache.get(normalize_index_query(query))

    def _remember(self, result: ResolvedIndex, *, persist: bool = True) -> None:
        for value in (result.symbol, *result.aliases):
            self._index_cache[normalize_index_query(value)] = result.symbol
        if persist and self._cache_path is not None:
            import os
            import tempfile
            temporary = None
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                        dir=self._cache_path.parent, delete=False) as file:
                    temporary = file.name
                    groups: dict[str, list[str]] = {}
                    for alias, symbol in self._index_cache.items():
                        aliases = groups.setdefault(symbol, [])
                        if alias != normalize_index_query(symbol):
                            aliases.append(alias)
                    json.dump([{"symbol": symbol, "aliases": aliases} for symbol, aliases in groups.items()],
                              file, ensure_ascii=False)
                os.replace(temporary, self._cache_path)
            except (OSError, ValueError, TypeError) as error:
                logger.warning("指数身份缓存保存失败：%s", error)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_config(self, timeout: float = 3) -> dict | None:
        """只读分析服务配置；失败返回 None，由调用方决定是否提示。"""
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/v1/config", timeout=timeout
            )
        except httpx.HTTPError as exc:
            logger.debug("读取分析服务配置失败: %s", redact(str(exc)))
            return None
        if resp.status_code != 200:
            logger.debug("读取分析服务配置返回 HTTP %s", resp.status_code)
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.debug("分析服务配置响应不是有效 JSON")
            return None
        return payload if isinstance(payload, dict) else None

    async def analyze_stock(
        self, symbol: str, effective_timeout: int
    ) -> AnalyzeOutcome:
        return await self._analyze(
            "/api/v1/analyze", symbol, effective_timeout, is_index=False
        )

    async def analyze_index(self, symbol: str | ResolvedIndex, effective_timeout: int) -> AnalyzeOutcome:
        deadline = time.monotonic() + effective_timeout
        try:
            resolved = symbol if isinstance(symbol, ResolvedIndex) else await self.resolve_index(symbol, effective_timeout)
        except IndexResolveError as exc:
            return AnalyzeOutcome(False, exc.user_message, model_note=exc.model_note)
        outcome = await self._analyze("/api/v1/index", resolved.symbol,
                                      max(1, int(deadline - time.monotonic())), is_index=True)
        from dataclasses import replace
        message = outcome.user_message
        if not outcome.ok:
            message = f"已确认指数身份 {resolved.symbol}（{resolved.name}），但行情/报告失败：{message}"
        return replace(outcome, user_message=message, resolved_symbol=resolved.symbol,
                       model_note=MODEL_NOTE if not outcome.ok else "")

    async def is_index_identifier(self, raw: str) -> bool:
        query = normalize_index_query(raw)
        # 六位数字与个股共用命名空间，不能据目录命中将个股请求改判为指数。
        if re.fullmatch(r"\d{6}", query):
            return False
        if query.startswith("HSI:") or self.cached_index(query) is not None:
            return True
        try:
            payload = await self._resolve_payload(query, 3)
        except IndexResolveError:
            return False
        entry = payload.get("entry")
        if payload["status"] != "ok" or not isinstance(entry, dict):
            return False
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            return False
        return query in {normalize_index_query(value) for value in
                         (entry.get("symbol", ""), entry.get("name", ""),
                          str(entry.get("name", "")).removesuffix("指数"), *aliases)
                         if isinstance(value, str) and value}

    async def _resolve_payload(self, query: str, timeout: float) -> dict:
        try:
            response = await self._http.get(f"{self._base_url}/api/v1/indices/resolve",
                                            params={"q": query}, timeout=timeout)
            if response.status_code in (404, 405):
                raise IndexResolveError("upgrade", "当前服务不支持目录解析/刷新，请升级至绑定版本。")
            response.raise_for_status()
            payload = response.json()
            if (not isinstance(payload, dict) or payload.get("status") not in
                    {"ok", "ambiguous", "not_found"} or not isinstance(payload.get("catalog"), dict)):
                raise ValueError("目录响应缺少协议字段")
            catalog = payload["catalog"]
            if (not all(isinstance(catalog.get(key), bool) for key in ("hsi_loaded", "hsi_aliases_loaded"))
                    or "hsi_updated_at" not in catalog):
                raise IndexResolveError("upgrade", "当前服务缺少目录解析字段，请升级至绑定版本。")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, IndexResolveError):
                raise
            raise IndexResolveError("unavailable", "指数目录服务暂不可用，未能确认身份；本次未生成报告。") from exc

    def _resolved(self, payload: dict) -> ResolvedIndex | None:
        if payload["status"] == "ambiguous":
            candidates = payload.get("candidates", [])
            choices = "、".join(f"{item.get('name')}（{item.get('symbol')}，{item.get('return_type')}）"
                               for item in candidates if isinstance(item, dict))
            raise IndexResolveError("ambiguous", f"指数名称有歧义，请选择具体代码：{choices}")
        if payload["status"] != "ok":
            return None
        entry = payload.get("entry")
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key]
                for key in ("symbol", "name", "market", "return_type")):
            raise IndexResolveError("invalid", "当前服务缺少目录解析字段，请升级至绑定版本。")
        if entry["market"] == "us":
            raise IndexResolveError("market", "美股指数暂未开放。")
        if entry["market"] not in {"hk", "a-shares"}:
            raise IndexResolveError("market", "市场尚未核验，暂不可分析。")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(value, str) for value in aliases):
            raise IndexResolveError("invalid", "指数目录服务暂不可用，未能确认身份。")
        result = ResolvedIndex(entry["symbol"], entry["name"], entry["market"], entry["return_type"], tuple(aliases))
        self._remember(result)
        return result

    async def resolve_index(self, symbol: str, timeout: int) -> ResolvedIndex:
        query = normalize_index_query(symbol)
        deadline = time.monotonic() + timeout
        payload = await self._resolve_payload(query, max(1, timeout))
        resolved = self._resolved(payload)
        if resolved is not None:
            return resolved
        catalog = payload["catalog"]
        stamp = catalog.get("hsi_updated_at")
        needs_refresh = (catalog.get("hsi_loaded") is not True or
                         catalog.get("hsi_aliases_loaded") is not True or
                         not isinstance(stamp, (int, float)) or time.time() - stamp > 7 * 86400)
        if not needs_refresh:
            raise IndexResolveError("not_found", "目录未收录该写法；无法确认官方身份。")
        if deadline - time.monotonic() < 20:
            raise IndexResolveError("budget", "本次时间不足，未刷新港股指数目录；本次未生成报告。")
        async with self._refresh_lock:
            payload = await self._resolve_payload(query, max(1, deadline - time.monotonic()))
            resolved = self._resolved(payload)
            if resolved is not None:
                return resolved
            if time.monotonic() < self._refresh_after:
                if self._refresh_error:
                    raise self._refresh_error
                raise IndexResolveError("not_found", "已刷新目录，仍未收录该写法；本次未生成报告；这不等于指数不存在。")
            if deadline - time.monotonic() < 20:
                raise IndexResolveError("budget", "本次时间不足，未刷新港股指数目录。")
            try:
                response = await asyncio.wait_for(
                    self._http.post(f"{self._base_url}/api/v1/indices/refresh",
                                    params={"source": "hsi"}, timeout=12), timeout=12)
                if response.status_code in (404, 405):
                    raise IndexResolveError("upgrade", "当前服务不支持目录解析/刷新，请升级至绑定版本。")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("state") != "live":
                    raise IndexResolveError("refresh", "港股指数目录刷新失败，仍使用旧目录；未能确认身份。")
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                error = (exc if isinstance(exc, IndexResolveError) else
                         IndexResolveError("timeout", "刷新未在等待内完成，可能仍在后台更新。")
                         if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else
                         IndexResolveError("refresh", "港股指数目录刷新失败，仍使用旧目录；未能确认身份。"))
                self._refresh_after = time.monotonic() + 60
                self._refresh_error = error
                if error is exc:
                    raise
                raise error from exc
            self._refresh_after = time.monotonic() + 600
            self._refresh_error = None
        payload = await self._resolve_payload(query, max(1, deadline - time.monotonic()))
        resolved = self._resolved(payload)
        if resolved is None:
            raise IndexResolveError("not_found", "已刷新目录，仍未收录该写法；本次未生成报告；这不等于指数不存在。")
        return resolved

    async def analyze_gold(self, effective_timeout: int) -> AnalyzeOutcome:
        """生成黄金简报，直接返回本次请求对应的 Markdown。"""
        try:
            resp = await self._http.post(
                f"{self._base_url}/api/v1/gold",
                json={"refresh_cache": False},
                timeout=effective_timeout,
            )
        except httpx.TimeoutException:
            logger.warning("黄金分析请求超时（上限 %s 秒）", effective_timeout)
            return AnalyzeOutcome(False, "⏱️ 黄金分析超时，请稍后重试或调大插件超时配置。")
        except httpx.HTTPError as exc:
            logger.warning("黄金分析服务连接失败: %s", redact(str(exc)))
            return AnalyzeOutcome(False, "❌ 分析服务未启动，请检查插件日志或服务配置。")
        if resp.status_code == 404:
            return AnalyzeOutcome(False, "❌ 当前分析服务不支持黄金分析，请升级至支持黄金的 Stock Robot 版本。")
        if resp.status_code >= 400:
            detail = self._error_detail(resp)
            logger.warning("黄金分析请求失败 HTTP %s: %s", resp.status_code, detail)
            return AnalyzeOutcome(False, f"❌ 黄金分析失败：{public_error_detail(detail)}")
        try:
            data = resp.json()
        except ValueError:
            logger.warning("黄金分析响应不是有效 JSON")
            return AnalyzeOutcome(False, "❌ 黄金报告结果异常，请稍后重试。")
        markdown = data.get("markdown") if isinstance(data, dict) else None
        if not isinstance(markdown, str) or not markdown.strip():
            logger.warning("黄金分析响应缺少报告正文")
            return AnalyzeOutcome(False, "❌ 黄金报告正文不可用，请稍后重试。")
        return AnalyzeOutcome(True, markdown=markdown)

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
            logger.warning("报告库查询失败: %s", redact(str(exc)))
            return None
        if resp.status_code != 200:
            logger.warning("报告库查询返回 HTTP %s", resp.status_code)
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("报告库响应解析失败: %s", redact(str(exc)))
            return None
        if not isinstance(payload, dict):
            logger.warning("报告库响应结构异常")
            return None
        reports = payload.get("reports")
        if reports is None:
            reports = []
        if not isinstance(reports, list) or any(
            not isinstance(item, dict) for item in reports
        ):
            logger.warning("报告库列表结构异常")
            return None
        if not reports:
            logger.warning("报告库未找到 %s 的报告: %s", kind, query)
            return None
        report_id = reports[0].get("id")
        if not isinstance(report_id, (str, int)) or isinstance(report_id, bool):
            logger.warning("报告库报告标识无效")
            return None
        return str(report_id) if report_id else None

    async def latest_report(
        self, kind: ReportKind, query: str, effective_timeout: int
    ) -> ReportRef | None:
        """定位最新一条报告（报告库按时间倒序，取第一条）；失败返回 None。"""
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/v1/reports",
                params={"type": kind, "query": query},
                timeout=effective_timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("报告库查询失败: %s", redact(str(exc)))
            return None
        if resp.status_code != 200:
            logger.warning("报告库查询返回 HTTP %s", resp.status_code)
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("报告库响应解析失败: %s", redact(str(exc)))
            return None
        if not isinstance(payload, dict):
            logger.warning("报告库响应结构异常")
            return None
        reports = payload.get("reports")
        if reports is None:
            reports = []
        if not isinstance(reports, list) or any(
            not isinstance(item, dict) for item in reports
        ):
            logger.warning("报告库列表结构异常")
            return None
        if not reports:
            logger.debug("报告库暂无 %s 的报告: %s", kind, query)
            return None
        report_id = reports[0].get("id")
        if not isinstance(report_id, (str, int)) or isinstance(report_id, bool):
            logger.warning("报告库报告标识无效")
            return None
        generated_at = reports[0].get("generated_at")
        if (not report_id or isinstance(generated_at, bool)
                or not isinstance(generated_at, (int, float))
                or not math.isfinite(generated_at)):
            logger.warning("报告库生成时间无效")
            return None
        return ReportRef(str(report_id), float(generated_at))

    async def download_report(
        self, report_id: str, effective_timeout: int
    ) -> str | None:
        """下载报告 Markdown 原文；失败返回 None。"""
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/v1/reports/{report_id}/download",
                timeout=effective_timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("报告下载失败: %s", redact(str(exc)))
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
                ambiguous=True,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "无法连接分析服务 %s: %s", redact(self._base_url), redact(str(exc))
            )
            return AnalyzeOutcome(
                False,
                "❌ 分析服务未启动，请检查插件日志或服务配置",
            )

        if resp.status_code == 422:
            if is_index:
                return AnalyzeOutcome(
                    False,
                    f"❌ 无法识别指数 {symbol}，请提供 A 股六位指数代码或主仓目录中的港股指数代码。"
                    + public_error_detail(self._error_detail(resp)),
                )
            return AnalyzeOutcome(
                False,
                f"❌ 无法识别代码 {symbol}，请提供 A 股 6 位代码（如 600519）、"
                "港股 5 位代码（如 00700.HK）或美股代码（如 AAPL）",
            )

        if resp.status_code >= 400:
            detail = self._error_detail(resp)
            logger.warning("分析请求失败 HTTP %s: %s", resp.status_code, detail)
            return AnalyzeOutcome(False, f"❌ 分析失败：{public_error_detail(detail)}")

        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning(
                "分析响应不是有效 JSON: %s（正文 %.200s）",
                redact(str(exc)),
                redact(resp.text)[:200],
            )
            return AnalyzeOutcome(False, "❌ 分析结果异常，已记录日志")

        if not isinstance(data, dict):
            logger.warning("分析响应结构异常: %.200s", redact(resp.text)[:200])
            return AnalyzeOutcome(False, "❌ 分析结果异常，已记录日志")

        # 指数专用校验
        if is_index and not (data.get("reports") or []):
            errors = data.get("errors") or []
            detail = (
                "；".join(str(e) for e in errors)
                if isinstance(errors, list)
                else str(errors)
            )
            detail = redact(detail)[:200] or "无可用结果"
            logger.warning("指数分析无结果: %s", detail)
            return AnalyzeOutcome(
                False, f"❌ 指数分析失败：{public_error_detail(detail)}"
            )

        return AnalyzeOutcome(True)

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return redact(resp.text)[:200] or f"HTTP {resp.status_code}"
        if not isinstance(payload, dict):
            return redact(resp.text)[:200] or f"HTTP {resp.status_code}"
        detail = payload.get("detail") or payload.get("error")
        return redact(str(detail))[:200] if detail else f"HTTP {resp.status_code}"
