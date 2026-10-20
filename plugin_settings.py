"""用户配置校验及旧版本地实例的一次迁移。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .bootstrap import instance_ready, runtime_path

DEFAULT_SERVICE_PORT = 25618


def resolve_port(config: dict[str, Any]) -> int:
    """旧地址必须先验证，不能因为新增端口默认值而丢失迁移保护。"""
    value = config.get("service_port", DEFAULT_SERVICE_PORT)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("本机分析服务端口必须是 1～65535 的整数")
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("本机分析服务端口必须是 1～65535 的整数") from error
    if not 1 <= port <= 65535:
        raise ValueError("本机分析服务端口必须是 1～65535 的整数")
    legacy = str(config.get("base_url") or "").strip()
    if legacy:
        try:
            parsed = urlsplit(legacy)
            valid = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                     and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                     and parsed.path in {"", "/"})
            old_port = parsed.port if parsed.port is not None else 80
            valid = valid and 1 <= old_port <= 65535
        except ValueError as error:
            raise ValueError("旧服务地址无效，请备份旧配置并迁移到本机服务") from error
        if not valid:
            raise ValueError("旧服务地址不是受支持的本机地址，请备份旧配置并迁移；不会自动切换实例")
        if port == DEFAULT_SERVICE_PORT:
            port = old_port
    return port


def migrate_port(config: Any, data_dir: Path) -> int:
    """在框架保存配置前保留旧值；仅迁移合法的本地旧地址。"""
    port = resolve_port(config)
    if config.get("base_url"):
        config_path = getattr(config, "config_path", None)
        if config_path and Path(config_path).is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            backup = data_dir / "config-before-port-migration.json"
            try:
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # 之前已保留迁移备份，不覆盖用户的原始配置。
                descriptor = None
            if descriptor is not None:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(Path(config_path).read_bytes())
        config["service_port"] = port
        config["base_url"] = ""
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    return port


def resolve_legacy_extras(config: dict[str, Any], instance: Path) -> str:
    """不卸载既有 RAG 环境，不将自定义实例悄悄替换成正式版。"""
    if config.get("source_archive_url"):
        raise ValueError("旧实例使用自定义源码，请备份并人工迁移；不会自动替换或升级")
    active = runtime_path(instance)
    source = active / "src/.bootstrap-source.json"
    if source.is_file():
        info = json.loads(source.read_text("utf-8"))
        if not isinstance(info, dict) or not info.get("release_tag"):
            raise ValueError("旧实例来源尚未确认正式版本，请备份并人工迁移")
    if instance_ready(instance, "rag"):
        return "rag"
    if config.get("bootstrap_extras"):
        raise ValueError("旧可选依赖实例尚未就绪，请备份并人工迁移；新安装仅使用核心依赖")
    return ""
