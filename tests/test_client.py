"""client.py 单测：HTTP 调用、错误分类、文案与超时收敛（respx 模拟，不依赖 astrbot）。"""

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import respx
from stock_robot_plugin_test.client import (
    StockRobotClient,
    progress_message,
    public_error_detail,
    resolve_effective_timeout,
)

BASE = "http://127.0.0.1:25618"


@pytest.fixture
def plugin_main(monkeypatch, tmp_path):
    """只替换 AstrBot 框架外壳，入口和生命周期模块使用真实产品代码。"""
    api = ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("plugin-test")
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = SimpleNamespace(llm_tool=lambda **_: lambda function: function)
    star = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

        async def text_to_image(self, text, return_url=True):
            raise AssertionError("入口测试不得调用真实转图服务")

    star.Star = Star
    star.Context = object
    star.register = lambda *_: lambda cls: cls
    paths = ModuleType("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_plugin_data_path = lambda: str(tmp_path)
    paths.get_astrbot_data_path = lambda: str(tmp_path / "astrbot-data")
    for name, module in {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": ModuleType("astrbot.core"),
        "astrbot.core.utils": ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": paths,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root.parent))
    name = f"{root.name}.main"
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


def test_plugin_modules_share_framework_logger(plugin_main, caplog):
    """入口与辅助模块统一走框架日志，参数格式能正常展开。"""
    package = plugin_main.__package__
    modules = [plugin_main] + [
        importlib.import_module(f"{package}.{name}")
        for name in ("client", "bootstrap", "launcher")
    ]
    framework_logger = sys.modules["astrbot.api"].logger
    with caplog.at_level(logging.WARNING, logger="plugin-test"):
        for module in modules:
            assert module.logger is framework_logger
            module.logger.warning("日志来源：%s", module.__name__)
    assert [record.getMessage() for record in caplog.records] == [
        f"日志来源：{module.__name__}" for module in modules
    ]


@pytest.mark.parametrize(
    "kind, valid",
    [
        ("openai_chat_completion", True),
        ("anthropic_chat_completion", True),
        ("unknown", False),
        ("azure", False),
        ("empty", False),
    ],
)
def test_default_provider_copy(plugin_main, kind, valid):
    class Context:
        async def get_using_provider_async(self, umo=None):
            assert umo is None
            return SimpleNamespace(
                provider_config={
                    "type": "openai_chat_completion"
                    if kind in {"azure", "empty"}
                    else kind,
                    "key": ["secret"],
                    **({"api_version": "version"} if kind == "azure" else {}),
                },
                get_current_key=lambda: "" if kind == "empty" else "secret",
                get_keys=lambda: ["secret"],
                get_model=lambda: "model",
                client=SimpleNamespace(base_url="https://example.com/v1"),
                base_url="https://example.com",
            )

    copied = run(plugin_main.read_llm_config(Context()))
    assert bool(copied) is valid
    if valid:
        assert copied["api_key"] == "secret" and copied["model"] == "model"


@pytest.mark.parametrize("pending", [False, True])
def test_preparation_failure_never_analyzes(plugin_main, pending):
    async def scenario():
        plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
        calls = []

        async def ready(**kwargs):
            return SimpleNamespace(ok=False, reason="准备失败", pending=pending)

        async def send(message):
            calls.append(message)

        async def forbidden(*args):
            raise AssertionError("未就绪不能分析")

        plugin._launcher = SimpleNamespace(ensure_ready=ready)
        plugin._client.analyze_stock = forbidden
        event = SimpleNamespace(send=send, plain_result=lambda value: value)
        note = []
        try:
            await plugin._run_analysis(event, "stock", "600519", note)
            assert "准备失败" in calls[-1] and "不要再调用工具" in note[-1]
            assert calls[-1].startswith("⏳" if pending else "❌")
        finally:
            await plugin._client.aclose()

    run(scenario())


def test_delivery_survives_analysis_tool_deadline(plugin_main):
    async def scenario():
        plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
        plugin._resolve_effective_timeout = lambda _: 5.5
        messages = []
        rendered = asyncio.Event()

        async def analyze(*_):
            return SimpleNamespace(ok=True)

        async def report_id(*_):
            return "report"

        async def download(*_):
            return "markdown"

        async def render(*_):
            await asyncio.sleep(0.6)
            rendered.set()
            return SimpleNamespace(png=b"png", width=1200, height=5000)

        async def send_image(_, image):
            messages.append(f"image:{image.png!r}")

        async def send(value):
            messages.append(value)

        plugin._client.analyze_stock = analyze
        plugin._client.latest_report_id = report_id
        plugin._client.download_report = download
        plugin._delivery._render = render
        plugin._delivery._send_image = send_image
        event = SimpleNamespace(
            send=send,
            plain_result=lambda value: value,
            image_result=lambda value: f"image:{value}",
        )
        note = []
        try:
            await plugin._run_analysis(event, "stock", "600519", note)
            assert not rendered.is_set() and "正在后台发送" in note[-1]
            await asyncio.gather(*plugin._delivery._tasks)
            assert rendered.is_set() and messages[-1].startswith("image:")
            assert not any("时间已耗尽" in message for message in messages)
            assert plugin._delivery._reserved == 0
        finally:
            await plugin._client.aclose()

    run(scenario())


def test_terminate_closes_client_even_if_cleanup_fails(plugin_main):
    async def scenario():
        plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})

        async def stop():
            raise OSError("清理失败")

        plugin._launcher = SimpleNamespace(stop=stop)
        with pytest.raises(OSError, match="清理失败"):
            await plugin.terminate()
        assert plugin._client._http.is_closed

    run(scenario())


@pytest.mark.parametrize("font_path_failure", [False, True])
def test_initialize_does_not_wait_for_provider(
    plugin_main, monkeypatch, font_path_failure, caplog
):
    async def scenario():
        started = []

        class Launcher:
            def __init__(self, **kwargs):
                assert callable(kwargs["llm_loader"])

            def start_background(self):
                started.append(True)

            async def stop(self):
                return None

        async def forbidden(**kwargs):
            raise AssertionError("initialize 不应等待模型接口")

        monkeypatch.setattr(plugin_main, "ServiceLauncher", Launcher)
        if font_path_failure:

            def unavailable_path():
                raise OSError("数据目录不可访问")

            monkeypatch.setattr(plugin_main, "get_astrbot_data_path", unavailable_path)
        plugin = plugin_main.StockRobotPlugin(
            SimpleNamespace(get_using_provider_async=forbidden), {}
        )
        try:
            await plugin.initialize()
            assert started == [True]
            if font_path_failure:
                assert "无法准备 AstrBot 中文字体" in caplog.text
            else:
                assert (
                    Path(plugin_main.get_astrbot_data_path()) / "font.ttf"
                ).is_file()
            assert plugin._delivery.renderer.native_render == plugin.text_to_image
        finally:
            await plugin.terminate()

    run(scenario())


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
        return_value=httpx.Response(
            500, json={"symbol": "600519", "error": "管道执行失败"}
        )
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
    assert BASE not in outcome.user_message


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
    respx.get(f"{BASE}/api/v1/reports").mock(
        return_value=httpx.Response(500, text="boom")
    )

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
        (100, 120, 100),  # 配置值较小 → 取配置值
        (300, 120, 105),  # 工具超时较小 → 工具超时 − 15
        (100, None, 100),  # 读不到工具超时 → 回落配置值
        (100, 20, 10),  # 差值低于下限 → 取下限 10
        (5, 120, 10),  # 配置值低于下限 → 取下限 10
    ],
)
def test_resolve_effective_timeout(config_timeout, tool_timeout, expected):
    assert resolve_effective_timeout(config_timeout, tool_timeout) == expected


def test_progress_message_contains_symbol_and_timeout():
    text = progress_message("600519", 100)
    assert "600519" in text
    assert "100" in text


def test_public_error_hides_links_ip_and_credentials():
    text = public_error_detail(
        "请求失败 http://192.168.1.5:8765/path 127.0.0.1:25618 api_key=TEST_SECRET_VALUE"
    )
    assert "http" not in text and "127.0.0.1" not in text
    assert "TEST_SECRET_VALUE" not in text and "请求失败" in text


def test_public_error_hides_ipv6_and_internal_paths():
    text = public_error_detail(
        r"安装失败 [::1]:25618 /AstrBot/data/plugin_data/file C:\Users\name\file"
    )
    assert "::1" not in text and "AstrBot" not in text and "Users" not in text
    assert "安装失败" in text


def test_empty_report_never_sends_blank_image_or_link(plugin_main):
    async def scenario():
        plugin = plugin_main.StockRobotPlugin(
            SimpleNamespace(), {"web_url": "http://never-send.invalid"}
        )

        async def analyze(*_):
            return SimpleNamespace(ok=True)

        async def report_id(*_):
            return "report"

        async def download(*_):
            return "   \n"

        messages = []

        async def send(message):
            messages.append(message)

        event = SimpleNamespace(send=send, plain_result=lambda value: value)
        plugin._client.analyze_stock = analyze
        plugin._client.latest_report_id = report_id
        plugin._client.download_report = download
        try:
            note = []
            await plugin._run_analysis(event, "stock", "600519", note)
            assert "未能取回报告" in messages[-1]
            assert all("http" not in message for message in messages)
            assert not plugin._delivery._tasks and plugin._delivery._reserved == 0
        finally:
            await plugin.terminate()

    run(scenario())


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"reports": {}},
        {"reports": "bad"},
        {"reports": [None]},
        {"reports": [{"id": {"bad": "value"}}]},
    ],
)
def test_latest_report_id_invalid_structure_returns_none(payload):
    respx.get(f"{BASE}/api/v1/reports").mock(
        return_value=httpx.Response(200, json=payload)
    )

    async def call():
        client = StockRobotClient(BASE)
        try:
            return await client.latest_report_id("stock", "600519", 100)
        finally:
            await client.aclose()

    assert run(call()) is None


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"detail": "api_key=TEST_SECRET_VALUE"}),
        httpx.Response(500, text="Authorization: Bearer TEST_SECRET_VALUE"),
        httpx.Response(200, text="api_key=TEST_SECRET_VALUE"),
        httpx.Response(200, json=["api_key=TEST_SECRET_VALUE"]),
    ],
)
def test_analysis_errors_hide_credentials(response, caplog):
    respx.post(f"{BASE}/api/v1/analyze").mock(return_value=response)
    outcome = run(call_stock("600519"))
    assert not outcome.ok
    assert "TEST_SECRET_VALUE" not in outcome.user_message
    assert "TEST_SECRET_VALUE" not in caplog.text


@respx.mock
def test_index_errors_hide_credentials(caplog):
    respx.post(f"{BASE}/api/v1/index").mock(
        return_value=httpx.Response(
            200, json={"reports": [], "errors": ["token=TEST_SECRET_VALUE"]}
        )
    )
    outcome = run(call_index("000300"))
    assert not outcome.ok
    assert "TEST_SECRET_VALUE" not in outcome.user_message
    assert "TEST_SECRET_VALUE" not in caplog.text


def test_error_detail_redacts_before_truncation():
    response = httpx.Response(
        500, text="x" * 180 + " api_key=" + "TEST_SECRET_VALUE" * 20
    )
    detail = StockRobotClient._error_detail(response)
    assert "TEST_SECRET" not in detail
    assert len(detail) <= 200
