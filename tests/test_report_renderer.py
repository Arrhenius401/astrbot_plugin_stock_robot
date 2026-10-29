"""原生字生图适配：原文、输出预算、读取取消和临时文件回收。"""

import asyncio
import threading
from io import BytesIO

import pytest
from PIL import Image
from stock_robot_plugin_test.report_renderer import ReportRenderer, ReportTooLarge


@pytest.mark.asyncio
async def test_native_call_preserves_markdown_and_cleans_image(tmp_path):
    path = tmp_path / "native.jpg"
    Image.new("RGB", (80, 100), "white").save(path)
    text = "# 新章节\n\n原始报告结尾"

    async def native(markdown, return_url):
        assert markdown == text and return_url is False
        return str(path)

    result = await ReportRenderer(native).render(text)
    assert (result.width, result.height) == (80, 100)
    assert Image.open(BytesIO(result.png)).format == "PNG"
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(1200, 14619), (800, 30000)])
async def test_long_report_within_pixel_budget(tmp_path, size):
    # 覆盖黄金报告 1200px 云端实际尺寸，以及原 2400 万像素边界。
    path = tmp_path / "native.png"
    Image.new("1", size).save(path)

    async def native(*args, **kwargs):
        return str(path)

    result = await ReportRenderer(native).render("黄金市场观察简报")
    assert (result.width, result.height) == size
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(800, 30624), (800, 30001)])
async def test_pixel_budget_and_invalid_output_cleanup(tmp_path, size):
    path = tmp_path / "native.png"

    async def native(*args, **kwargs):
        return str(path)

    renderer = ReportRenderer(native)
    Image.new("1", size).save(path)
    with pytest.raises(ReportTooLarge):
        await renderer.render("原文")
    assert not path.exists()
    path.write_bytes(b"not an image")
    with pytest.raises(OSError):
        await renderer.render("原文")
    assert not path.exists()


@pytest.mark.asyncio
async def test_cancelled_read_finishes_cleanup_before_return(tmp_path, monkeypatch):
    path = tmp_path / "native.png"
    Image.new("RGB", (20, 20)).save(path)
    started, finish = threading.Event(), threading.Event()

    async def native(*args, **kwargs):
        return str(path)

    renderer = ReportRenderer(native)
    original = renderer._read

    def slow_read(path):
        started.set()
        assert finish.wait(3)
        return original(path)

    monkeypatch.setattr(renderer, "_read", slow_read)
    task = asyncio.create_task(renderer.render("原文"))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and path.exists()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


@pytest.mark.asyncio
async def test_gold_width_reaches_native_renderer(tmp_path):
    path = tmp_path / "gold.png"
    Image.new("1", (1200, 100)).save(path)

    async def native(markdown, *, return_url, width):
        assert markdown == "黄金原文" and return_url is False and width == 1200
        return str(path)

    result = await ReportRenderer(native).render("黄金原文", width=1200)
    assert result.width == 1200
    assert not path.exists()
