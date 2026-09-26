"""client.py 单测：HTTP 调用、错误分类、文案与超时收敛（respx 模拟，不依赖 astrbot）。"""

import asyncio

import httpx
import pytest
import respx

from client import (
    StockRobotClient,
    fallback_image_message,
    progress_message,
    resolve_effective_timeout,
)

BASE = "http://127.0.0.1:25618"


def run(coro):
    """在独立事件循环中执行协程（不引入 pytest-asyncio 依赖）。"""
    return asyncio.run(coro)


async def call_stock(symbol: str, timeout: int = 100):
    client = StockRobotClient(BASE, timeout)
    try:
        return await client.analyze_stock(symbol, timeout)
    finally:
        await client.aclose()


@respx.mock
def test_analyze_stock_success():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(200, json={"symbol": "600519", "name": "贵州茅台"})
    )
    outcome = run(call_stock("600519"))
    assert outcome.ok is True
    assert outcome.user_message == ""


@respx.mock
def test_analyze_stock_invalid_symbol_422():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(422, json={"detail": "无效的股票代码: 999999"})
    )
    outcome = run(call_stock("999999"))
    assert outcome.ok is False
    assert "999999" in outcome.user_message
    assert "6 位股票代码" in outcome.user_message


@respx.mock
def test_analyze_stock_server_error_500():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(500, json={"symbol": "600519", "error": "管道执行失败"})
    )
    outcome = run(call_stock("600519"))
    assert outcome.ok is False
    assert "管道执行失败" in outcome.user_message


@respx.mock
def test_analyze_stock_timeout():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    outcome = run(call_stock("600519", timeout=42))
    assert outcome.ok is False
    assert "42" in outcome.user_message
    assert "超时" in outcome.user_message


@respx.mock
def test_analyze_stock_connect_error():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    outcome = run(call_stock("600519"))
    assert outcome.ok is False
    assert "分析服务未启动" in outcome.user_message
    assert BASE in outcome.user_message


@respx.mock
def test_analyze_stock_malformed_body():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    outcome = run(call_stock("600519"))
    assert outcome.ok is False
    assert "分析结果异常" in outcome.user_message


async def call_index(symbol: str, timeout: int = 100):
    client = StockRobotClient(BASE, timeout)
    try:
        return await client.analyze_index(symbol, timeout)
    finally:
        await client.aclose()


@respx.mock
def test_analyze_index_success():
    respx.post(f"{BASE}/api/v1/index").mock(
        return_value=httpx.Response(200, json={"reports": [{"code": "000300"}]})
    )
    outcome = run(call_index("000300"))
    assert outcome.ok is True


@respx.mock
def test_analyze_index_empty_reports():
    respx.post(f"{BASE}/api/v1/index").mock(
        return_value=httpx.Response(
            200, json={"reports": [], "errors": ["无法识别指数 999999"]}
        )
    )
    outcome = run(call_index("999999"))
    assert outcome.ok is False
    assert "无法识别指数 999999" in outcome.user_message


@respx.mock
def test_analyze_index_invalid_symbol_422():
    respx.post(f"{BASE}/api/v1/index").mock(
        return_value=httpx.Response(422, json={"detail": "无效的指数代码"})
    )
    outcome = run(call_index("abc"))
    assert outcome.ok is False
    assert "6 位指数代码" in outcome.user_message


@respx.mock
def test_latest_report_id_returns_first():
    respx.get(f"{BASE}/api/v1/reports").mock(
        return_value=httpx.Response(
            200,
            json={"reports": [{"id": "newest"}, {"id": "older"}], "total": 2},
        )
    )

    async def call():
        client = StockRobotClient(BASE, 100)
        try:
            return await client.latest_report_id("stock", "600519", 100)
        finally:
            await client.aclose()

    assert run(call()) == "newest"


@respx.mock
def test_latest_report_id_empty_returns_none():
    respx.get(f"{BASE}/api/v1/reports").mock(
        return_value=httpx.Response(200, json={"reports": [], "total": 0})
    )

    async def call():
        client = StockRobotClient(BASE, 100)
        try:
            return await client.latest_report_id("stock", "600519", 100)
        finally:
            await client.aclose()

    assert run(call()) is None


@respx.mock
def test_latest_report_id_server_error_returns_none():
    respx.get(f"{BASE}/api/v1/reports").mock(return_value=httpx.Response(500, text="boom"))

    async def call():
        client = StockRobotClient(BASE, 100)
        try:
            return await client.latest_report_id("stock", "600519", 100)
        finally:
            await client.aclose()

    assert run(call()) is None


@respx.mock
def test_download_report_ok():
    respx.get(f"{BASE}/api/v1/reports/abc/download").mock(
        return_value=httpx.Response(200, text="# 贵州茅台（600519）分析报告")
    )

    async def call():
        client = StockRobotClient(BASE, 100)
        try:
            return await client.download_report("abc", 100)
        finally:
            await client.aclose()

    assert run(call()) == "# 贵州茅台（600519）分析报告"


@respx.mock
def test_download_report_404_returns_none():
    respx.get(f"{BASE}/api/v1/reports/abc/download").mock(
        return_value=httpx.Response(404, json={"detail": "报告不存在"})
    )

    async def call():
        client = StockRobotClient(BASE, 100)
        try:
            return await client.download_report("abc", 100)
        finally:
            await client.aclose()

    assert run(call()) is None

@pytest.mark.parametrize(
    ("config_timeout", "tool_timeout", "expected"),
    [
        (100, 120, 100),   # 配置值较小 → 取配置值
        (300, 120, 105),   # 工具超时较小 → 工具超时 − 15
        (100, None, 100),  # 读不到工具超时 → 回落配置值
        (100, 20, 10),     # 差值低于下限 → 取下限 10
        (5, 120, 10),      # 配置值低于下限 → 取下限 10
    ],
)
def test_resolve_effective_timeout(config_timeout, tool_timeout, expected):
    assert resolve_effective_timeout(config_timeout, tool_timeout) == expected


def test_progress_message_contains_symbol_and_timeout():
    text = progress_message("600519", 100)
    assert "600519" in text
    assert "100" in text


def test_fallback_image_message_strips_trailing_slash():
    text = fallback_image_message("http://192.168.1.5:8765/")
    assert text.endswith("/#report-library")
    assert "192.168.1.5:8765/#report-library" in text
