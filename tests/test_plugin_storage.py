"""真实插件入口与存储生命周期接线，分析服务使用隔离替身。"""

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest
from stock_robot_plugin_test.report_renderer import RenderedReport


@pytest.fixture
def plugin_module(monkeypatch, tmp_path):
    api = sys.modules["astrbot.api"]
    monkeypatch.setattr(api, "AstrBotConfig", dict, raising=False)
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = SimpleNamespace(llm_tool=lambda **kwargs: lambda function: function,
                                   on_llm_request=lambda: lambda function: function)
    star = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

        async def text_to_image(self, *args, **kwargs):
            raise AssertionError("渲染在本测试中单独注入")

    star.Star, star.Context = Star, object
    paths = ModuleType("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_data_path = lambda: str(tmp_path / "framework")
    paths.get_astrbot_plugin_data_path = lambda: str(tmp_path / "plugins")
    components = ModuleType("astrbot.api.message_components")
    components.Image = SimpleNamespace(
        fromBytes=lambda data: SimpleNamespace(data=data)
    )
    for module in (event, star, paths, components):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    name = "stock_robot_plugin_test.main"
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "ensure_chinese_font", lambda path: None)

    class Launcher:
        def __init__(self, **kwargs):
            self.stopped = False

        def start_background(self):
            pass

        async def ensure_ready(self, **kwargs):
            return SimpleNamespace(ok=True)

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(module, "ServiceLauncher", Launcher)
    yield module
    sys.modules.pop(name, None)


class Event:
    unified_msg_origin = "isolated"

    def __init__(self):
        self.messages = []

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_plugin_entry_creates_retained_report_and_stops_cleanly(plugin_module):
    plugin = plugin_module.StockRobotPlugin(SimpleNamespace(), {})
    await plugin.initialize()
    assert plugin._storage.enabled

    class Client:
        closed = False

        async def analyze_index(self, *args):
            return SimpleNamespace(ok=True)

        async def latest_report_id(self, *args):
            return "isolated"

        async def download_report(self, *args):
            return "# 完整中文\n原文\n"

        async def aclose(self):
            self.closed = True

    class Renderer:
        async def render(self, text):
            return RenderedReport(b"png", 10, 10)

    await plugin._client.aclose()
    client = Client()
    plugin._client = client
    plugin._delivery.renderer = Renderer()
    plugin._resolve_effective_timeout = lambda event: 100
    await plugin._run_analysis(Event(), "index", "000300", [])
    await asyncio.gather(*list(plugin._delivery._tasks))
    directory = next(plugin._storage.root.iterdir())
    assert artifact(directory).read_bytes() == "# 完整中文\n原文\n".encode()
    assert plugin._delivery._reserved == 0
    await plugin.terminate()
    assert (
        client.closed
        and plugin._launcher.stopped
        and plugin._storage._lock.file is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "timeout", "cancel"])
async def test_uncertain_analysis_keeps_delivery_released(plugin_module, failure):
    """分析失败或取消后不得占用交付名额；原始报告清理由主仓负责。"""
    plugin = plugin_module.StockRobotPlugin(
        SimpleNamespace(), {"cleanup_previous_reports": True}
    )
    await plugin.initialize()

    class Client:
        async def analyze_index(self, *args):
            if failure == "timeout":
                raise TimeoutError()
            if failure == "cancel":
                raise asyncio.CancelledError()
            return SimpleNamespace(ok=False, user_message="failed")

        async def aclose(self):
            pass

    await plugin._client.aclose()
    plugin._client = Client()
    plugin._resolve_effective_timeout = lambda event: 100
    try:
        await plugin._run_analysis(Event(), "index", "000300", [])
    except asyncio.CancelledError:
        pass
    assert plugin._delivery._reserved == 0
    await plugin.terminate()


def artifact(directory, suffix="md"):
    return next(directory.glob("*." + suffix), directory / ("report." + suffix))
