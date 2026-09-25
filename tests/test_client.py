"""client.py 单测：HTTP 调用、错误分类、文案与超时收敛（respx 模拟，不依赖 astrbot）。"""

import asyncio

import httpx
import respx

from client import StockRobotClient

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
