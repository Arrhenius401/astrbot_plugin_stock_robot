"""插件与内置分析服务的报告清理开关同步。

插件保留 AstrBot 配置页的开关，实际清理由分析服务执行。同步发生在服务启动前
（此时插件自建的服务进程尚未运行），直接读写服务配置，并用数据目录中的同步
基线区分“插件侧修改”和“服务端修改”，避免两边互相覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from astrbot.api import logger

from .disk_support import FileLock

SYNC_FILE = "retention-sync.json"


@dataclass(frozen=True)
class SyncOutcome:
    """一次同步的结果；warning 用于插件日志，不面向聊天用户。"""

    action: str
    service_value: bool | None = None
    warning: str = ""


def sync_cleanup_setting(config: Any, data_dir: Path, instance: Path) -> SyncOutcome:
    """把 AstrBot 的清理开关与自建服务配置对齐；不处理外部复用服务。"""
    path = Path(instance) / ".stock_robot" / "config.yaml"
    if not path.is_file():
        return SyncOutcome("missing", warning="服务配置尚未创建")
    plugin_value = _bool(config.get("cleanup_previous_reports", True), True)
    lock = FileLock(path.with_name(path.name + ".lock"))
    if not lock.acquire():
        logger.debug("报告清理设置同步跳过：服务配置正被占用")
        return SyncOutcome("locked", warning="服务配置正被占用")
    try:
        return _sync(config, path, Path(data_dir), plugin_value)
    finally:
        lock.close()


def _sync(config: Any, path: Path, data_dir: Path, plugin_value: bool) -> SyncOutcome:
    try:
        document = yaml.safe_load(path.read_text("utf-8"))
        mtime = path.stat().st_mtime_ns
    except (OSError, yaml.YAMLError):
        logger.debug("报告清理设置同步跳过：服务配置无法读取或解析")
        return SyncOutcome("invalid", warning="服务配置无法读取或解析")
    if not isinstance(document, dict):
        logger.debug("报告清理设置同步跳过：服务配置不是 YAML 对象")
        return SyncOutcome("invalid", warning="服务配置不是 YAML 对象")
    service_value = _service_value(document)
    baseline = _load_baseline(data_dir)

    if baseline is None:
        # 首次升级：服务端已有显式值就沿用，否则把插件当前值写下去。
        if service_value is not None:
            return _adopt(config, data_dir, service_value, mtime)
        return _push(path, document, data_dir, plugin_value)
    if mtime != baseline["mtime_ns"]:
        # 服务配置文件被外部修改（Web 配置页或手动编辑），以服务端为准。
        return _adopt(
            config,
            data_dir,
            service_value if service_value is not None else True,
            mtime,
        )
    if plugin_value != baseline["value"]:
        return _push(path, document, data_dir, plugin_value)
    if service_value is None:
        return _push(path, document, data_dir, plugin_value)
    if service_value != plugin_value:
        return _adopt(config, data_dir, service_value, mtime)
    return SyncOutcome("in_sync", service_value)


def _adopt(
    config: Any,
    data_dir: Path,
    value: bool,
    mtime: int,
) -> SyncOutcome:
    """服务端值回写到插件配置；保存失败时保留基线不变，下轮重试。"""
    current = _bool(config.get("cleanup_previous_reports", True), True)
    if current != value:
        config["cleanup_previous_reports"] = value
        save = getattr(config, "save_config", None)
        if callable(save):
            try:
                save()
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning(
                    "报告清理开关回写插件配置失败: %s", type(error).__name__
                )
                return SyncOutcome("adopt_failed", value, "插件配置保存失败")
    _save_baseline(data_dir, value, mtime)
    if current != value:
        logger.info(
            "报告清理开关已按分析服务配置同步为%s", "开启" if value else "关闭"
        )
    return SyncOutcome("in_sync" if current == value else "adopted", value)


def _push(path: Path, document: dict, data_dir: Path, value: bool) -> SyncOutcome:
    """把插件值写入服务配置；已相同则只更新同步基线。"""
    if _service_value(document) == value:
        _save_baseline(data_dir, value, path.stat().st_mtime_ns)
        return SyncOutcome("in_sync", value)
    try:
        _write_service_value(path, document, value)
        mtime = path.stat().st_mtime_ns
    except OSError as error:
        logger.warning(
            "报告清理开关写入分析服务配置失败: %s", type(error).__name__
        )
        return SyncOutcome("write_failed", value, "服务配置写入失败")
    _save_baseline(data_dir, value, mtime)
    logger.info("报告清理开关已写入分析服务配置：%s", "开启" if value else "关闭")
    return SyncOutcome("pushed", value)


def _write_service_value(path: Path, document: dict, value: bool) -> None:
    maintenance = document.get("maintenance")
    if not isinstance(maintenance, dict):
        maintenance = {}
        document["maintenance"] = maintenance
    maintenance["report_cleanup_enabled"] = value
    source = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=".config-", suffix=".yaml.tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _service_value(document: dict) -> bool | None:
    maintenance = document.get("maintenance")
    if not isinstance(maintenance, dict):
        return None
    value = maintenance.get("report_cleanup_enabled")
    return value if isinstance(value, bool) else None


def _load_baseline(data_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((Path(data_dir) / SYNC_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("value"), bool)
        or type(payload.get("mtime_ns")) is not int
    ):
        return None
    return {"value": payload["value"], "mtime_ns": payload["mtime_ns"]}


def _save_baseline(data_dir: Path, value: bool, mtime: int) -> None:
    path = Path(data_dir) / SYNC_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps({"value": value, "mtime_ns": mtime}), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        logger.warning("报告清理同步基线写入失败: %s", type(error).__name__)


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default
