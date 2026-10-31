"""插件与内置分析服务的布尔设置同步。

插件保留 AstrBot 配置页的开关，实际行为由分析服务执行。同步发生在服务启动前
（此时插件自建的服务进程尚未运行），直接读写服务配置，并用数据目录中的每键
基线区分“插件侧修改”和“服务端修改”，避免两边互相覆盖。只处理插件自建的
本地服务；健康复用的外部服务不写配置。
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

SYNC_FILE = "service-settings-sync.json"
LEGACY_SYNC_FILE = "retention-sync.json"
SYNC_VERSION = 1


@dataclass(frozen=True)
class SettingSpec:
    """一个需要与插件配置保持一致的布尔服务设置。"""

    name: str
    plugin_key: str
    section: str
    key: str
    default: bool
    label: str


SPECS = (
    SettingSpec("extra_analysis", "extra_analysis_enabled", "analysis", "extra_enabled", False, "额外分析"),
    SettingSpec("report_cleanup", "cleanup_previous_reports", "maintenance", "report_cleanup_enabled", True, "报告清理"),
)


@dataclass(frozen=True)
class SyncOutcome:
    """一次同步中单个设置的结果；warning 用于插件日志和一次性提示。"""

    setting: str
    action: str
    service_value: bool | None = None
    warning: str = ""


@dataclass(frozen=True)
class _Decision:
    spec: SettingSpec
    action: str
    target: bool


def sync_service_settings(config: Any, data_dir: Path, instance: Path) -> list[SyncOutcome]:
    """把插件开关与自建服务配置对齐；不处理外部复用服务。"""
    path = Path(instance) / ".stock_robot" / "config.yaml"
    if not path.is_file():
        return [_outcome(spec, "missing", warning="服务配置尚未创建") for spec in SPECS]
    lock = FileLock(path.with_name(path.name + ".lock"))
    if not lock.acquire():
        logger.debug("服务设置同步跳过：配置正被占用")
        return [_outcome(spec, "locked", warning="服务配置正被占用") for spec in SPECS]
    try:
        return _sync(config, path, Path(data_dir))
    finally:
        lock.close()


def _sync(config: Any, path: Path, data_dir: Path) -> list[SyncOutcome]:
    try:
        document = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError):
        logger.debug("服务设置同步跳过：配置无法读取或解析")
        return [_outcome(spec, "invalid", warning="服务配置无法读取或解析") for spec in SPECS]
    if not isinstance(document, dict):
        logger.debug("服务设置同步跳过：配置不是 YAML 对象")
        return [_outcome(spec, "invalid", warning="服务配置不是 YAML 对象") for spec in SPECS]

    baseline = _load_baseline(data_dir)
    decisions = [_decide(spec, document, config, baseline) for spec in SPECS]
    service_changed = [
        decision
        for decision in decisions
        if decision.action == "push" and _service_value(document, decision.spec) != decision.target
    ]
    write_failed = False
    if service_changed:
        for decision in service_changed:
            _set_service_value(document, decision.spec, decision.target)
        try:
            _write_document(path, document)
        except OSError as error:
            write_failed = True
            logger.warning("服务设置写入失败: %s", type(error).__name__)

    plugin_changes = [
        decision
        for decision in decisions
        if decision.action in {"adopt", "conflict"}
        and _bool(config.get(decision.spec.plugin_key, decision.spec.default), decision.spec.default)
        != decision.target
    ]
    plugin_saved = True
    if plugin_changes:
        for decision in plugin_changes:
            config[decision.spec.plugin_key] = decision.target
        save = getattr(config, "save_config", None)
        if callable(save):
            try:
                save()
            except (OSError, RuntimeError, ValueError) as error:
                plugin_saved = False
                logger.warning("服务设置回写插件配置失败: %s", type(error).__name__)

    outcomes: list[SyncOutcome] = []
    updated = dict(baseline)
    for decision in decisions:
        spec = decision.spec
        action = decision.action
        warning = ""
        synced = True
        if action == "push":
            needs_write = any(item is decision for item in service_changed)
            if write_failed and needs_write:
                action, warning, synced = "write_failed", "服务配置写入失败", False
            else:
                action = "pushed" if needs_write else "in_sync"
                if action == "pushed":
                    logger.info("已写入分析服务设置「%s」：%s", spec.label, _text(decision.target))
        elif action in {"adopt", "conflict"}:
            was_conflict = action == "conflict"
            changed = any(item is decision for item in plugin_changes)
            action = "adopted" if changed and plugin_saved else (
                "adopt_failed" if changed else "in_sync"
            )
            synced = action != "adopt_failed"
            if synced:
                logger.info(
                    "已按分析服务配置同步「%s」：%s", spec.label, _text(decision.target)
                )
            if was_conflict:
                warning = "插件与服务端同时修改，以服务端为准"
                logger.warning("「%s」同步冲突：%s", spec.label, warning)
            elif action == "adopt_failed":
                warning = "插件配置保存失败"
        else:
            action = "in_sync"
        if synced:
            updated[spec.name] = decision.target
        outcomes.append(SyncOutcome(spec.name, action, decision.target, warning))
    _save_baseline(data_dir, updated)
    return outcomes


def _decide(
    spec: SettingSpec,
    document: dict,
    config: Any,
    baseline: dict[str, bool],
) -> _Decision:
    explicit = _service_value(document, spec)
    plugin = _bool(config.get(spec.plugin_key, spec.default), spec.default)
    last = baseline.get(spec.name)
    if last is None:
        if explicit is not None:
            return _Decision(spec, "adopt", explicit)
        if plugin == spec.default:
            return _Decision(spec, "in_sync", spec.default)
        return _Decision(spec, "push", plugin)
    service_changed = (explicit != last) if explicit is not None else (spec.default != last)
    plugin_changed = plugin != last
    if service_changed and plugin_changed:
        target = explicit if explicit is not None else spec.default
        return _Decision(spec, "conflict", target)
    if service_changed:
        target = explicit if explicit is not None else spec.default
        return _Decision(spec, "adopt", target)
    if plugin_changed:
        return _Decision(spec, "push", plugin)
    return _Decision(spec, "in_sync", last)


def _outcome(spec: SettingSpec, action: str, warning: str = "") -> SyncOutcome:
    return SyncOutcome(spec.name, action, None, warning)


def _service_value(document: dict, spec: SettingSpec) -> bool | None:
    section = document.get(spec.section)
    if not isinstance(section, dict):
        return None
    value = section.get(spec.key)
    return value if isinstance(value, bool) else None


def _set_service_value(document: dict, spec: SettingSpec, value: bool) -> None:
    section = document.get(spec.section)
    if not isinstance(section, dict):
        section = {}
        document[spec.section] = section
    section[spec.key] = value


def _write_document(path: Path, document: dict) -> None:
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


def _load_baseline(data_dir: Path) -> dict[str, bool]:
    path = Path(data_dir) / SYNC_FILE
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict):
        settings = payload.get("settings")
        if isinstance(settings, dict):
            return {key: value for key, value in settings.items() if isinstance(value, bool)}
    return _load_legacy_baseline(data_dir)


def _load_legacy_baseline(data_dir: Path) -> dict[str, bool]:
    """兼容只同步过清理开关的旧版本基线。"""
    try:
        payload = json.loads((Path(data_dir) / LEGACY_SYNC_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), bool):
        return {}
    return {"report_cleanup": payload["value"]}


def _save_baseline(data_dir: Path, values: dict[str, bool]) -> None:
    path = Path(data_dir) / SYNC_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": SYNC_VERSION, "settings": values}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as error:
        logger.warning("服务设置同步基线写入失败: %s", type(error).__name__)


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _text(value: bool) -> str:
    return "开启" if value else "关闭"
