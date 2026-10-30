"""报告交付的异常注入、分离容器文件上传和生命周期验收。"""

import asyncio
import base64
import hashlib
import json
import sys
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from stock_robot_plugin_test.report_delivery import DeliveryLimits, ReportDelivery
from stock_robot_plugin_test.report_renderer import (
    RenderedReport,
    ReportTooLarge,
)


@pytest.fixture(autouse=True)
def components(monkeypatch):
    module = ModuleType("astrbot.api.message_components")
    module.Image = SimpleNamespace(
        fromBytes=lambda data: SimpleNamespace(kind="image", data=data)
    )
    module.File = lambda name, file: SimpleNamespace(kind="file", name=name, file_=file)
    monkeypatch.setitem(sys.modules, module.__name__, module)


class Event:
    def __init__(self, image_error=None, file_error=None, session="test"):
        self.unified_msg_origin = session
        self.image_error = image_error
        self.file_error = file_error
        self.messages = []
        self.files = []
        self.paths = []

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, message):
        if isinstance(message, str):
            self.messages.append(message)
            return
        component = message[0]
        if component.kind == "image":
            if self.image_error:
                raise self.image_error
            self.messages.append(component)
        else:
            path = Path(component.file_)
            assert path.exists()
            self.paths.append(path)
            self.files.append((component.name, path.read_bytes()))
            if self.file_error:
                raise self.file_error


class Renderer:
    def __init__(self, error=None):
        self.error = error

    async def render(self, markdown, **kwargs):
        if self.error:
            raise self.error
        return RenderedReport(markdown.encode(), 1200, 6000)


def manager(tmp_path, renderer=None, **limits):
    return ReportDelivery(
        tmp_path / "delivery_tmp",
        renderer or Renderer(),
        replace(DeliveryLimits(), image_interval=0, **limits),
    )


async def deliver(service, event, text="# 完整中文报告\n\n原始内容"):
    assert service.reserve()
    service.submit(event, text, "000300", "index")
    await asyncio.gather(*list(service._tasks))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_success_is_one_image_and_no_disk_artifact(tmp_path):
    service = manager(tmp_path)
    event = Event()
    await deliver(service, event)
    assert len(event.messages) == 1 and event.messages[0].kind == "image"
    assert not event.files and not list(tmp_path.rglob("*.png"))
    assert not list(tmp_path.rglob("*.md"))
    assert service._reserved == 0 and not service._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("render_failure", [False, True])
async def test_render_or_image_failure_sends_exact_md_and_cleans(
    tmp_path, render_failure
):
    service = manager(
        tmp_path, Renderer(ReportTooLarge("预算超出") if render_failure else None)
    )
    event = Event(image_error=OSError("网络失败"))
    original = "# 原始中文报告\n\n**完整内容**\n\n" + "最后一行" * 20
    await deliver(service, event, original)
    assert len(event.files) == 1 and event.files[0][0].endswith(".md")
    assert event.files[0][1].decode("utf-8") == original
    assert all(not path.exists() for path in event.paths)
    assert not list(service.temp_root.iterdir())
    assert not any(
        "http" in message for message in event.messages if isinstance(message, str)
    )


@pytest.mark.asyncio
async def test_file_failure_only_short_error_and_no_link(tmp_path):
    service = manager(tmp_path, Renderer(OSError("字体失败")))
    event = Event(file_error=OSError("unsupported"))
    await deliver(service, event)
    assert "图片和文件都未能发送" in event.messages[-1]
    assert all(
        "http" not in message for message in event.messages if isinstance(message, str)
    )
    assert all(not path.exists() for path in event.paths)


@pytest.mark.asyncio
async def test_native_render_failure_delivers_exact_original(tmp_path, caplog):
    service = manager(tmp_path, Renderer(OSError("原生转图失败")))
    event = Event()
    text = "# 新章节\n\n**原始内容**\n\n报告结尾"
    await deliver(service, event, text)
    assert event.files[0][1].decode("utf-8") == text
    assert "改发 Markdown" in caplog.text
    assert not list(service.temp_root.iterdir())


@pytest.mark.asyncio
async def test_explicit_size_rejection_tries_zip_once(tmp_path):
    service = manager(tmp_path, Renderer(OSError("字体失败")))

    class SizeEvent(Event):
        async def send(self, message):
            if not isinstance(message, str) and message[0].kind == "file":
                self.file_error = (
                    OSError("file too large")
                    if message[0].name.endswith(".md")
                    else None
                )
            await super().send(message)

    event = SizeEvent()
    text = "完整内容\n" * 500
    await deliver(service, event, text)
    assert [name.rsplit(".", 1)[1] for name, _ in event.files] == ["md", "zip"]
    import io

    with zipfile.ZipFile(io.BytesIO(event.files[-1][1])) as zipped:
        assert zipped.read(zipped.namelist()[0]).decode("utf-8") == text
    assert not list(service.temp_root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["12345", ""])
async def test_onebot_stream_upload_cross_container(tmp_path, group):
    calls = []
    uploaded = []
    text = "中文文件原文\n" * 15000

    class Bot:
        async def call_action(self, action, **kwargs):
            calls.append((action, kwargs))
            if action == "upload_file_stream":
                if kwargs.get("is_complete"):
                    assert (
                        hashlib.sha256(b"".join(uploaded)).hexdigest()
                        == calls[0][1]["expected_sha256"]
                    )
                    return {
                        "status": "ok",
                        "data": {"file_path": "/napcat-only/uploaded.md"},
                    }
                uploaded.append(base64.b64decode(kwargs["chunk_data"]))
                return {"status": "ok", "data": {"status": "chunk_received"}}
            assert kwargs["file"] == "/napcat-only/uploaded.md"
            assert kwargs["group_id" if group else "user_id"] == (
                12345 if group else 67890
            )
            return {"status": "ok", "retcode": 0, "data": {"file_id": "file"}}

    event = Event()
    event.bot = Bot()
    event.get_platform_name = lambda: "aiocqhttp"
    event.get_group_id = lambda: group
    event.get_sender_id = lambda: "67890"
    service = manager(tmp_path, Renderer(OSError("图片失败")))
    await deliver(service, event, text)
    assert b"".join(uploaded).decode("utf-8") == text
    assert calls[-1][0] == ("upload_group_file" if group else "upload_private_file")
    assert len(uploaded) > 1 and not event.files
    assert not list(service.temp_root.iterdir())


@pytest.mark.asyncio
async def test_stopping_waits_for_render_worker_and_releases_reservation(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()

    class BlockingRenderer:
        async def render(self, *_, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await finish.wait()

    service = manager(tmp_path, BlockingRenderer())
    event = Event()
    assert service.reserve()
    service.submit(event, "原文", "000300", "index")
    await asyncio.wait_for(started.wait(), 1)
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0.03)
    assert not stopping.done() and not service.reserve()
    finish.set()
    await stopping
    assert service._reserved == 0 and not service._workers and not event.messages


@pytest.mark.asyncio
async def test_render_timeout_waits_for_native_cleanup_before_file_fallback(tmp_path):
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingRenderer:
        async def render(self, *_, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()

    service = manager(tmp_path, BlockingRenderer(), render_timeout=0.03)
    event = Event()
    delivery = asyncio.create_task(deliver(service, event, "完整原文"))
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        assert started.is_set() and not event.files
        assert service._render_gate.locked() and service._workers
        finish.set()
        await delivery
        assert started.is_set() and event.files
        assert not service._render_gate.locked() and not service._workers
    finally:
        finish.set()
        await service.stop()


@pytest.mark.asyncio
async def test_repeated_stop_cancellation_cannot_release_render_slot_early(tmp_path):
    cleaning, finish = asyncio.Event(), asyncio.Event()

    class ClosingRenderer:
        async def render(self, *_, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()

    service = manager(tmp_path, ClosingRenderer(), render_timeout=0.01)
    event = Event()
    assert service.reserve()
    service.submit(event, "原文", "000300", "index")
    await asyncio.wait_for(cleaning.wait(), 1)
    task = next(iter(service._tasks))
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert service._render_gate.locked() and not task.done()
    finish.set()
    await service.stop()
    assert not service._workers and not service._render_gate.locked()
    assert not event.files and not event.messages


@pytest.mark.asyncio
async def test_file_send_timeout_cleans_then_reports_failure(tmp_path):
    service = manager(tmp_path, Renderer(OSError("图片失败")), file_timeout=0.03)

    class SlowEvent(Event):
        async def send(self, message):
            await super().send(message)
            if not isinstance(message, str):
                await asyncio.sleep(1)

    event = SlowEvent()
    await deliver(service, event)
    assert "图片和文件都未能发送" in event.messages[-1]
    assert all(not path.exists() for path in event.paths)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_file_write_before_cleanup(tmp_path):
    started, finish = threading.Event(), threading.Event()
    service = manager(tmp_path, Renderer(OSError("图片失败")))
    original_write = service._write_markdown

    def slow_write(path, text):
        started.set()
        assert finish.wait(3)
        assert path.parent.exists()
        original_write(path, text)

    service._write_markdown = slow_write
    event = Event()
    assert service.reserve()
    service.submit(event, "完整原文", "000300", "index")
    assert await asyncio.to_thread(started.wait, 1)
    task = next(iter(service._tasks))
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert list(service.temp_root.iterdir())
    finish.set()
    await asyncio.gather(task, return_exceptions=True)
    assert not list(service.temp_root.iterdir())
    assert service._reserved == 0


@pytest.mark.asyncio
async def test_same_session_delivery_stays_in_order(tmp_path):
    service = manager(tmp_path)
    event = Event()
    for text in ("第一份", "第二份", "第三份"):
        assert service.reserve()
        service.submit(event, text, "000300", "index")
    await asyncio.gather(*list(service._tasks))
    assert [message.data.decode() for message in event.messages] == [
        "第一份",
        "第二份",
        "第三份",
    ]


def test_capacity_rejected_before_new_analysis(tmp_path):
    service = manager(tmp_path, max_tasks=2)
    assert service.reserve() and service.reserve()
    assert not service.reserve()
    service.release()
    assert service.reserve()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "image", "render", "file"])
async def test_retention_integrated_with_delivery(tmp_path, failure):
    import json

    from stock_robot_plugin_test.report_storage import ReportStorage

    storage = ReportStorage(tmp_path)
    await storage.start()
    event = Event(
        image_error=OSError("image") if failure in {"image", "file"} else None,
        file_error=OSError("file") if failure == "file" else None,
    )
    service = ReportDelivery(
        storage.root,
        Renderer(OSError("render") if failure == "render" else None),
        storage=storage,
    )
    assert service.reserve()
    service.submit(event, "# 中文\n完整原文\n", "000300", "index")
    await asyncio.gather(*list(service._tasks))
    directories = list(storage.root.iterdir())
    assert len(directories) == 1
    directory = directories[0]
    assert artifact(directory).read_bytes() == "# 中文\n完整原文\n".encode()
    assert artifact(directory, "png").exists() == (failure != "render")
    meta = json.loads((directory / "task.json").read_text("utf-8"))
    assert meta["state"] == ("success" if failure is None else "failed")
    assert not storage.active and service._reserved == 0
    await service.stop()
    await storage.stop()


@pytest.mark.asyncio
async def test_cancelled_delivery_keeps_report_after_render_worker(tmp_path):
    import json

    from stock_robot_plugin_test.report_storage import ReportStorage

    storage = ReportStorage(tmp_path)
    await storage.start()
    entered = asyncio.Event()

    class BlockingRenderer:
        async def render(self, markdown, **kwargs):
            entered.set()
            await asyncio.Future()

    service = ReportDelivery(storage.root, BlockingRenderer(), storage=storage)
    assert service.reserve()
    service.submit(Event(), "cancelled report", "1", "stock")
    await entered.wait()
    await service.stop()
    directory = next(storage.root.iterdir())
    assert artifact(directory).read_text() == "cancelled report"
    assert json.loads((directory / "task.json").read_text())["state"] == "cancelled"
    assert not storage.active and service._reserved == 0
    await storage.stop()


@pytest.mark.asyncio
async def test_retention_failure_does_not_change_successful_send(tmp_path, monkeypatch):
    from stock_robot_plugin_test.report_storage import ReportStorage

    storage = ReportStorage(tmp_path)
    await storage.start()

    def cannot_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "_write_json", cannot_write)
    event = Event()
    service = ReportDelivery(storage.root, Renderer(), storage=storage)
    assert service.reserve()
    service.submit(event, "report", "1", "stock")
    await asyncio.gather(*list(service._tasks))
    assert len(event.messages) == 1 and not event.files
    assert service._reserved == 0 and not storage.active
    await storage.stop()


def artifact(directory, suffix="md"):
    return next(directory.glob("*." + suffix), directory / ("report." + suffix))


@pytest.mark.asyncio
@pytest.mark.parametrize("render_failure", [False, True])
async def test_webchat_delivery_survives_closed_request(tmp_path, render_failure):
    """原请求已关闭时，图片与文件兜底均通过会话接口交付。"""
    service = manager(tmp_path, Renderer(OSError("绘图失败") if render_failure else None))
    event = Event(session="webchat:FriendMessage:webchat!tester!conversation")
    event.get_platform_name = lambda: "webchat"
    received = Event()
    sessions = []

    async def send_session(session, message):
        sessions.append(session)
        await received.send(message)
        return True

    async def closed_request(message):
        raise AssertionError("后台交付不能再发送到已关闭的请求")

    service.send_message = send_session
    event.send = closed_request
    original = "# 完整中文报告\n\n正文"
    await deliver(service, event, original)
    assert sessions and set(sessions) == {event.unified_msg_origin}
    if render_failure:
        assert received.files[0][1].decode("utf-8") == original
        assert "正在发送完整 Markdown" in received.messages[0]
        assert all(not path.exists() for path in received.paths)
    else:
        assert len(received.messages) == 1 and received.messages[0].kind == "image"
    assert service._reserved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_missing", [False, True])
async def test_webchat_missing_platform_is_delivery_failure(tmp_path, sender_missing):
    service = manager(tmp_path)
    event = Event()
    event.get_platform_name = lambda: "webchat"

    async def no_platform(session, message):
        return False

    service.send_message = None if sender_missing else no_platform
    with pytest.raises(OSError, match="WebChat"):
        await service._send(event, "报告")
    assert not event.messages


@pytest.mark.asyncio
async def test_other_platform_keeps_event_delivery(tmp_path):
    async def forbidden(*args):
        raise AssertionError("其他平台不改用 WebChat 会话交付")

    service = ReportDelivery(tmp_path, Renderer(), send_message=forbidden)
    event = Event()
    event.get_platform_name = lambda: "aiocqhttp"
    await deliver(service, event)
    assert len(event.messages) == 1 and event.messages[0].kind == "image"


@pytest.mark.asyncio
async def test_gold_webchat_delivery_retains_report_copy(tmp_path):
    from stock_robot_plugin_test.report_storage import ReportStorage

    storage = ReportStorage(tmp_path)
    assert storage._lock.acquire()
    storage.enabled = True
    received = Event()

    async def send_session(session, message):
        assert session == "webchat:FriendMessage:webchat!user!session"
        await received.send(message)
        return True

    service = ReportDelivery(storage.root, Renderer(), storage=storage, send_message=send_session)
    event = Event(session="webchat:FriendMessage:webchat!user!session")
    event.get_platform_name = lambda: "webchat"
    try:
        assert service.reserve()
        service.submit(event, "黄金报告原文", "GOLD", "gold")
        await asyncio.gather(*list(service._tasks))
        await asyncio.sleep(0)
        assert len(received.messages) == 1 and received.messages[0].kind == "image"
        tasks = list(storage.root.iterdir())
        assert len(tasks) == 1
        meta = json.loads((tasks[0] / "task.json").read_text(encoding="utf-8"))
        assert meta["kind"] == "gold" and meta["state"] == "success"
        assert (tasks[0] / meta["files"][0]).read_text(encoding="utf-8") == "黄金报告原文"
        assert any(name.endswith(".png") for name in meta["files"])
    finally:
        storage._lock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, expected", [("gold", {"width": 1200}), ("stock", {}), ("index", {"width": 1200})])
async def test_report_kind_selects_width(tmp_path, kind, expected):
    class WidthRenderer:
        async def render(self, markdown, **kwargs):
            assert markdown == "原文" and kwargs == expected
            return RenderedReport(b"image", 1200 if kind in {"gold", "index"} else 800, 100)

    event = Event()
    service = manager(tmp_path, WidthRenderer())
    assert service.reserve()
    service.submit(event, "原文", "GOLD" if kind == "gold" else "000300", kind)
    await asyncio.gather(*list(service._tasks))
    assert len(event.messages) == 1 and event.messages[0].kind == "image"
    assert not event.files


def test_image_timeout_default():
    assert DeliveryLimits().image_timeout == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["large", "small", "invalid"])
async def test_prepared_image_bytes(tmp_path, caplog, mode):
    import io
    import random

    from PIL import Image

    output = io.BytesIO()
    Image.frombytes(
        "RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3)
    ).save(output, "PNG")
    original = b"invalid PNG" if mode == "invalid" else output.getvalue()
    limits = {} if mode == "small" else {"image_compress_bytes": 1, "image_compress_height": 0}
    service = manager(tmp_path, **limits)
    event = Event()
    await service._send_image(event, RenderedReport(original, 256, 256))
    sent = event.messages[0].data
    if mode == "large":
        assert sent.startswith(b"\xff\xd8\xff")
        assert len(sent) < len(original)
    else:
        assert sent == original
    if mode == "invalid":
        assert "UnidentifiedImageError" in caplog.text
