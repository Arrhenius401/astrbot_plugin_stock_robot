"""插件报告留存、诊断与受保护的周期清理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path

from astrbot.api import logger

from .disk_support import FileLock, RotatingLog, safe_path

# 与 main.py MANAGED_RELEASE_TAG 对应的 src/report/formatter.py；升级受管版本时同步更新。
FORMATTER_SHA256 = "40b9779816eee0d657b615812deec86669b5479ef233c59bef64c6c29008b536"
TASK_ID = re.compile(r"[0-9a-f]{32}")
ARTIFACT = re.compile(r"(?:stock|index|gold)_[A-Za-z0-9_-]{1,32}_\d{8}_\d{6}\.(?:md|png)")


def serialized(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        with self._disk_guard:
            return function(self, *args, **kwargs)

    return guarded


class ReportStorage:
    def __init__(
        self,
        data_dir: Path,
        *,
        artifact_budget=256 * 1024**2,
        diagnostic_budget=16 * 1024**2,
        clock=time.time,
    ):
        self.data_dir = data_dir
        self.root = data_dir / "delivery_tmp"
        self.diagnostics = data_dir / "delivery_diagnostics"
        self.artifact_budget = artifact_budget
        self.diagnostic_budget = diagnostic_budget
        self.clock = clock
        self.active: set[str] = set()
        self._disk_guard = threading.RLock()
        self._warned_unknown = False
        self._lock = FileLock(data_dir / ".report-retention.lock")
        self.enabled = False
        self._gate = asyncio.Lock()
        self._periodic = None
        self.can_clean_raw: Callable[[], bool] = lambda: False
        self.cleanup_raw = False
        self.busy: Callable[[], bool] = lambda: bool(self.active)

    async def io(self, function, *args):
        """取消也等待线程完成，不能在文件工作未结束时解除保护。"""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    async def start(self):
        try:
            self.enabled = await self.io(self._lock.acquire)
            if not self.enabled:
                logger.warning("报告留存锁不可用；跳过留存与删除，继续报告交付")
                return
            await self.sweep()
            self._periodic = asyncio.create_task(self._loop())
        except asyncio.CancelledError:
            self.enabled = False
            self._lock.close()
            raise
        except Exception as error:  # noqa: BLE001 — 清理失败不阻止插件加载
            logger.warning("报告清理初始化失败: %s", type(error).__name__)

    async def stop(self):
        if self._periodic is not None:
            self._periodic.cancel()
            await asyncio.gather(self._periodic, return_exceptions=True)
        async with self._gate:
            self.enabled = False
            self._lock.close()

    async def _loop(self):
        while True:
            await asyncio.sleep(600)
            await self.sweep()

    async def before_task(self, reserve):
        async with self._gate:
            await self._sweep_guarded()
            return reserve()

    async def sweep(self):
        async with self._gate:
            await self._sweep_guarded()

    async def _sweep_guarded(self):
        if not self.enabled:
            return
        try:
            await self.io(RotatingLog(self.data_dir / "service.log").write, "")
            await self.io(self.clean)
            if self.cleanup_raw and not self.busy():
                if self.can_clean_raw():
                    await self.io(self.clean_raw)
                else:
                    logger.debug("原始报告清理跳过：服务归属、任务状态或版本未确认")
        except Exception as error:  # noqa: BLE001 — 清理失败不阻止报告分析
            logger.warning("报告清理失败，继续服务: %s", type(error).__name__)

    def _write_json(self, path, value):
        if not safe_path(path):
            raise OSError("留存路径不安全")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        if not safe_path(temporary):
            raise OSError("元数据临时路径不安全")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_artifact(self, path: Path, content: bytes):
        temporary = path.with_name(path.name + ".tmp")
        if not safe_path(path) or not safe_path(temporary):
            raise OSError("产物路径不安全")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @serialized
    def begin(self, markdown: str, symbol: str, kind: str, task_id=None) -> str | None:
        if not self.enabled:
            return None
        task_id = task_id or uuid.uuid4().hex
        self.active.add(task_id)
        directory = self.root / task_id
        try:
            if not safe_path(directory):
                raise OSError("任务目录不安全")
            directory.mkdir(mode=0o700, parents=True)
            safe_symbol = re.sub(r"[^A-Za-z0-9_-]", "_", symbol)[:32] or "report"
            if kind not in {"stock", "index", "gold"}:
                raise ValueError("未知报告类型")
            started = self.clock()
            filename = f"{kind}_{safe_symbol}_{datetime.fromtimestamp(started).astimezone():%Y%m%d_%H%M%S}.md"
            meta = {
                "version": 1,
                "owner": "stock-robot-delivery",
                "id": task_id,
                "symbol": re.sub(r"[^\w.-]", "_", symbol)[:64],
                "kind": kind,
                "started": started,
                "ended": None,
                "state": "running",
                "files": [filename],
            }
            self._write_json(directory / "task.json", meta)
            self._write_artifact(directory / filename, markdown.encode("utf-8"))
            return task_id
        except Exception as error:  # noqa: BLE001 — 留存失败仍交付内存原文
            self.active.discard(task_id)
            logger.warning("报告副本创建失败，继续交付: %s", type(error).__name__)
            return None

    @serialized
    def finish(self, task_id, image, state: str, stage: str, error: str):
        if task_id is None:
            return
        try:
            meta = self._metadata(self.root / task_id, allow_running=True)
            if meta is None:
                raise OSError("任务元数据无法验证")
            directory = self.root / task_id
            image_path = (directory / meta["files"][0]).with_suffix(".png")
            if image is not None and safe_path(image_path):
                try:
                    self._write_artifact(image_path, image.png)
                    meta["files"].append(image_path.name)
                except OSError:
                    logger.warning("图片副本写入失败，保留 Markdown 与诊断")
            meta.update(ended=self.clock(), state=state, stage=stage, error=error)
            if image is not None:
                meta.update(
                    image_width=image.width,
                    image_height=image.height,
                    image_bytes=len(image.png),
                )
            self._write_json(directory / "task.json", meta)
            diagnostic = {key: value for key, value in meta.items() if key != "files"}
            diagnostic["elapsed_seconds"] = max(0, meta["ended"] - meta["started"])
            self._write_json(self.diagnostics / f"{task_id}.json", diagnostic)
        except Exception as exc:  # noqa: BLE001 — 留存失败不能改判实际交付结果
            logger.warning("报告留存完成失败: %s", type(exc).__name__)
        finally:
            self.active.discard(task_id)
        try:
            self.clean()
        except OSError as exc:
            logger.warning("交付后清理失败: %s", type(exc).__name__)

    def _metadata(self, directory, *, allow_running=False):
        try:
            path = directory / "task.json"
            if (
                not TASK_ID.fullmatch(directory.name)
                or not safe_path(path)
                or path.stat().st_size > 16384
            ):
                return None
            meta = json.loads(path.read_text("utf-8"))
            if (
                not isinstance(meta, dict)
                or meta.get("version") != 1
                or meta.get("owner") != "stock-robot-delivery"
                or meta.get("id") != directory.name
            ):
                return None
            if meta.get("state") not in {
                "running",
                "success",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return None
            if (
                not isinstance(meta.get("files"), list)
                or not meta["files"]
                or any(
                    not isinstance(name, str)
                    or (
                        name not in {"report.md", "report.png"}
                        and not ARTIFACT.fullmatch(name)
                    )
                    for name in meta["files"]
                )
                or not meta["files"][0].endswith(".md")
            ):
                return None
            for key in (
                ("started",) if meta.get("state") == "running" else ("started", "ended")
            ):
                if not isinstance(meta.get(key), (int, float)) or not math.isfinite(
                    meta[key]
                ):
                    return None
            if meta["state"] == "running" and not allow_running:
                # 独占锁已取得，上一进程退出；以本次发现时间开始 72 小时窗口。
                meta.update(
                    ended=self.clock(),
                    state="interrupted",
                    stage="restart",
                    error="Interrupted",
                )
                self._write_json(path, meta)
                self._write_json(
                    self.diagnostics / f"{directory.name}.json",
                    {k: v for k, v in meta.items() if k != "files"},
                )
            return meta
        except (OSError, ValueError, TypeError):
            return None

    @serialized
    def clean(self):
        if not safe_path(self.root) or not safe_path(self.diagnostics):
            logger.warning("报告清理路径不安全，跳过")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        now = self.clock()
        tasks = []
        for directory in self.root.iterdir():
            if directory.name in self.active:
                continue
            meta = self._metadata(directory)
            if meta is None:
                if not self._warned_unknown:
                    logger.warning(
                        "存在无可信元数据的旧产物，已保留；请手动确认 delivery_tmp 中的遗留文件"
                    )
                    self._warned_unknown = True
                continue
            files = [directory / name for name in meta["files"]]
            if not all(safe_path(path) for path in files):
                continue
            try:
                size = sum(
                    path.stat().st_size
                    for path in directory.iterdir()
                    if path.is_file() and safe_path(path)
                )
                tasks.append((meta["ended"], directory, meta, size))
            except OSError:
                continue
        total = sum(task[3] for task in tasks)
        tasks.sort(key=lambda task: task[0])
        # 必须先处理全部到期任务，再做容量淘汰。
        for expired_pass in (True, False):
            for task in list(tasks):
                ended, directory, meta, size = task
                expired = (
                    now - ended >= (24 if meta["state"] == "success" else 72) * 3600
                )
                if (
                    (expired_pass and expired)
                    or (not expired_pass and total > self.artifact_budget)
                ) and self._remove_task(
                    directory, meta, "expired" if expired_pass else "capacity"
                ):
                    total -= size
                    tasks.remove(task)
        records = []
        for path in self.diagnostics.glob("*.json"):
            try:
                if (
                    not TASK_ID.fullmatch(path.stem)
                    or not safe_path(path)
                    or path.stat().st_size > 16384
                ):
                    continue
                record = json.loads(path.read_text("utf-8"))
                ended = record.get("ended")
                if (
                    record.get("owner") != "stock-robot-delivery"
                    or record.get("id") != path.stem
                    or not isinstance(ended, (float, int))
                    or not math.isfinite(ended)
                ):
                    continue
                retention = 72 * 3600 if record.get("stage") == "queued_delivery" else 7 * 86400
                records.append((ended, path, path.stat().st_size, retention))
            except (OSError, ValueError, AttributeError):
                continue
        total = sum(record[2] for record in records)
        for expired_pass in (True, False):
            for record in sorted(records):
                ended, path, size, retention = record
                if (expired_pass and now - ended >= retention) or (
                    not expired_pass and total > self.diagnostic_budget
                ):
                    try:
                        if safe_path(path):
                            path.unlink()
                            total -= size
                            records.remove(record)
                    except OSError as error:
                        logger.warning("诊断清理失败: %s", type(error).__name__)

    def _remove_task(self, directory, meta, reason):
        if directory.name in self.active or not safe_path(directory):
            return False
        try:
            # 逐文件删除，不对未知内容递归删除。
            allowed = {
                *meta["files"],
                "task.json",
                "task.json.tmp",
                *(name + ".tmp" for name in meta["files"]),
            }
            if any(
                path.name not in allowed or not safe_path(path) or not path.is_file()
                for path in directory.iterdir()
            ):
                return False
            # 元数据最后删除，文件占用导致部分清理失败后仍可在下轮安全重试。
            for path in sorted(
                directory.iterdir(), key=lambda path: path.name == "task.json"
            ):
                if not safe_path(path) or directory.name in self.active:
                    return False
                path.unlink()
            directory.rmdir()
            diagnostic = {k: v for k, v in meta.items() if k != "files"}
            diagnostic.update(cleanup_reason=reason, cleaned_at=self.clock())
            self._write_json(self.diagnostics / f"{directory.name}.json", diagnostic)
            return True
        except OSError as error:
            logger.warning("报告文件删除失败: %s", type(error).__name__)
            return False

    @serialized
    def clean_raw(self):
        instance = self.data_dir / "instance"
        formatter = instance / "src/src/report/formatter.py"
        root = instance / "reports"
        if not safe_path(formatter) or not safe_path(root):
            return
        if (
            not formatter.is_file()
            or hashlib.sha256(formatter.read_bytes()).hexdigest() != FORMATTER_SHA256
        ):
            logger.warning("原始报告清理跳过：未验证的服务报告布局")
            return
        if not root.is_dir():
            return
        for kind in ("stock", "index"):
            category = root / kind
            if not safe_path(category) or not category.is_dir():
                continue
            for symbol in category.iterdir():
                if (
                    not re.fullmatch(r"[A-Za-z0-9_-]+", symbol.name)
                    or not safe_path(symbol)
                    or not symbol.is_dir()
                ):
                    continue
                for month in symbol.iterdir():
                    if (
                        not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month.name)
                        or not safe_path(month)
                        or not month.is_dir()
                    ):
                        continue
                    for path in month.iterdir():
                        if not re.fullmatch(
                            re.escape(symbol.name) + r"_\d{8}_\d{6}\.md", path.name
                        ):
                            continue
                        if (
                            safe_path(path)
                            and path.is_file()
                            # 有结构快照时保留整组，避免破坏报告库与重试。
                            and not path.with_suffix(".report.json").exists()
                            and not path.with_suffix(".report.json").is_symlink()
                            and self.clock() - path.stat().st_mtime > 7 * 86400
                        ):
                            path.unlink()
                    if safe_path(month) and not any(month.iterdir()):
                        month.rmdir()
                if safe_path(symbol) and not any(symbol.iterdir()):
                    symbol.rmdir()
