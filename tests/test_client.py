"""client.py 单测：HTTP 调用、错误分类、文案与超时收敛（respx 模拟，不依赖 astrbot）。"""

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

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
    event.filter = SimpleNamespace(llm_tool=lambda **_: lambda function: function,
                                   on_llm_request=lambda: lambda function: function)
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


@pytest.mark.asyncio
async def test_unavailable_service_queues_group_shared_latest_three(plugin_main):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    plugin._launcher = SimpleNamespace(state="starting")
    event = SimpleNamespace(unified_msg_origin="group")
    try:
        notes = []
        for symbol in ("600519", "00700", "000001", "09988"):
            await plugin._run_analysis(event, "stock", symbol, notes)
        assert [x["symbol"] for x in plugin._pending.get("group")["targets"]] == [
            "00700.HK", "000001", "09988.HK",
        ]
        assert "600519" in notes[-1]
        assert plugin._delivery._reserved == 0
    finally:
        await plugin._client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("question_sent", [False, True])
async def test_new_request_clears_waiting_queue_before_normal_analysis(plugin_main, question_sent):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    plugin._pending.enqueue("group", "stock", "00700")
    record = plugin._pending.get("group")
    if question_sent:
        plugin._pending.asked("group", record["id"], "继续吗？")
    plugin._launcher = SimpleNamespace(state="ready")

    async def perform(*args):
        assert plugin._pending.get("group") is None

    plugin._perform_analysis = perform
    event = SimpleNamespace(unified_msg_origin="group")
    try:
        await plugin._run_analysis(event, "stock", "600519", [])
    finally:
        await plugin._client.aclose()


@pytest.mark.asyncio
async def test_confirmation_can_only_dispatch_current_session_once(plugin_main):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    plugin._pending.enqueue("group", "stock", "00700")
    record = plugin._pending.get("group")
    plugin._pending.asked("group", record["id"], "继续吗？")
    executed = []

    async def execute(event, batch):
        executed.append(batch)

    plugin._run_confirmed_batch = execute
    try:
        await plugin.confirm_waiting_analysis(SimpleNamespace(unified_msg_origin="other"), record["id"], "confirm")
        assert not executed
        # 未检查 sender_id；群内任意成员的明确确认有效。
        event = SimpleNamespace(unified_msg_origin="group", sender_id="another-user")
        await plugin.confirm_waiting_analysis(event, record["id"], "confirm")
        await plugin.confirm_waiting_analysis(event, record["id"], "confirm")
        await asyncio.gather(*list(plugin._analysis_tasks))
        assert len(executed) == 1
    finally:
        await plugin._client.aclose()


@pytest.mark.asyncio
async def test_ai_failure_blocks_report_across_all_retries(plugin_main, monkeypatch):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    generated = []
    reports = []

    async def fail(*args):
        generated.append(1)
        raise TimeoutError("model")

    async def no_delay(*args):
        return None

    async def deliver(*args):
        reports.append(1)
        return True

    monkeypatch.setattr(plugin_main, "generate_text", fail)
    monkeypatch.setattr(plugin_main.asyncio, "sleep", no_delay)
    plugin._delivery.deliver_reserved = deliver
    try:
        await plugin._deliver_confirmed_report(SimpleNamespace(unified_msg_origin="group"), "report", "00700.HK", "stock")
        assert len(generated) == 3
        assert not reports
    finally:
        await plugin._client.aclose()


@pytest.mark.asyncio
async def test_reload_recovers_generated_report_without_reanalysis(plugin_main, monkeypatch):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    plugin._pending.enqueue("group", "stock", "00700")
    queued = plugin._pending.get("group")
    plugin._pending.asked("group", queued["id"], "继续吗？")
    record = plugin._pending.confirm("group", queued["id"])
    record["steps"] = [{"kind": "stock", "symbol": "00700.HK", "state": "report_ready",
                        "markdown": "saved report", "bridge": "刚才的研报现在发给你。"}]
    plugin._pending.save_accepted(record)
    generated = []
    delivered = []

    async def forbidden(*args, **kwargs):
        generated.append(1)
        raise AssertionError("不能重新生成报告或文案")

    async def deliver(event, markdown, *args):
        delivered.append(markdown)
        return True

    async def send(*args):
        pass

    monkeypatch.setattr(plugin_main, "generate_text", forbidden)
    plugin._client.analyze_stock = forbidden
    plugin._delivery.deliver_reserved = deliver
    try:
        event = SimpleNamespace(unified_msg_origin="group", send=send, plain_result=lambda value: value)
        await plugin._run_confirmed_batch(event, record)
        assert delivered == ["saved report"]
        assert not generated
        assert plugin._pending.accepted() == []
        assert plugin._delivery._reserved == 0
    finally:
        await plugin._client.aclose()


@pytest.mark.asyncio
async def test_reload_does_not_blindly_repeat_interrupted_analysis(plugin_main):
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    plugin._pending.enqueue("group", "stock", "00700")
    queued = plugin._pending.get("group")
    plugin._pending.asked("group", queued["id"], "继续吗？")
    record = plugin._pending.confirm("group", queued["id"])
    record["steps"] = [{"kind": "stock", "symbol": "00700.HK", "state": "analyzing"}]
    plugin._pending.save_accepted(record)
    try:
        await plugin._run_confirmed_batch(SimpleNamespace(unified_msg_origin="group"), record)
        assert plugin._pending.accepted() == []
        with plugin._pending._transaction() as conn:
            body = conn.execute("SELECT body FROM accepted WHERE id=?", (record["id"],)).fetchone()[0]
        assert "analysis_interrupted_unknown_result" in body
    finally:
        await plugin._client.aclose()


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
        event = SimpleNamespace(send=send, plain_result=lambda value: value,
                                unified_msg_origin="test")
        note = []
        try:
            await plugin._run_analysis(event, "stock", "600519", note)
            assert len(calls) == 2
            assert calls[0] == progress_message("600519")
            assert "准备失败" in calls[-1] and "不要再调用工具" in note[-1]
            assert calls[-1].startswith("⏳" if pending else "❌")
        finally:
            await plugin._client.aclose()

    run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,symbol", [("stock", "00700.HK"), ("index", "000300")])
async def test_ready_analysis_sends_only_one_progress_message(plugin_main, kind, symbol):
    """服务就绪时只发送分析进度，避免连续出现两条等待提示。"""
    plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), {})
    event = SimpleNamespace(send=AsyncMock(), plain_result=lambda value: value)

    async def ready(**kwargs):
        event.send.assert_awaited_once_with(progress_message(symbol))
        return SimpleNamespace(ok=True)

    plugin._launcher = SimpleNamespace(ensure_ready=AsyncMock(side_effect=ready))
    analyze = AsyncMock(return_value=SimpleNamespace(ok=True))
    plugin._client.analyze_stock = analyze
    plugin._client.analyze_index = analyze
    plugin._client.latest_report_id = AsyncMock(return_value="report")
    plugin._client.download_report = AsyncMock(return_value="# 报告")
    try:
        markdown = await plugin._perform_analysis(
            event, kind, symbol, [], asyncio.get_running_loop().time() + 95
        )
        assert markdown == "# 报告"
        analyze.assert_awaited_once()
        actual_symbol, timeout = analyze.await_args.args
        assert actual_symbol == symbol and 0 < timeout <= 95
        event.send.assert_awaited_once_with(progress_message(symbol))
        plugin._launcher.ensure_ready.assert_awaited_once()
    finally:
        await plugin._client.aclose()


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
            unified_msg_origin="test",
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
            assert plugin._delivery.renderer.native_render == plugin_main._render_report
        finally:
            await plugin.terminate()

    run(scenario())


def test_render_report_uses_local_t2i(plugin_main, monkeypatch):
    calls = []

    class Renderer:
        async def render_t2i(self, text, *, use_network=True, return_url=True, template_name=None):
            calls.append((text, use_network, return_url, template_name))
            return "/tmp/report.jpg"

    monkeypatch.setattr(
        sys.modules["astrbot.core"], "html_renderer", Renderer(), raising=False
    )
    assert run(plugin_main._render_report("报告")) == "/tmp/report.jpg"
    assert calls == [("报告", False, False, None)]


def test_settings_defaults_and_explicit_legacy_values(plugin_main, monkeypatch):
    async def scenario():
        captured = []

        class Launcher:
            def __init__(self, **kwargs):
                captured.append(kwargs)

            def start_background(self):
                return None

            async def stop(self):
                return None

        monkeypatch.setattr(plugin_main, "ServiceLauncher", Launcher)
        monkeypatch.setattr(plugin_main, "ensure_chinese_font", lambda _: None)
        for config, cleanup, timeout in [({}, True, 1800),
                ({"cleanup_previous_reports": False, "install_timeout_seconds": 2400,
                  "auto_install": False, "service_port": 18080}, False, 2400)]:
            plugin = plugin_main.StockRobotPlugin(SimpleNamespace(), config)
            try:
                await plugin.initialize()
                assert plugin._storage.cleanup_raw is cleanup
                assert plugin._install_timeout == timeout
                assert captured[-1]["auto_install"] is True
                assert captured[-1]["archive_url"] is None and captured[-1]["extras"] == ""
                assert plugin._base_url == f"http://127.0.0.1:{config.get('service_port', 25618)}"
            finally:
                await plugin.terminate()

    run(scenario())


def test_report_renderer_calls_real_local_wrapper_with_path_output(plugin_main, monkeypatch, tmp_path):
    """贯通公共渲染器与真实包装函数，不能分别用兼容桩掩盖签名错误。"""
    from PIL import Image

    image_path = tmp_path / "local-t2i.png"
    calls = []

    class Renderer:
        async def render_t2i(self, text, *, use_network=True, return_url=True, template_name=None):
            calls.append((text, use_network, return_url, template_name))
            Image.new("RGB", (80, 120), "white").save(image_path)
            return str(image_path)

    monkeypatch.setattr(sys.modules["astrbot.core"], "html_renderer", Renderer(), raising=False)
    rendered = run(plugin_main.ReportRenderer(plugin_main._render_report).render("# 完整研报\n\n正文"))
    assert (rendered.width, rendered.height) == (80, 120)
    assert rendered.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert calls == [("# 完整研报\n\n正文", False, False, None)]
    assert not image_path.exists()


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
    assert "A 股 6 位代码" in outcome.user_message
    assert "00700.HK" in outcome.user_message


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


def test_progress_message_describes_preparation_and_analysis():
    text = progress_message("600519")
    assert "600519" in text
    assert "准备服务" in text
    assert "超时上限" not in text


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

        event = SimpleNamespace(send=send, plain_result=lambda value: value,
                                unified_msg_origin="test")
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
