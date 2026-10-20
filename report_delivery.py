"""管理后台报告交付：内存长图、文件兜底、发送节奏和临时文件生命周期。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .report_renderer import RenderedReport, ReportRenderer
from .report_storage import ReportStorage

CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class DeliveryLimits:
    max_tasks: int = 8
    render_timeout: float = 60
    image_timeout: float = 60
    file_timeout: float = 300
    image_interval: float = 1


@dataclass
class _Session:
    lock: asyncio.Lock
    users: int = 0


class FileDeliveryError(OSError):
    """协议端明确拒绝上传或发送文件。"""


def _check_response(response: Any) -> dict:
    if not isinstance(response, dict):
        raise FileDeliveryError("文件接口响应结构异常")
    if response.get("retcode", 0) != 0 or response.get("status") in {"failed", "error"}:
        # 原始原因仅用于分类；用户消息始终使用固定文案。
        raise FileDeliveryError(
            str(
                response.get("message") or response.get("wording") or "文件接口拒绝请求"
            )
        )
    data = response.get("data", response)
    return data if isinstance(data, dict) else {}


def _size_rejected(error: Exception) -> bool:
    text = str(error).casefold()
    return any(
        term in text
        for term in (
            "file too large",
            "size limit",
            "文件太大",
            "文件过大",
            "文件大小超过",
            "413",
        )
    )


class ReportDelivery:
    def __init__(
        self,
        temp_root: Path,
        renderer: ReportRenderer,
        limits: DeliveryLimits | None = None,
        storage: ReportStorage | None = None,
    ):
        self.temp_root = temp_root
        self.renderer = renderer
        self.limits = limits or DeliveryLimits()
        self.storage = storage
        self._reserved = 0
        self._closing = False
        self._tasks: set[asyncio.Task] = set()
        self._workers: set[asyncio.Task] = set()
        self._render_gate = asyncio.Semaphore(1)
        self._image_gate = asyncio.Lock()
        self._last_image = float("-inf")
        self._sessions: dict[str, _Session] = {}

    def reserve(self) -> bool:
        """必须在开始新分析前预留，避免生成完才因队列满拒绝报告。"""
        if self._closing or self._reserved >= self.limits.max_tasks:
            return False
        self._reserved += 1
        return True

    def release(self) -> None:
        self._reserved -= 1

    async def deliver_reserved(self, event: Any, markdown: str, symbol: str, kind: str) -> bool:
        """已确认积压任务等待真实交付结果，调用方负责释放预留名额。"""
        if self._closing:
            return False
        key = str(event.unified_msg_origin)
        session = self._sessions.setdefault(key, _Session(asyncio.Lock()))
        session.users += 1
        try:
            result = await self._deliver(event, markdown, symbol, kind, session, queue_origin=True)
            return result["stage"] in {"complete", "complete-with-fallback"}
        finally:
            session.users -= 1
            if session.users == 0:
                self._sessions.pop(key, None)

    def submit(self, event: Any, markdown: str, symbol: str, kind: str) -> None:
        """接管调用方已预留的名额，任务完成或取消时归还。"""
        if self._closing:
            raise RuntimeError("插件正在停用，不能创建发送任务")
        key = str(getattr(event, "unified_msg_origin", "") or id(event))
        session = self._sessions.setdefault(key, _Session(asyncio.Lock()))
        session.users += 1
        task = asyncio.create_task(
            self._deliver(event, markdown, symbol, kind, session),
            name=f"stock-report-{symbol}",
        )
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            self.release()
            session.users -= 1
            if session.users == 0:
                self._sessions.pop(key, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("报告交付任务异常: %s", type(done.exception()).__name__)

        task.add_done_callback(finished)

    async def stop(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        # 等待原生渲染及文件读取完成后再结束停用。
        await asyncio.gather(*list(self._workers), return_exceptions=True)

    async def _render(self, markdown: str) -> RenderedReport:
        async with self._render_gate:
            worker = asyncio.create_task(self.renderer.render(markdown))
            self._workers.add(worker)
            try:
                return await asyncio.shield(worker)
            except BaseException:
                worker.cancel()
                # 重复取消交付任务也必须等文件读取清理完成。
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001 — 完成资源回收后保留原始异常
                        break
                if not worker.cancelled():
                    worker.exception()
                raise
            finally:
                self._workers.discard(worker)

    async def _notify(self, event: Any, message: str) -> None:
        try:
            await asyncio.wait_for(event.send(event.plain_result(message)), timeout=5)
        except Exception as error:  # noqa: BLE001 — 消息平台边界，提示失败仍尝试交付文件
            logger.warning("报告提示发送失败: %s", type(error).__name__)

    async def _deliver(
        self, event: Any, markdown: str, symbol: str, kind: str, session: _Session,
        *, queue_origin: bool = False,
    ) -> dict:
        task_id = None
        result = {"image": None, "state": "failed", "stage": "queued", "error": ""}
        try:
            if self.storage is not None:
                task_id = uuid.uuid4().hex
                task_id = await self.storage.io(
                    self.storage.begin, markdown, symbol, kind, task_id
                )
            await self._deliver_content(event, markdown, symbol, kind, session, result)
        except asyncio.CancelledError:
            result.update(state="cancelled", error="CancelledError")
            raise
        finally:
            if self.storage is not None:
                await self.storage.io(
                    self.storage.finish,
                    task_id,
                    result["image"],
                    result["state"],
                    "queued_delivery" if queue_origin else result["stage"],
                    result["error"],
                )
        return result

    async def _deliver_content(
        self,
        event: Any,
        markdown: str,
        symbol: str,
        kind: str,
        session: _Session,
        result: dict,
    ) -> None:
        async with session.lock:
            try:
                result["stage"] = "render"
                image = await asyncio.wait_for(
                    self._render(markdown),
                    timeout=self.limits.render_timeout,
                )
                result.update(image=image, stage="image")
                await self._send_image(event, image)
                result.update(state="success", stage="complete")
                logger.info(
                    "报告长图已提交: symbol=%s size=%sx%s bytes=%s",
                    symbol,
                    image.width,
                    image.height,
                    len(image.png),
                )
                return
            except Exception as error:  # noqa: BLE001 — 绘图／消息平台隔离，失败交付完整原文
                result["error"] = type(error).__name__
                logger.warning(
                    "报告长图不可用，改发 Markdown: symbol=%s reason=%s",
                    symbol,
                    type(error).__name__,
                )
            await self._notify(event, "报告图片未能发送，正在发送完整 Markdown 文件。")
            try:
                result["stage"] = "file"
                async with asyncio.timeout(self.limits.file_timeout):
                    await self._send_markdown(event, markdown, symbol, kind)
                logger.info("报告文件已提交: symbol=%s", symbol)
                result["stage"] = "complete-with-fallback"
            except Exception as error:  # noqa: BLE001 — 消息平台边界，不发送链接或拆分正文
                result["error"] = type(error).__name__
                logger.warning(
                    "报告文件发送失败: symbol=%s reason=%s",
                    symbol,
                    type(error).__name__,
                )
                await self._notify(
                    event,
                    "❌ 报告已生成，但图片和文件都未能发送，请检查插件日志或稍后重试。",
                )

    async def _send_image(self, event: Any, image: RenderedReport) -> None:
        from astrbot.api.message_components import Image

        async with self._image_gate:
            delay = self.limits.image_interval - (
                asyncio.get_running_loop().time() - self._last_image
            )
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await asyncio.wait_for(
                    event.send(event.chain_result([Image.fromBytes(image.png)])),
                    timeout=self.limits.image_timeout,
                )
            finally:
                self._last_image = asyncio.get_running_loop().time()

    async def _file_io(self, function: Callable, *args: Any) -> Any:
        """取消时等待文件操作结束，随后临时目录才可删除。"""
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # 超时和插件停用可能先后取消同一任务，重复取消也不能打断文件保护。
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            if not worker.cancelled():
                worker.exception()
            raise asyncio.CancelledError
        return worker.result()

    async def _send_markdown(
        self, event: Any, markdown: str, symbol: str, kind: str
    ) -> None:
        await self._file_io(self.temp_root.mkdir, 0o700, True, True)
        directory = tempfile.TemporaryDirectory(prefix="report-", dir=self.temp_root)
        try:
            clean_symbol = re.sub(r"[^A-Za-z0-9_-]", "_", symbol)[:32] or "report"
            timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            heading = next(
                (
                    line.lstrip("# ").strip()
                    for line in markdown.splitlines()
                    if line.startswith("#")
                ),
                "分析报告",
            )
            heading = (
                re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", heading)[:32].strip(" .")
                or "分析报告"
            )
            path = (
                Path(directory.name) / f"{heading}_{kind}_{clean_symbol}_{timestamp}.md"
            )
            await self._file_io(self._write_markdown, path, markdown)
            try:
                await self._send_file(event, path)
            except Exception as error:
                if not _size_rejected(error):
                    raise
                zipped = path.with_suffix(".zip")
                await self._file_io(self._zip, path, zipped)
                await self._send_file(event, zipped)
        finally:
            await self._file_io(directory.cleanup)

    @staticmethod
    def _write_markdown(path: Path, markdown: str) -> None:
        # Windows 不能把原文 LF 改成 CRLF；文件兜底须保留完整原文。
        path.write_text(markdown, encoding="utf-8", newline="")

    @staticmethod
    def _zip(path: Path, target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(path, arcname=path.name)

    async def _send_file(self, event: Any, path: Path) -> None:
        from astrbot.api.message_components import File

        platform = getattr(event, "get_platform_name", lambda: "")()
        bot = getattr(event, "bot", None)
        if platform != "aiocqhttp" or bot is None:
            await event.send(event.chain_result([File(name=path.name, file=str(path))]))
            return
        try:
            remote_path = await self._upload_stream(bot, path)
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as error:
            if _size_rejected(error):
                raise
            logger.warning(
                "文件流上传不可用，尝试适配器文件发送: %s", type(error).__name__
            )
            await event.send(event.chain_result([File(name=path.name, file=str(path))]))
            return
        group = str(event.get_group_id() or "")
        target = group or str(event.get_sender_id())
        if not target.isdigit():
            raise FileDeliveryError("文件发送目标无效")
        action = "upload_group_file" if group else "upload_private_file"
        field = "group_id" if group else "user_id"
        response = await bot.call_action(
            action, **{field: int(target), "file": remote_path, "name": path.name}
        )
        _check_response(response)

    async def _upload_stream(self, bot: Any, path: Path) -> str:
        def digest() -> tuple[str, int]:
            hasher = hashlib.sha256()
            length = 0
            with path.open("rb") as file:
                while chunk := file.read(CHUNK_SIZE):
                    hasher.update(chunk)
                    length += len(chunk)
            return hasher.hexdigest(), length

        sha256, size = await self._file_io(digest)
        chunks = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        stream = str(uuid.uuid4())
        with path.open("rb") as file:
            for index in range(chunks):
                chunk = await self._file_io(file.read, CHUNK_SIZE)
                response = await bot.call_action(
                    "upload_file_stream",
                    stream_id=stream,
                    chunk_data=base64.b64encode(chunk).decode("ascii"),
                    chunk_index=index,
                    total_chunks=chunks,
                    file_size=size,
                    expected_sha256=sha256,
                    filename=path.name,
                    file_retention=300_000,
                )
                _check_response(response)
        response = await bot.call_action(
            "upload_file_stream", stream_id=stream, is_complete=True
        )
        data = _check_response(response)
        remote = data.get("file_path") or response.get("file_path")
        if not isinstance(remote, str) or not remote:
            raise FileDeliveryError("文件流上传未返回接收端路径")
        return remote
